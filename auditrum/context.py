import re
from collections.abc import Callable
from contextlib import contextmanager
from contextvars import ContextVar
from functools import wraps
from typing import Any

from auditrum.executor import ConnectionExecutor, NullExecutor

_IDENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def _is_valid_key(key: str) -> bool:
    return isinstance(key, str) and bool(_IDENT_RE.match(key))


def _apply_ctx(cursor, items: dict[str, Any]) -> None:
    """Apply key/value pairs as transaction-local GUCs via ``set_config()``.

    Uses ``is_local=true`` so values are bound to the current transaction
    and automatically reset at commit/rollback. This eliminates leaks
    across requests on pooled connections (pgbouncer, Django
    ``CONN_MAX_AGE > 0``).

    The caller is responsible for being inside an open transaction. If
    the cursor is in autocommit mode, the GUCs will be reset before any
    subsequent query sees them — use the framework-specific
    ``auditrum_context`` helper instead, which prefixes every statement
    with ``set_config`` in the same submission so autocommit works.

    Values are passed as bound parameters; keys are validated against
    an identifier regex to prevent SQL injection via the GUC name.
    """
    for k, v in items.items():
        if not _is_valid_key(k):
            continue
        cursor.execute(
            "SELECT set_config(%s, %s, true)",
            (f"session.myapp_{k}", "" if v is None else str(v)),
        )


def _reset_ctx(cursor, keys) -> None:
    """Best-effort reset of transaction-local GUCs.

    With ``is_local=true`` the GUCs auto-reset on transaction end, so
    this is technically redundant. We still emit it for the rare case
    where the caller manages a long-lived transaction across multiple
    audit blocks and wants the second block to start from a clean slate.
    """
    for k in keys:
        if not _is_valid_key(k):
            continue
        cursor.execute("SELECT set_config(%s, %s, true)", (f"session.myapp_{k}", ""))


class AuditContext:
    def __init__(self, executor: ConnectionExecutor | None = None):
        self._data: ContextVar[dict[str, Any]] = ContextVar("audit_ctx_data")
        self._reason_stack: ContextVar[list[str]] = ContextVar("audit_ctx_reason")
        self._executor: ConnectionExecutor = executor or NullExecutor()

    def set_executor(self, executor: ConnectionExecutor) -> None:
        """Install the active :class:`ConnectionExecutor`.

        Called by framework integrations (e.g. the Django AppConfig) to bind
        the audit context to the active database connection.
        """
        self._executor = executor

    def get_executor(self) -> ConnectionExecutor:
        return self._executor

    def _ensure_data(self) -> dict:
        try:
            return self._data.get()
        except LookupError:
            value: dict[str, Any] = {}
            self._data.set(value)
            return value

    def _ensure_reason_stack(self) -> list[str]:
        try:
            return self._reason_stack.get()
        except LookupError:
            value: list[str] = []
            self._reason_stack.set(value)
            return value

    def set(self, key: str, value: Any) -> None:
        ctx = self._ensure_data().copy()
        ctx[key] = value
        self._data.set(ctx)

    def get(self, key: str) -> Any:
        return self._ensure_data().get(key)

    def push_change_reason(self, reason: str) -> None:
        stack = self._ensure_reason_stack().copy()
        stack.append(reason)
        self._reason_stack.set(stack)

    def get_change_reason(self) -> str:
        return " -> ".join(self._ensure_reason_stack())

    def _build_items(self) -> dict[str, Any]:
        ctx = self._ensure_data().copy()
        reason = self.get_change_reason()
        if reason:
            ctx["change_reason"] = reason
        return ctx

    def build_sql(self) -> str:
        """Render current context as ``SELECT set_config(...)`` calls (debug use).

        Uses ``is_local=true`` (the third argument) so the GUCs are
        bound to the current transaction, matching the runtime behaviour
        of :meth:`use`. Intended for inspection and copy-paste-into-
        ``psql`` debugging — production code should use :meth:`use`.
        """
        items = self._build_items()
        lines = []
        for k, v in items.items():
            if not _is_valid_key(k):
                continue
            val = ("" if v is None else str(v)).replace("'", "''")
            lines.append(f"SELECT set_config('session.myapp_{k}', '{val}', true);")
        return "\n".join(lines)

    @contextmanager
    def use(self, reset: bool = True, **kwargs):
        original_data = self._ensure_data().copy()
        ctx = original_data.copy()
        ctx.update(kwargs)
        self._data.set(ctx)

        applied_items = self._build_items()
        with self._executor.cursor() as cursor:
            _apply_ctx(cursor, applied_items)

        try:
            yield
        finally:
            self._data.set(original_data)
            if reset:
                with self._executor.cursor() as cursor:
                    _reset_ctx(cursor, applied_items.keys())

    @contextmanager
    def use_change_reason(self, reason: str, reset: bool = True):
        original_stack = self._ensure_reason_stack().copy()
        stack = original_stack.copy()
        stack.append(reason)
        self._reason_stack.set(stack)

        with self._executor.cursor() as cursor:
            _apply_ctx(cursor, {"change_reason": self.get_change_reason()})

        try:
            yield
        finally:
            self._reason_stack.set(original_stack)
            if reset:
                with self._executor.cursor() as cursor:
                    _apply_ctx(
                        cursor,
                        {"change_reason": self.get_change_reason()},
                    )


audit_context = AuditContext()


def with_context(**kwargs):
    def decorator(func: Callable):
        @wraps(func)
        def wrapper(*args, **fkwargs):
            with audit_context.use(**kwargs):
                return func(*args, **fkwargs)

        return wrapper

    return decorator


def with_change_reason(reason: str):
    def decorator(func: Callable):
        @wraps(func)
        def wrapper(*args, **kwargs):
            with audit_context.use_change_reason(reason):
                return func(*args, **kwargs)

        return wrapper

    return decorator
