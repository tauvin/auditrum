"""Framework-agnostic cursor provider used by :class:`AuditContext`.

``AuditContext.use()`` needs to execute ``SELECT set_config(...)`` on the
current database connection so audit triggers can read session GUCs. The
actual cursor is provided by a pluggable :class:`ConnectionExecutor` so the
core package does not need to import Django (or any other web framework).

Three executors ship out of the box:

* :class:`NullExecutor` — default; discards everything. Safe for imports in
  environments without a database connection (tests, docs, CLI ``--dry-run``).
* :class:`PsycopgExecutor` — wraps a live :mod:`psycopg` connection. Use for
  scripts, management commands, and FastAPI/Flask integrations.
* ``DjangoExecutor`` — lives in :mod:`auditrum.integrations.django.executor`
  so the core module does not depend on Django.
"""

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any, Protocol, runtime_checkable

__all__ = [
    "ConnectionExecutor",
    "CursorProtocol",
    "NullExecutor",
    "PsycopgExecutor",
]


class CursorProtocol(Protocol):
    def execute(self, query: Any, params: Any = ...) -> Any: ...


@runtime_checkable
class ConnectionExecutor(Protocol):
    """Provides a cursor context-manager for AuditContext to write session GUCs."""

    def cursor(self) -> Any: ...


class _NullCursor:
    def execute(self, query: Any, params: Any = None) -> None:
        return None

    def __enter__(self) -> "_NullCursor":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        return None


class NullExecutor:
    """No-op executor used when no database is wired up yet."""

    def cursor(self) -> _NullCursor:
        return _NullCursor()


class PsycopgExecutor:
    """Executor backed by a live psycopg connection.

    Typical usage::

        import psycopg
        from auditrum import audit_context
        from auditrum.executor import PsycopgExecutor

        with psycopg.connect(dsn) as conn:
            audit_context.set_executor(PsycopgExecutor(conn))
            with audit_context.use(user_id=42, source="cli"):
                ...
    """

    def __init__(self, conn: Any) -> None:
        self._conn = conn

    @contextmanager
    def cursor(self) -> Iterator[Any]:
        with self._conn.cursor() as cur:
            yield cur
