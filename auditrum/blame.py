"""Git-style blame for audit log rows.

``auditrum blame <table> <object_id>`` prints the full history of a single
row; ``auditrum blame <table> <id> <field>`` narrows to events that
actually changed the given field.

The implementation is split into two pure functions so it's testable
without a terminal:

* :func:`fetch_blame` — issues a parametrised SQL query against a live
  connection and returns a list of :class:`BlameEntry`
* :func:`format_blame` — renders those entries as plain text, rich text,
  or JSON

The CLI just wires these together.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal

from psycopg import sql as pg_sql

from auditrum.tracking.spec import validate_identifier


@dataclass(frozen=True)
class BlameEntry:
    """One event in a row's history, with values already narrowed if a field filter was applied."""

    changed_at: datetime
    operation: str
    user_id: int | None
    context_id: str | None
    context_metadata: dict[str, Any] | None
    old_value: Any  # JSON value (dict for full-row, scalar for field-narrow). None for INSERT.
    new_value: Any  # None for DELETE.
    change_reason: str | None
    diff: dict[str, Any] | None


def fetch_blame(
    conn,
    *,
    table: str,
    object_id: str,
    field: str | None = None,
    audit_table: str = "auditlog",
    context_table: str = "audit_context",
    limit: int = 200,
) -> list[BlameEntry]:
    """Fetch the audit history of a single row.

    Uses the ``(table_name, object_id, changed_at DESC)`` composite index
    for a fast range scan. When ``field`` is given, filters in-Python
    after the SQL pull so the index is still fully used — the field
    narrow is small-cardinality relative to the row history.
    """
    validate_identifier(audit_table, "audit_table")
    validate_identifier(context_table, "context_table")

    query = pg_sql.SQL(
        "SELECT a.changed_at, a.operation, a.user_id, a.context_id, "
        "a.old_data, a.new_data, a.diff, c.metadata "
        "FROM {audit_table} a "
        "LEFT JOIN {context_table} c ON c.id = a.context_id "
        "WHERE a.table_name = %s AND a.object_id = %s "
        "ORDER BY a.changed_at ASC, a.id ASC "
        "LIMIT %s"
    ).format(
        audit_table=pg_sql.Identifier(audit_table),
        context_table=pg_sql.Identifier(context_table),
    )

    with conn.cursor() as cur:
        cur.execute(query, (table, str(object_id), int(limit)))
        rows = cur.fetchall()

    entries: list[BlameEntry] = []
    for changed_at, operation, user_id, context_id, old_data, new_data, diff, metadata in rows:
        if isinstance(metadata, str):
            metadata = json.loads(metadata)
        if isinstance(diff, str):
            diff = json.loads(diff)
        if isinstance(old_data, str):
            old_data = json.loads(old_data)
        if isinstance(new_data, str):
            new_data = json.loads(new_data)

        change_reason = None
        if metadata and isinstance(metadata, dict):
            change_reason = metadata.get("change_reason")

        if field is not None:
            # Narrow to events that actually touched this field
            if operation == "UPDATE":
                if not diff or field not in diff:
                    continue
                old_val = (old_data or {}).get(field)
                new_val = (new_data or {}).get(field)
            elif operation == "INSERT":
                if new_data is None or field not in new_data:
                    continue
                old_val = None
                new_val = new_data.get(field)
            elif operation == "DELETE":
                if old_data is None or field not in old_data:
                    continue
                old_val = old_data.get(field)
                new_val = None
            else:
                continue
        else:
            old_val = old_data
            new_val = new_data

        entries.append(
            BlameEntry(
                changed_at=changed_at,
                operation=operation,
                user_id=user_id,
                context_id=str(context_id) if context_id is not None else None,
                context_metadata=metadata if isinstance(metadata, dict) else None,
                old_value=old_val,
                new_value=new_val,
                change_reason=change_reason,
                diff=diff if isinstance(diff, dict) else None,
            )
        )

    return entries


BlameFormat = Literal["text", "rich", "json"]


def format_blame(
    entries: list[BlameEntry],
    *,
    field: str | None = None,
    fmt: BlameFormat = "rich",
    table: str | None = None,
    object_id: str | None = None,
) -> str:
    """Render blame entries as plain text, rich markup, or JSON."""
    if fmt == "json":
        return _format_json(entries)
    return _format_text(
        entries, field=field, rich=(fmt == "rich"), table=table, object_id=object_id
    )


def _format_json(entries: list[BlameEntry]) -> str:
    return json.dumps(
        [
            {
                "changed_at": e.changed_at.isoformat() if e.changed_at else None,
                "operation": e.operation,
                "user_id": e.user_id,
                "context_id": e.context_id,
                "context_metadata": e.context_metadata,
                "old_value": e.old_value,
                "new_value": e.new_value,
                "change_reason": e.change_reason,
                "diff": e.diff,
            }
            for e in entries
        ],
        indent=2,
        default=str,
    )


