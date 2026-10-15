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
      AND (%(object_id)s::text IS NULL OR a.object_id = %(object_id)s)
),
live AS (
    SELECT sc.object_id, _audit_live_row(%(table)s, sc.object_id, spec.s) AS row_data
    FROM scope sc, spec
),
ev AS (
    SELECT a.id, a.object_id, a.operation, a.changed_at, a.diff,
           LAG(a.operation) OVER (PARTITION BY a.object_id ORDER BY a.id) AS prev_op,
           max(a.id) OVER (PARTITION BY a.object_id) AS last_id
    FROM {audit} a JOIN scope sc ON sc.object_id = a.object_id
    WHERE a.table_name = %(table)s
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
mismatches AS (
    -- chain: old[k] differs from new[k] of the previous event touching k
    SELECT k.object_id, k.id AS audit_id, 'chain' AS kind, k.key AS column_name,
           k.prev_new AS expected, k.old_v AS actual,
           format('old value of %%s differs from event %%s', k.key, k.prev_id) AS detail,
           k.changed_at
    FROM kv k
    WHERE k.paired AND k.prev_id IS NOT NULL
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


def _decode(value: str | None) -> Any:
    # The query returns expected/actual as jsonb text, so decoding is the same
    # on every driver (psycopg would otherwise hand a JSON string over as a bare str).
    return None if value is None else json.loads(value)


def parse_since(value: str) -> datetime:
    """Turn ``'1 day'`` / ``'2 weeks'`` or an ISO-8601 timestamp into a UTC datetime.

    Intervals use the ``purge --older-than`` format and are subtracted from
    now; a naive ISO timestamp is taken as UTC.
    """
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        try:
            return datetime.now(UTC) - _parse_interval(value)
        except ValueError:
            raise ValueError(
                f"invalid --since value {value!r}: use e.g. '1 day', '2 weeks' "
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
) -> list[DiffChainMismatch]:
    """Return the first chain inconsistency per object of ``table``.

    Scope: objects with events since ``since`` (windowed), every object
    with events (``full=True``), optionally narrowed to ``object_id``. At
    least one of the three is required. In windowed mode a break older
    than ``since`` is not reported again; mismatches with the live row
    always are, because they describe the current state.
    """
    validate_identifier(table, "table")
    validate_identifier(audit_table, "audit_table")
    if since is None and object_id is None and not full:
        raise ValueError("verify_diff_chain needs since, object_id or full=True")

    query = sql.SQL(_VERIFY_SQL).format(audit=sql.Identifier(audit_table))
    params = {
        "table": table,
        "since": since,
        "object_id": None if object_id is None else str(object_id),
        "full": full,
        "limit": limit,
    }
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
