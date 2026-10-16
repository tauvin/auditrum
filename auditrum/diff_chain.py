"""Per-object consistency check for the audit chain.

Diff-based reconstruction is correct only if every change to a tracked row
went through the audit trigger. Writes that bypass it
(``session_replication_role = replica``, ``ALTER TABLE ... DISABLE TRIGGER``,
``TRUNCATE``, bulk loads) break that silently. The chain is self-checking:
per object and per column, each event's ``old`` must equal the previous
event's ``new``, the last event must agree with the live row, and INSERT /
DELETE must alternate with the row's existence. :func:`verify_diff_chain`
checks exactly that with one set-based query per table and returns the
first mismatch per object.

This is consistency, not tamper evidence: that is the hash chain's job.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Literal

from psycopg import sql

from auditrum.executor import ConnectionProtocol
from auditrum.retention import _parse_interval
from auditrum.tracking.spec import validate_identifier

__all__ = [
    "DiffChainMismatch",
    "MismatchKind",
    "list_registered_tables",
    "parse_since",
    "verify_diff_chain",
]

MismatchKind = Literal["chain", "live", "lifecycle"]

_TRACKING_TABLE = "auditrum_applied_triggers"


@dataclass(frozen=True)
class DiffChainMismatch:
    """The first inconsistency found for one object."""

    table: str
    object_id: str
    audit_id: int | None
    kind: MismatchKind
    column: str | None
    expected: Any
    actual: Any
    detail: str


# Built with psycopg.sql: {audit} is the audit table identifier, every value
# is a bound parameter. "%%s" is a literal "%s" for format(), "{{}}" a literal "{}".
# Values compare as jsonb, except timestamptz: to_jsonb renders it in the
# session TimeZone, so one instant written or read in different zones would
# differ as text. Dropped columns are not in cols and compare as jsonb.
# {scope} narrows the objects, {body} builds ev / entries / kv (one variant
# reads the diff only, the other the snapshots too), {chain_guard} picks the
# kv rows that carry an old value.
_VERIFY_SQL = """
WITH spec AS (
    SELECT _audit_tracked_spec(%(table)s) AS s
),
cols AS (
    -- A domain resolves to its base type; timestamptz(n) has the same type oid.
    SELECT a.attname::text AS key,
           COALESCE(NULLIF(t.typbasetype, 0), a.atttypid) = 'timestamptz'::regtype AS is_tstz
    FROM pg_attribute a
    JOIN pg_type t ON t.oid = a.atttypid
    WHERE a.attrelid = quote_ident(%(table)s)::regclass
      AND a.attnum > 0
      AND NOT a.attisdropped
),
scope AS (
    SELECT DISTINCT a.object_id
    FROM {audit} a
    WHERE a.table_name = %(table)s
      AND (%(full)s OR %(since)s::timestamptz IS NULL OR a.changed_at >= %(since)s)
      AND (%(object_id)s::text IS NULL OR a.object_id = %(object_id)s){scope}
),
live AS (
    SELECT sc.object_id, _audit_live_row(%(table)s, sc.object_id, spec.s) AS row_data
    FROM scope sc, spec
),
{body}mismatches AS (
    -- chain: old[k] differs from new[k] of the previous event touching k
    SELECT k.object_id, k.id AS audit_id, 'chain' AS kind, k.key AS column_name,
           k.prev_new AS expected, k.old_v AS actual,
           format('old value of %%s differs from event %%s', k.key, k.prev_id) AS detail,
           k.changed_at
    FROM kv k
    WHERE {chain_guard}
      AND CASE WHEN k.is_tstz
                    AND jsonb_typeof(k.old_v) = 'string' AND jsonb_typeof(k.prev_new) = 'string'
               THEN (k.old_v #>> '{{}}')::timestamptz IS DISTINCT FROM (k.prev_new #>> '{{}}')::timestamptz
               ELSE k.old_v IS DISTINCT FROM k.prev_new END
    UNION ALL
    -- lifecycle inside the history
    SELECT e.object_id, e.id, 'lifecycle', NULL, NULL, NULL,
           CASE WHEN e.operation = 'INSERT'
                THEN 'INSERT for a row that already existed'
                ELSE e.operation || ' after DELETE without INSERT' END,
           e.changed_at
    FROM ev e
    WHERE (e.operation = 'INSERT' AND e.prev_op IS NOT NULL AND e.prev_op <> 'DELETE')
       OR (e.operation <> 'INSERT' AND e.prev_op = 'DELETE')
    UNION ALL
    -- lifecycle at the end: live row vs last event
    SELECT e.object_id, e.id, 'lifecycle', NULL, NULL, NULL,
           CASE WHEN e.operation = 'DELETE' THEN 'row exists although the last event is DELETE'
                ELSE 'row is missing although the last event is ' || e.operation END,
           NULL
    FROM ev e JOIN live l ON l.object_id = e.object_id
    WHERE e.id = e.last_id
      AND ((e.operation = 'DELETE') = (l.row_data IS NOT NULL))
    UNION ALL
    -- live: last event touching k vs the live row
    SELECT k.object_id, k.id, 'live', k.key, k.new_v, l.row_data -> k.key,
           format('live value of %%s differs from event %%s', k.key, k.id),
           NULL
    FROM kv k JOIN live l ON l.object_id = k.object_id
    WHERE k.rn_desc = 1 AND l.row_data ? k.key
      AND CASE WHEN k.is_tstz
                    AND jsonb_typeof(k.new_v) = 'string' AND jsonb_typeof(l.row_data -> k.key) = 'string'
               THEN (k.new_v #>> '{{}}')::timestamptz IS DISTINCT FROM (l.row_data ->> k.key)::timestamptz
               ELSE k.new_v IS DISTINCT FROM l.row_data -> k.key END
)
SELECT DISTINCT ON (m.object_id)
       m.object_id, m.audit_id, m.kind, m.column_name, m.expected::text, m.actual::text, m.detail
FROM mismatches m
WHERE %(full)s OR %(since)s::timestamptz IS NULL OR m.changed_at IS NULL OR m.changed_at >= %(since)s
ORDER BY m.object_id, m.audit_id,
         CASE m.kind WHEN 'lifecycle' THEN 0 WHEN 'chain' THEN 1 ELSE 2 END,
         m.column_name
LIMIT %(limit)s
"""

# Repeated where the audit table is read, so a batch reads only its objects.
_SCOPE_OBJECT_IDS = """
      AND a.object_id = ANY(%(object_ids)s::text[])"""

# The diff only: one kv row per diff entry.
_DIFF_BODY = """ev AS (
    SELECT a.id, a.object_id, a.operation, a.changed_at, a.diff,
           LAG(a.operation) OVER (PARTITION BY a.object_id ORDER BY a.id) AS prev_op,
           max(a.id) OVER (PARTITION BY a.object_id) AS last_id
    FROM {audit} a JOIN scope sc ON sc.object_id = a.object_id
    WHERE a.table_name = %(table)s{scope}
),
entries AS (
    -- A paired entry is an object with exactly the keys old and new; anything
    -- else is a pre-0.4 values-only entry that carries just the new value.
    SELECT e.id, e.object_id, e.changed_at, d.key, d.value,
           CASE WHEN jsonb_typeof(d.value) = 'object'
                THEN d.value ?& array['old', 'new']
                     AND (SELECT count(*) FROM jsonb_object_keys(d.value)) = 2
                ELSE false END AS paired
    FROM ev e
    CROSS JOIN LATERAL jsonb_each(e.diff) AS d(key, value)
),
kv AS (
    SELECT n.id, n.object_id, n.changed_at, n.key, n.paired,
           CASE WHEN n.paired THEN n.value -> 'old' END AS old_v,
           CASE WHEN n.paired THEN n.value -> 'new' ELSE n.value END AS new_v,
           LAG(CASE WHEN n.paired THEN n.value -> 'new' ELSE n.value END) OVER w AS prev_new,
           LAG(n.id) OVER w AS prev_id,
           row_number() OVER (PARTITION BY n.object_id, n.key ORDER BY n.id DESC) AS rn_desc,
           COALESCE(c.is_tstz, false) AS is_tstz
    FROM entries n
    LEFT JOIN cols c ON c.key = n.key
    WINDOW w AS (PARTITION BY n.object_id, n.key ORDER BY n.id)
),
"""

_DIFF_CHAIN_GUARD = "k.paired AND k.prev_id IS NOT NULL"

# The diff and the snapshots. Keys come from both: a full-mode row carries
# every column, so it feeds the chain of columns its diff does not name. A
# paired diff entry is an object with exactly the keys old and new; any other
# diff entry is a pre-0.4 values-only entry with just the new value. Without
# a diff entry the snapshot value is used. -> yields SQL NULL only for a
# missing key, a JSON null stays jsonb 'null'.
#
# Snapshots before 0.6.2 hold excluded columns too: they are read through the
# current field filter, as reconstruction does (_audit_filter_row; the
# exclude case is inlined because a function call per snapshot is slow).
#
# n_all is the new value of every key an event touches. An event whose
# snapshots, outside its diff keys, are unchanged and contained in n_all of
# the event before, and that is followed by another row with both snapshots
# (the usual full-mode UPDATE inside a full-mode run), adds only its diff
# keys. Each skipped key carries the value the chain already holds, so no
# check changes. A key the event before touches takes prev_new / prev_id from
# it directly, and the last row of a run is never skipped, so the reported
# events stay those of the unskipped chain.
_SNAPSHOT_BODY = """filt AS MATERIALIZED (
    SELECT spec.s, spec.s ->> 'fields_kind' AS kind,
           ARRAY(SELECT jsonb_array_elements_text(spec.s -> 'fields')) AS fields
    FROM spec
),
snap AS (
    SELECT a.id, a.object_id, a.operation, a.changed_at, d.diff, s.old_data, s.new_data,
           d.keys AS diff_keys,
           COALESCE(s.new_data, '{{}}'::jsonb) || COALESCE((
               SELECT jsonb_object_agg(x.key, CASE
                   WHEN jsonb_typeof(x.value) = 'object'
                        AND x.value ?& array['old', 'new']
                        AND (SELECT count(*) FROM jsonb_object_keys(x.value)) = 2
                   THEN x.value -> 'new' ELSE x.value END)
               FROM jsonb_each(d.diff) AS x(key, value)
           ), '{{}}'::jsonb) AS n_all
    FROM {audit} a JOIN scope sc ON sc.object_id = a.object_id
    CROSS JOIN filt
    CROSS JOIN LATERAL (
        SELECT CASE filt.kind WHEN 'exclude' THEN a.old_data - filt.fields
                              WHEN 'only' THEN _audit_filter_row(a.old_data, filt.s)
                              ELSE a.old_data END AS old_data,
               CASE filt.kind WHEN 'exclude' THEN a.new_data - filt.fields
                              WHEN 'only' THEN _audit_filter_row(a.new_data, filt.s)
                              ELSE a.new_data END AS new_data
    ) AS f
    -- Checked on the stored value: the "only" filter turns NULL into {{}}.
    CROSS JOIN LATERAL (
        SELECT CASE WHEN jsonb_typeof(a.old_data) = 'object' THEN f.old_data END AS old_data,
               CASE WHEN jsonb_typeof(a.new_data) = 'object' THEN f.new_data END AS new_data
    ) AS s
    CROSS JOIN LATERAL (
        SELECT CASE WHEN jsonb_typeof(a.diff) = 'object' THEN a.diff ELSE '{{}}'::jsonb END AS diff,
               ARRAY(SELECT jsonb_object_keys(a.diff) WHERE jsonb_typeof(a.diff) = 'object')
                   AS keys
    ) AS d
    WHERE a.table_name = %(table)s{scope}
),
ev AS (
    SELECT p.id, p.object_id, p.operation, p.changed_at, p.diff, p.old_data, p.new_data,
           p.diff_keys,
           LAG(p.n_all) OVER w AS prev_n,
           LAG(p.id) OVER w AS prev_evt,
           LEAD(p.old_data IS NOT NULL AND p.new_data IS NOT NULL) OVER w AS next_full,
           LAG(p.operation) OVER w AS prev_op,
           max(p.id) OVER (PARTITION BY p.object_id) AS last_id
    FROM snap p
    WINDOW w AS (PARTITION BY p.object_id ORDER BY p.id)
),
entries AS MATERIALIZED (
    SELECT e.id, e.object_id, e.changed_at, k.key,
           CASE WHEN jsonb_typeof(e.diff -> k.key) = 'object'
                THEN (e.diff -> k.key) ?& array['old', 'new']
                     AND (SELECT count(*) FROM jsonb_object_keys(e.diff -> k.key)) = 2
                ELSE false END AS paired,
           e.diff -> k.key AS d_value,
           e.old_data -> k.key AS s_old,
           e.new_data -> k.key AS s_new,
           e.prev_evt,
           e.prev_n -> k.key AS prev_n_k
    FROM ev e
    CROSS JOIN LATERAL (
        SELECT COALESCE(
            e.prev_n IS NOT NULL
            AND e.next_full
            AND e.old_data - e.diff_keys = e.new_data - e.diff_keys
            AND e.prev_n || e.old_data = e.prev_n,
            false
        ) AS skip
        OFFSET 0
    ) AS sk
    CROSS JOIN LATERAL jsonb_object_keys(
        CASE WHEN sk.skip THEN e.diff
             ELSE COALESCE(e.old_data, '{{}}'::jsonb) || COALESCE(e.new_data, '{{}}'::jsonb)
                  || e.diff
        END
    ) AS k(key)
),
kv AS (
    SELECT n.id, n.object_id, n.changed_at, n.key,
           x.old_v, x.new_v,
           CASE WHEN n.prev_n_k IS NOT NULL THEN n.prev_n_k ELSE LAG(x.new_v) OVER w END
               AS prev_new,
           CASE WHEN n.prev_n_k IS NOT NULL THEN n.prev_evt ELSE LAG(n.id) OVER w END
               AS prev_id,
           row_number() OVER (PARTITION BY n.object_id, n.key ORDER BY n.id DESC) AS rn_desc,
           COALESCE(c.is_tstz, false) AS is_tstz
    FROM entries n
    CROSS JOIN LATERAL (
        SELECT CASE WHEN n.paired THEN n.d_value -> 'old' ELSE n.s_old END AS old_v,
               CASE WHEN n.paired THEN n.d_value -> 'new'
                    ELSE COALESCE(n.d_value, n.s_new) END AS new_v
    ) AS x
    LEFT JOIN cols c ON c.key = n.key
    WINDOW w AS (PARTITION BY n.object_id, n.key ORDER BY n.id)
),
"""

_SNAPSHOT_CHAIN_GUARD = "k.old_v IS NOT NULL AND k.prev_id IS NOT NULL AND k.prev_new IS NOT NULL"


def _verify_query(audit_table: str, *, snapshots: bool, scoped: bool) -> sql.Composed:
    audit = sql.Identifier(audit_table)
    scope = sql.SQL(_SCOPE_OBJECT_IDS if scoped else "")
    body = _SNAPSHOT_BODY if snapshots else _DIFF_BODY
    return sql.SQL(_VERIFY_SQL).format(
        audit=audit,
        scope=scope,
        body=sql.SQL(body).format(audit=audit, scope=scope),
        chain_guard=sql.SQL(_SNAPSHOT_CHAIN_GUARD if snapshots else _DIFF_CHAIN_GUARD),
    )


def _decode(value: str | None) -> Any:
    # The query returns expected/actual as jsonb text, so decoding is the same
    # on every driver (psycopg would otherwise hand a JSON string over as a bare str).
    return None if value is None else json.loads(value)


def parse_since(value: str, *, option: str = "--since") -> datetime:
    """Turn ``'1 day'`` / ``'2 weeks'`` or an ISO-8601 timestamp into a UTC datetime.

    Intervals use the ``purge --older-than`` format and are subtracted from
    now; a naive ISO timestamp is taken as UTC. ``option`` names the option
    in the error message.
    """
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        try:
            return datetime.now(UTC) - _parse_interval(value)
        except ValueError:
            raise ValueError(
                f"invalid {option} value {value!r}: use e.g. '1 day', '2 weeks' "
                "or an ISO-8601 timestamp"
            ) from None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def list_registered_tables(conn: ConnectionProtocol, audit_table: str = "auditlog") -> list[str]:
    """Tracked tables registered for ``audit_table`` in the trigger registry.

    Tables with a ``log_condition`` are left out: their chain has gaps by design.
    """
    validate_identifier(audit_table, "audit_table")
    query = sql.SQL(
        "SELECT DISTINCT table_name FROM {registry} "
        "WHERE spec_fingerprint->>'audit_table' = %s "
        "AND spec_fingerprint->>'log_condition' IS NULL ORDER BY table_name"
    ).format(registry=sql.Identifier(_TRACKING_TABLE))
    with conn.cursor() as cur:
        cur.execute(query, (audit_table,))
        return [row[0] for row in cur.fetchall()]


def verify_diff_chain(
    conn: ConnectionProtocol,
    *,
    table: str,
    since: datetime | None = None,
    object_id: Any = None,
    full: bool = False,
    audit_table: str = "auditlog",
    limit: int | None = None,
    snapshots: bool = False,
    _object_ids: Sequence[str] | None = None,
) -> list[DiffChainMismatch]:
    """Return the first chain inconsistency per object of ``table``.

    Scope: objects with events since ``since`` (windowed), every object
    with events (``full=True``), optionally narrowed to ``object_id``. At
    least one of the three is required. In windowed mode a break older
    than ``since`` is not reported again; mismatches with the live row
    always are, because they describe the current state.

    With ``snapshots=True`` the ``old_data`` / ``new_data`` of full-mode rows
    feed the chain too, read through the current field filter, so columns
    their diff does not name are checked as well. It reads every snapshot in
    scope and costs accordingly; :func:`auditrum.compaction.compact_snapshots`
    uses it before clearing snapshots.
    """
    validate_identifier(table, "table")
    validate_identifier(audit_table, "audit_table")
    if since is None and object_id is None and not full:
        raise ValueError("verify_diff_chain needs since, object_id or full=True")

    query = _verify_query(audit_table, snapshots=snapshots, scoped=_object_ids is not None)
    params: dict[str, Any] = {
        "table": table,
        "since": since,
        "object_id": None if object_id is None else str(object_id),
        "full": full,
        "limit": limit,
    }
    if _object_ids is not None:
        params["object_ids"] = list(_object_ids)
    with conn.cursor() as cur:
        cur.execute("SELECT _audit_tracked_spec(%s)", (table,))
        spec = cur.fetchone()
        if spec is None or spec[0] is None:
            raise ValueError(
                f"table {table!r} has no readable entry in {_TRACKING_TABLE} "
                "(not tracked through TriggerManager, or no SELECT on the registry)"
            )
        # Django's connection hands jsonb over as str; psycopg already decodes it.
        spec_data = json.loads(spec[0]) if isinstance(spec[0], str) else spec[0]
        if spec_data.get("log_condition") is not None:
            raise ValueError(
                f"table {table!r} uses log_condition; its audit chain has gaps by design "
                "and cannot be verified"
            )
        cur.execute(query, params)
        rows = cur.fetchall()

    return [
        DiffChainMismatch(
            table=table,
            object_id=str(oid),
            audit_id=audit_id,
            kind=kind,
            column=column,
            expected=_decode(expected),
            actual=_decode(actual),
            detail=detail,
        )
        for oid, audit_id, kind, column, expected, actual, detail in rows
    ]
