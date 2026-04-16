"""SQLAlchemy integration core — executor, registry, bootstrap, sync."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from typing import TYPE_CHECKING, Any

from auditrum.schema import (
    generate_audit_attach_context_sql,
    generate_audit_context_table_sql,
    generate_audit_current_user_id_sql,
    generate_audit_reconstruct_sql,
    generate_auditlog_partitions_sql,
    generate_auditlog_table_sql,
    generate_jsonb_diff_function_sql,
)
from auditrum.tracking import FieldFilter, SyncReport, TrackSpec, TriggerManager

__all__ = [
    "SQLAlchemyExecutor",
    "bootstrap_schema",
    "clear_registry",
    "registered_specs",
    "sync",
    "track_table",
]

if TYPE_CHECKING:
    from sqlalchemy import Table
    from sqlalchemy.engine import Connection, Engine


# -------------------------------------------------------------------
# Cursor adapter
# -------------------------------------------------------------------


class _SQLAlchemyCursor:
    """Adapts a SQLAlchemy ``Connection`` to the psycopg cursor shape used
    by :class:`TriggerManager`.

    The manager issues positional-argument SQL with ``%s`` placeholders,
    which SQLAlchemy Core can bind via :func:`sqlalchemy.text` + a dict of
    parameters. We translate ``%s`` → ``:p0, :p1, …`` at call time.
    """

    def __init__(self, conn: Connection) -> None:
        self._conn = conn
        self._last_result = None

    def execute(self, sql: str, params: Any = None) -> None:
        from sqlalchemy import text

        if params is None:
            self._last_result = self._conn.execute(text(sql))
            return

        # psycopg uses %s and tuple params; SQLAlchemy core uses :name and dicts
        bind_params: dict[str, Any] = {}
        out_sql = []
        param_idx = 0
        i = 0
        if isinstance(params, dict):
            # Direct pass-through — caller already used :name placeholders
            self._last_result = self._conn.execute(text(sql), params)
            return

        # Translate %s → :p0, :p1, …
        seq = tuple(params)
        while i < len(sql):
            if sql[i : i + 2] == "%s":
                name = f"p{param_idx}"
                out_sql.append(f":{name}")
                bind_params[name] = seq[param_idx]
                param_idx += 1
                i += 2
            else:
                out_sql.append(sql[i])
                i += 1
        self._last_result = self._conn.execute(text("".join(out_sql)), bind_params)

    def fetchone(self) -> Any:
        if self._last_result is None:
            return None
        return self._last_result.fetchone()

    def fetchall(self) -> list:
        if self._last_result is None:
            return []
        return self._last_result.fetchall()

    def __enter__(self) -> _SQLAlchemyCursor:
        return self

    def __exit__(self, *args: Any) -> None:
        self._last_result = None


class SQLAlchemyExecutor:
    """Framework-agnostic executor backed by a SQLAlchemy ``Connection``.

    Feed into :class:`TriggerManager` to drive install / uninstall / sync
    through the same code path as the Django and raw-psycopg integrations.
    """

    def __init__(self, conn: Connection) -> None:
        self._conn = conn

    @contextmanager
    def cursor(self) -> Iterator[_SQLAlchemyCursor]:
        cur = _SQLAlchemyCursor(self._conn)
        try:
            yield cur
        finally:
            cur._last_result = None


# -------------------------------------------------------------------
# Registry
# -------------------------------------------------------------------


_registry: dict[str, TrackSpec] = {}


def _build_filter(
    fields: list[str] | None, exclude: list[str] | None
) -> FieldFilter:
    if fields is not None and exclude is not None:
        raise ValueError("track_table(): cannot pass both `fields` and `exclude`")
    if fields is not None:
        return FieldFilter.only(*fields)
    if exclude is not None:
        return FieldFilter.exclude(*exclude)
    return FieldFilter.all()


def track_table(
    table: Table,
    *,
    fields: list[str] | None = None,
    exclude: list[str] | None = None,
    extra_meta: list[str] | None = None,
    log_condition: str | None = None,
    audit_table: str = "auditlog",
    trigger_name: str | None = None,
) -> TrackSpec:
    """Register an audit trigger for a SQLAlchemy ``Table``.

    Returns the :class:`TrackSpec` for chaining / introspection and
    stores it in the module-level registry that :func:`sync` consumes.
    """
    spec = TrackSpec(
        table=table.name,
        audit_table=audit_table,
        fields=_build_filter(fields, exclude),
        extra_meta_fields=tuple(extra_meta or ()),
        log_condition=log_condition,
        trigger_name=trigger_name,
    )
    _registry[table.name] = spec
    return spec


def registered_specs() -> list[TrackSpec]:
    """All currently registered :class:`TrackSpec` instances, stable order."""
    return [spec for _, spec in sorted(_registry.items())]


def clear_registry() -> None:
    """Reset the registry. Test-only."""
    _registry.clear()


# -------------------------------------------------------------------
# Bootstrap + sync
# -------------------------------------------------------------------


def bootstrap_schema(
    engine: Engine,
    *,
    audit_table: str = "auditlog",
    context_table: str = "audit_context",
    months_ahead: int = 3,
) -> None:
    """Idempotently install the audit log + context table + helper functions.

    Executes the same SQL the Django integration's ``0001_initial``
    migration runs, but via SQLAlchemy transactions. Safe to call on
    every app startup — the DDL uses ``CREATE ... IF NOT EXISTS``.
    """
    from sqlalchemy import text

    parts = [
        generate_audit_context_table_sql(context_table),
        generate_auditlog_table_sql(audit_table),
        generate_jsonb_diff_function_sql(),
        generate_audit_attach_context_sql(context_table),
        generate_audit_current_user_id_sql(),
        generate_audit_reconstruct_sql(audit_table),
        generate_auditlog_partitions_sql(audit_table, months_ahead=months_ahead),
    ]
    full_sql = "\n\n".join(p.rstrip(";") + ";" if not p.rstrip().endswith(";") else p for p in parts)

    with engine.begin() as conn:
        for statement in full_sql.split(";\n"):
            stmt = statement.strip()
            if stmt:
                conn.execute(text(stmt))


def sync(
    engine: Engine,
    *,
    specs: list[TrackSpec] | None = None,
    prune: bool = False,
) -> SyncReport:
    """Idempotently install / update triggers for the registered specs.

    Uses :class:`TriggerManager` under the hood so drift detection,
    advisory locks, and the tracking table work identically to the
    Django path. If ``specs`` is ``None``, syncs everything in the
    module-level registry.
    """
    target = specs if specs is not None else registered_specs()
    with engine.begin() as conn:
        executor = SQLAlchemyExecutor(conn)
        mgr = TriggerManager(executor)
        mgr.bootstrap()
        return mgr.sync(target, prune=prune)
