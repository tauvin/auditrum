"""Clear the snapshots of audit rows that belong to diff-mode tables.

Switching a table to ``storage="diff"`` affects new events only; rows
written before keep their ``old_data`` / ``new_data`` snapshots, which are
most of the audit log's size. :func:`compact_snapshots` clears them in
batches. Reconstruction decides per row, so a cleared row reads like a
diff-only one.

Objects whose diff chain is broken keep their snapshots: while a snapshot
exists, reconstruction short-cuts through it and stays right across a
trigger bypass. Rows covered by the hash chain are never touched, because
the hash includes the snapshots.

Each batch is one ``UPDATE``; batches commit separately only on an
autocommit connection. The freed space is reused by new rows after
``VACUUM``; the files on disk do not shrink.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime

from psycopg import sql

from auditrum.diff_chain import DiffChainMismatch, verify_diff_chain
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

_BATCH_SQL = (
    "WITH batch AS (SELECT a.id FROM {audit} a WHERE "
    + _ELIGIBLE
    + " AND {hashed} IS NULL AND a.id > %(after)s ORDER BY a.id LIMIT %(batch_size)s), "
    "updated AS (UPDATE {audit} a SET old_data = NULL, new_data = NULL "
    "FROM batch b WHERE a.id = b.id AND a.table_name = %(table)s RETURNING a.id) "
    "SELECT count(*), max(id) FROM updated"
)


def _has_row_hash(cur, audit_table: str) -> bool:
    cur.execute(
        "SELECT 1 FROM pg_attribute WHERE attrelid = %s::regclass "
        "AND attname = 'row_hash' AND NOT attisdropped",
        (audit_table,),
    )
    return cur.fetchone() is not None


def compact_snapshots(
    conn: ConnectionProtocol,
    *,
    table: str,
    before: datetime | None = None,
    batch_size: int = 10_000,
    dry_run: bool = False,
    audit_table: str = "auditlog",
) -> CompactionResult:
    """Clear ``old_data`` / ``new_data`` of ``table``'s audit rows.

    Only rows whose diff is fully paired, of objects with no diff-chain
    finding, written before ``before`` (when given) and not covered by the
    hash chain are touched. The table must be registered with
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

    excluded = tuple(verify_diff_chain(conn, table=table, full=True, audit_table=audit_table))
    params = {
        "table": table,
        "before": before,
        "excluded": sorted({f.object_id for f in excluded}),
    }
    with conn.cursor() as cur:
        hashed = sql.SQL("a.row_hash") if _has_row_hash(cur, audit_table) else sql.SQL("NULL")
        audit = sql.Identifier(audit_table)
        cur.execute(sql.SQL(_COUNT_SQL).format(audit=audit, hashed=hashed), params)
        eligible, skipped_hashed = cur.fetchone()
        if dry_run:
            return CompactionResult(table, eligible, skipped_hashed, excluded, True)

        batch = sql.SQL(_BATCH_SQL).format(audit=audit, hashed=hashed)
        compacted, after = 0, 0
        while True:
            cur.execute(batch, {**params, "after": after, "batch_size": batch_size})
            count, last_id = cur.fetchone()
            if not count:
                break
            compacted += count
            after = last_id
    return CompactionResult(table, compacted, skipped_hashed, excluded, False)
