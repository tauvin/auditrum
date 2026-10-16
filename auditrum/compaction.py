"""Clear the snapshots of audit rows that belong to diff-mode tables.

Switching a table to ``storage="diff"`` affects new events only; rows
written before keep their ``old_data`` / ``new_data`` snapshots, which are
most of the audit log's size. :func:`compact_snapshots` clears them in
batches of objects. Reconstruction decides per row, so a cleared row reads
like a diff-only one.

Each batch is verified first, with snapshot values in the diff chain, and
two kinds of objects keep their snapshots: objects with a chain finding
(while a snapshot exists, reconstruction short-cuts through it and stays
right across a trigger bypass), and objects with a snapshot that records a
change of a column their diff does not (rows written before 0.6.2 under a
narrower field filter; reconstruction from the diff alone would lose it).
Rows covered by the hash chain are never touched, because the hash
includes the snapshots.

Each batch is its own statements; batches commit separately only on an
autocommit connection. The freed space is reused by new rows after
``VACUUM``; the files on disk do not shrink.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime

from psycopg import sql

from auditrum.diff_chain import DiffChainMismatch, _decode, verify_diff_chain
from auditrum.executor import ConnectionProtocol
from auditrum.tracking.spec import validate_identifier

__all__ = ["CompactionResult", "compact_snapshots"]


@dataclass(frozen=True)
class CompactionResult:
    """What :func:`compact_snapshots` did (or would do, on a dry run) for one table."""

    table: str
    compacted: int
    skipped_hashed: int
    excluded: tuple[DiffChainMismatch, ...]
    dry_run: bool


# A paired entry is an object with exactly the keys old and new (the rule
# diff_chain uses); a row with any other entry predates 0.4 and needs its
# snapshots. {audit} is the audit table identifier, every value is bound.
_ELIGIBLE = """
a.table_name = %(table)s
AND a.object_id = ANY(%(object_ids)s::text[])
AND (a.old_data IS NOT NULL OR a.new_data IS NOT NULL)
AND a.diff IS NOT NULL
AND NOT EXISTS (
    SELECT 1 FROM jsonb_each(a.diff) AS d(key, value)
    WHERE jsonb_typeof(d.value) <> 'object'
       OR NOT d.value ?& array['old', 'new']
       OR (SELECT count(*) FROM jsonb_object_keys(d.value)) <> 2
)
AND (%(before)s::timestamptz IS NULL OR a.changed_at < %(before)s::timestamptz)
AND NOT (a.object_id = ANY(%(excluded)s::text[]))
"""

_COUNT_SQL = (
    "SELECT count(*) FILTER (WHERE {hashed} IS NULL), "
    "count(*) FILTER (WHERE {hashed} IS NOT NULL) "
    "FROM {audit} a WHERE " + _ELIGIBLE
)

_UPDATE_SQL = (
    "WITH updated AS (UPDATE {audit} a SET old_data = NULL, new_data = NULL WHERE "
    + _ELIGIBLE
    + " AND {hashed} IS NULL RETURNING 1) "
    "SELECT (SELECT count(*) FROM updated), "
    "(SELECT count(*) FROM {audit} a WHERE " + _ELIGIBLE + " AND {hashed} IS NOT NULL)"
)

# Rows with a NULL object_id (very old triggers, tables without an id
# column) are never eligible; paging past them would also restart the scan.
_FIRST_OBJECTS_SQL = (
    "SELECT DISTINCT a.object_id FROM {audit} a WHERE a.table_name = %(table)s "
    "AND a.object_id IS NOT NULL ORDER BY a.object_id LIMIT %(limit)s"
)

_NEXT_OBJECTS_SQL = (
    "SELECT DISTINCT a.object_id FROM {audit} a WHERE a.table_name = %(table)s "
    "AND a.object_id IS NOT NULL AND a.object_id > %(after)s ORDER BY a.object_id LIMIT %(limit)s"
)

# Rows whose snapshots, read through the current field filter, record a
# change the diff does not: an UPDATE with a column outside the diff that
# differs between old and new, a DELETE with a column outside the diff. The
# first such row and column per object. The filter is the one diff_chain
# uses (_audit_filter_row, with the exclude case inlined). An UPDATE whose
# new snapshot is its old one with the diff's new values applied has no such
# column, which rules out most rows before the per-column check.
_SNAPSHOT_ONLY_SQL = """
WITH filt AS MATERIALIZED (
    SELECT s, s ->> 'fields_kind' AS kind,
           ARRAY(SELECT jsonb_array_elements_text(s -> 'fields')) AS fields
    FROM (SELECT _audit_tracked_spec(%(table)s) AS s) AS spec
),
snap AS (
    SELECT a.id, a.object_id, a.operation,
           CASE filt.kind WHEN 'exclude' THEN a.old_data - filt.fields
                          WHEN 'only' THEN _audit_filter_row(a.old_data, filt.s)
                          ELSE a.old_data END AS old_data,
           CASE filt.kind WHEN 'exclude' THEN a.new_data - filt.fields
                          WHEN 'only' THEN _audit_filter_row(a.new_data, filt.s)
                          ELSE a.new_data END AS new_data,
           CASE WHEN jsonb_typeof(a.diff) = 'object' THEN a.diff ELSE '{{}}'::jsonb END AS diff
    FROM {audit} a
    CROSS JOIN filt
    WHERE a.table_name = %(table)s
      AND a.object_id = ANY(%(object_ids)s::text[])
      AND a.operation IN ('UPDATE', 'DELETE')
      AND (a.old_data IS NOT NULL OR a.new_data IS NOT NULL)
    OFFSET 0
),
rows AS (
    SELECT p.id, p.object_id, p.operation, p.diff,
           CASE WHEN jsonb_typeof(p.old_data) = 'object' THEN p.old_data
                ELSE '{{}}'::jsonb END AS old_data,
           CASE WHEN jsonb_typeof(p.new_data) = 'object' THEN p.new_data
                ELSE '{{}}'::jsonb END AS new_data
    FROM snap p
    OFFSET 0
)
SELECT DISTINCT ON (r.object_id)
       r.object_id, r.id, c.key, c.old_v::text, c.new_v::text
