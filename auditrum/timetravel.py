"""Time-travel queries over the audit log.

Reconstruct what a row (or a whole table) looked like at any past
timestamp, without any PostgreSQL extensions. Backed by the
``_audit_reconstruct_row`` / ``_audit_reconstruct_table`` PL/SQL
functions (see :func:`auditrum.schema.generate_audit_reconstruct_sql`)
which walk the composite ``(table_name, object_id, changed_at DESC)``
index for O(log n + 1) lookups per row.

Example usage::

    from datetime import datetime, UTC
    from auditrum.timetravel import reconstruct_row, reconstruct_table

    row = reconstruct_row(
        conn,
        table="users",
        object_id="42",
        at=datetime(2024, 6, 1, tzinfo=UTC),
    )
    if row is None:
        print("row didn't exist at that time")
    else:
        print(row["email"])

    for object_id, row_data in reconstruct_table(
        conn, table="orders", at=datetime(2024, 1, 1, tzinfo=UTC)
    ):
        print(object_id, row_data["status"])

The Django integration exposes the same functionality through the
:class:`AuditLogQuerySet` / :class:`AuditedModelMixin` helpers in phase
9.3.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from psycopg import sql as pg_sql

from auditrum.tracking.spec import validate_identifier


@dataclass(frozen=True)
class HistoricalRow:
    """A row's data as it existed at a specific point in time.

    Framework-agnostic value object. Supports dict-style (``row["email"]``)
    and attribute-style (``row.email``) access to the underlying data, plus
    a :meth:`to_model` helper that instantiates a current Django model
    from historical data while filtering out fields that no longer exist.
    """

    table: str
    object_id: str
    at: datetime
    data: dict[str, Any] = field(default_factory=dict)

    def __getitem__(self, key: str) -> Any:
        return self.data[key]

    def __contains__(self, key: str) -> bool:
        return key in self.data

    def get(self, key: str, default: Any = None) -> Any:
        return self.data.get(key, default)

    def __getattr__(self, name: str) -> Any:
        # Only called when normal attribute lookup fails, so dataclass fields
        # (table, object_id, at, data) still work normally.
        data = object.__getattribute__(self, "data")
        if name in data:
            return data[name]
        raise AttributeError(name)

    def to_model(self, model_cls: Any) -> Any:
        """Instantiate an unsaved model from historical data.

        Only fields that still exist on ``model_cls`` are copied. Extra
        historical fields (columns dropped from the model since the
        snapshot) are preserved in :attr:`data` for inspection but
        silently excluded from the constructed instance.
        """
        try:
            # Django Model path
            valid_names = {
                f.name for f in model_cls._meta.get_fields() if getattr(f, "concrete", False)
            }
        except AttributeError:
            # Fall back: assume it's a plain class that takes **kwargs
            return model_cls(**self.data)
        return model_cls(**{k: v for k, v in self.data.items() if k in valid_names})


def reconstruct_row(
    conn,
    *,
    table: str,
    object_id: Any,
    at: datetime,
    audit_table: str = "auditlog",
) -> dict[str, Any] | None:
    """Return the full row as it existed at ``at``, or ``None``.

    Calls into the ``_audit_reconstruct_row`` PL/SQL helper installed by
    the initial migration. ``None`` is returned when the row did not
    exist (no INSERT yet, or the most recent event before ``at`` was a
    DELETE).
    """
    validate_identifier(audit_table, "audit_table")
    with conn.cursor() as cur:
        cur.execute(
            "SELECT _audit_reconstruct_row(%s, %s, %s)",
            (table, str(object_id), at),
        )
        row = cur.fetchone()
    if row is None:
        return None
    value = row[0]
    if value is None:
        return None
    if isinstance(value, str):
        return json.loads(value)
    return value


def reconstruct_table(
    conn,
    *,
    table: str,
    at: datetime,
    audit_table: str = "auditlog",
    stream: bool = False,
    batch_size: int = 1000,
) -> Iterator[tuple[str, dict[str, Any]]]:
    """Iterate ``(object_id, row_data)`` for every row alive in ``table`` at ``at``.

    Skips rows that were either never inserted by that time or were
    DELETE'd before it.

    Two modes:

    * ``stream=False`` (default) — fetches all surviving rows in one
      ``cur.fetchall()`` call. Simple, fine for tables with up to a few
      tens of thousands of distinct ``object_id``s.
    * ``stream=True`` — opens a server-side **named cursor** with
      ``itersize=batch_size`` so rows arrive in batches without
      materialising the whole result into Python memory. Use this for
      large tables (millions of distinct ids) where the default mode
      would OOM. Requires that the connection supports named cursors
      (``psycopg`` 3 does; some pooled wrappers do not — fall back to
      ``stream=False`` if you see ``ProgrammingError`` or similar).
    """
    validate_identifier(audit_table, "audit_table")
    sql = "SELECT object_id, row_data FROM _audit_reconstruct_table(%s, %s)"

    if stream:
        # Named server-side cursor — psycopg 3 supports it via the
        # ``name`` argument. Rows are fetched in ``itersize`` batches.
        cursor_name = f"auditrum_tt_{abs(hash((table, at.isoformat()))) & 0xFFFF:04x}"
        with conn.cursor(name=cursor_name) as cur:
            cur.itersize = int(batch_size)
            cur.execute(sql, (table, at))
            for object_id, row_data in cur:
                if isinstance(row_data, str):
                    row_data = json.loads(row_data)
                yield object_id, row_data
        return

    with conn.cursor() as cur:
        cur.execute(sql, (table, at))
        for object_id, row_data in cur.fetchall():
            if isinstance(row_data, str):
                row_data = json.loads(row_data)
            yield object_id, row_data


def reconstruct_field_history(
    conn,
    *,
    table: str,
    object_id: Any,
    field: str,
    audit_table: str = "auditlog",
) -> list[tuple[datetime, Any]]:
    """Return ``(changed_at, value)`` pairs for one field over time.

    Only events that actually changed the field are included. The first
    entry is typically the INSERT snapshot; subsequent entries are UPDATEs
    that touched the field. A final ``(timestamp, None)`` entry appears if
    the row was DELETE'd.
    """
    validate_identifier(audit_table, "audit_table")
    validate_identifier(field, "field")

    query = pg_sql.SQL(
        "SELECT changed_at, operation, old_data, new_data, diff "
        "FROM {} "
        "WHERE table_name = %s AND object_id = %s "
        "ORDER BY changed_at ASC, id ASC"
    ).format(pg_sql.Identifier(audit_table))

    history: list[tuple[datetime, Any]] = []
    with conn.cursor() as cur:
        cur.execute(query, (table, str(object_id)))
        rows = cur.fetchall()

    for changed_at, operation, old_data, new_data, diff in rows:
        if isinstance(old_data, str):
            old_data = json.loads(old_data)
        if isinstance(new_data, str):
            new_data = json.loads(new_data)
        if isinstance(diff, str):
            diff = json.loads(diff)

        if operation == "INSERT":
            if new_data and field in new_data:
                history.append((changed_at, new_data[field]))
        elif operation == "UPDATE":
            if diff and field in diff and new_data:
                history.append((changed_at, new_data.get(field)))
        elif operation == "DELETE":
            history.append((changed_at, None))

    return history