_OP_COLORS = {
    "INSERT": "green",
    "UPDATE": "yellow",
    "DELETE": "red",
}


def _escape_rich(text: object) -> str:
    """Neutralise rich-markup brackets in user-controlled strings.

    Audit metadata can contain attacker-supplied values (username from
    a compromised request, change_reason from an automated job, etc.).
    Without escaping, a value like ``[red]VICTIM[/red]`` injected into
    a `username` field would render as red text in the operator's
    terminal — useful for spoofing log output during incident response.
    Doubling the brackets makes them literal in rich's parser.
    """
    s = "" if text is None else str(text)
    return s.replace("[", "\\[")


def _format_text(
    entries: list[BlameEntry],
    *,
    field: str | None,
    rich: bool,
    table: str | None,
    object_id: str | None,
) -> str:
    """One-line-per-event render. Git-blame-ish alignment."""

    def _color(text: str, color: str) -> str:
        return f"[{color}]{text}[/{color}]" if rich else text

    def _dim(text: str) -> str:
        return f"[dim]{text}[/dim]" if rich else text

    def _bold(text: str) -> str:
        return f"[bold]{text}[/bold]" if rich else text

    def _safe(text: object) -> str:
        return _escape_rich(text) if rich else ("" if text is None else str(text))

    lines: list[str] = []
    if table is not None and object_id is not None:
        header = f"Audit history for {_safe(table)}:{_safe(object_id)}"
        if field is not None:
            header += f" (field: {_safe(field)})"
        lines.append(_bold(header))
        lines.append(_dim("─" * len(header)))

    if not entries:
        lines.append(_dim("(no events found)"))
        return "\n".join(lines)

    for e in entries:
        ts = e.changed_at.strftime("%Y-%m-%d %H:%M:%S") if e.changed_at else "—"
        op = _color(f"[{e.operation:<6}]", _OP_COLORS.get(e.operation, "white"))
        actor = _render_actor(e, rich=rich)

        if field is not None:
            change = _render_field_change(e, field, rich=rich)
        else:
            change = _render_row_change(e, rich=rich)

        reason = ""
        if e.change_reason:
            reason = "  " + _dim(f'reason="{_safe(e.change_reason)}"')

        ctx = ""
        if e.context_id:
            ctx = "  " + _dim(f"ctx={_safe(e.context_id[:8])}")

        lines.append(f"{op} {_dim(ts)}  {actor:<30}{change}{reason}{ctx}")

    return "\n".join(lines)


def _render_actor(entry: BlameEntry, *, rich: bool = False) -> str:
    """Figure out who did this change — prefer typed user_id, fall back to context metadata.

    Strings sourced from context metadata (``username``, ``source``) are
    user-controlled and must be escaped against rich-markup injection
    when ``rich`` mode is on.
    """
    def _safe(text: object) -> str:
        return _escape_rich(text) if rich else ("" if text is None else str(text))

    if entry.user_id is not None:
        username = None
        if entry.context_metadata:
            username = entry.context_metadata.get("username")
        if username:
            return f"user={entry.user_id} ({_safe(username)})"
        return f"user={entry.user_id}"
    if entry.context_metadata:
        username = entry.context_metadata.get("username")
        source = entry.context_metadata.get("source")
        if username:
            return f"user={_safe(username)}"
        if source:
            return f"source={_safe(source)}"
    return "system"


def _render_field_change(entry: BlameEntry, field: str, *, rich: bool) -> str:
    old = entry.old_value
    new = entry.new_value
    arrow = "[dim]→[/dim]" if rich else "→"
    if entry.operation == "INSERT":
        return f"{arrow} {_repr_value(new)}"
    if entry.operation == "DELETE":
        return f"{_repr_value(old)} {arrow} [red]∅[/red]" if rich else f"{_repr_value(old)} → ∅"
    return f"{_repr_value(old)} {arrow} {_repr_value(new)}"


def _render_row_change(entry: BlameEntry, *, rich: bool) -> str:
    if entry.operation == "INSERT":
        keys = sorted((entry.new_value or {}).keys())
        return f"inserted ({len(keys)} fields)"
    if entry.operation == "DELETE":
        keys = sorted((entry.old_value or {}).keys())
        return f"deleted ({len(keys)} fields)"
    if entry.diff:
        fields = ", ".join(sorted(entry.diff.keys()))
        return f"changed: {fields}"
    return ""


def _repr_value(v: Any) -> str:
    if v is None:
        return "∅"
    if isinstance(v, str):
        if len(v) > 40:
            return json.dumps(v[:37] + "...")
        return json.dumps(v)
    return json.dumps(v, default=str)