FROM rows r
CROSS JOIN LATERAL (
    SELECT k.key, r.old_data -> k.key AS old_v, r.new_data -> k.key AS new_v
    FROM jsonb_object_keys(
        CASE WHEN r.operation = 'DELETE' THEN r.old_data ELSE r.old_data || r.new_data END
    ) AS k(key)
    WHERE NOT (r.diff ? k.key)
      AND (r.operation = 'DELETE'
           OR (r.old_data -> k.key) IS DISTINCT FROM (r.new_data -> k.key))
    ORDER BY k.key
    LIMIT 1
) AS c
WHERE r.operation = 'DELETE'
   OR r.old_data || COALESCE((
          SELECT jsonb_object_agg(x.key, CASE
              WHEN jsonb_typeof(x.value) = 'object' AND x.value ? 'new'
              THEN x.value -> 'new' ELSE x.value END)
          FROM jsonb_each(r.diff) AS x(key, value)
      ), '{{}}'::jsonb) <> r.new_data
ORDER BY r.object_id, r.id
"""


def _has_row_hash(cur, audit_table: str) -> bool:
    cur.execute(
        "SELECT 1 FROM pg_attribute WHERE attrelid = quote_ident(%s)::regclass "
        "AND attname = 'row_hash' AND NOT attisdropped",
        (audit_table,),
    )
    return cur.fetchone() is not None


def _snapshot_only_changes(
    cur, audit: sql.Identifier, table: str, object_ids: list[str]
) -> list[DiffChainMismatch]:
    cur.execute(
        sql.SQL(_SNAPSHOT_ONLY_SQL).format(audit=audit),
        {"table": table, "object_ids": object_ids},
    )
    return [
        DiffChainMismatch(
            table=table,
            object_id=str(oid),
            audit_id=audit_id,
            kind="chain",
            column=key,
            expected=_decode(old_v),
            actual=_decode(new_v),
            detail=(
                f"snapshot records a change of {key} that the diff does not "
                "(written before 0.6.2 under a narrower field filter?)"
            ),
        )
        for oid, audit_id, key, old_v, new_v in cur.fetchall()
    ]


def compact_snapshots(
    conn: ConnectionProtocol,
    *,
    table: str,
    before: datetime | None = None,
    batch_size: int = 1000,
    dry_run: bool = False,
    audit_table: str = "auditlog",
) -> CompactionResult:
    """Clear ``old_data`` / ``new_data`` of ``table``'s audit rows.

    Only rows whose diff is fully paired, written before ``before`` (when
    given) and not covered by the hash chain are touched, and only of
    objects that pass the check described in the module docstring. The
    table is processed ``batch_size`` objects at a time; each batch reads
    the full history of its objects. The table must be registered with
    ``storage="diff"``.
    """
    validate_identifier(table, "table")
    validate_identifier(audit_table, "audit_table")
    if batch_size < 1:
        raise ValueError("batch_size must be at least 1")

    with conn.cursor() as cur:
        cur.execute("SELECT _audit_tracked_spec(%s)", (table,))
        row = cur.fetchone()
    spec = None if row is None else row[0]
    # Django's connection hands jsonb over as str; psycopg already decodes it.
    if isinstance(spec, str):
        spec = json.loads(spec)
    if spec is None:
        raise ValueError(f"table {table!r} has no readable entry in the trigger registry")
    if spec.get("storage") != "diff":
        raise ValueError(
            f"table {table!r} is not in diff storage mode; switch it to storage='diff' first"
        )

    audit = sql.Identifier(audit_table)
    excluded: list[DiffChainMismatch] = []
    compacted = skipped_hashed = 0
    with conn.cursor() as cur:
        hashed = sql.SQL("a.row_hash") if _has_row_hash(cur, audit_table) else sql.SQL("NULL")
        first_objects = sql.SQL(_FIRST_OBJECTS_SQL).format(audit=audit)
        next_objects = sql.SQL(_NEXT_OBJECTS_SQL).format(audit=audit)
        apply = sql.SQL(_COUNT_SQL if dry_run else _UPDATE_SQL).format(audit=audit, hashed=hashed)
        after = None
        while True:
            if after is None:
                cur.execute(first_objects, {"table": table, "limit": batch_size})
            else:
                cur.execute(next_objects, {"table": table, "after": after, "limit": batch_size})
            object_ids = [row[0] for row in cur.fetchall()]
            if not object_ids:
                break
            after = object_ids[-1]

            findings = verify_diff_chain(
                conn,
                table=table,
                full=True,
                snapshots=True,
                audit_table=audit_table,
                _object_ids=object_ids,
            )
            found = {f.object_id for f in findings}
            findings += [
                m
                for m in _snapshot_only_changes(cur, audit, table, object_ids)
                if m.object_id not in found
            ]
            excluded += findings
            params = {
                "table": table,
                "object_ids": object_ids,
                "before": before,
                "excluded": sorted({f.object_id for f in findings}),
            }
            cur.execute(apply, params)
            cleared, hashed_rows = cur.fetchone()
            compacted += cleared
            skipped_hashed += hashed_rows
    excluded.sort(key=lambda f: f.object_id)
    return CompactionResult(table, compacted, skipped_hashed, tuple(excluded), dry_run)
