from contextlib import contextmanager
from contextvars import ContextVar
from functools import wraps
from typing import Any, Callable, Dict


class AuditContext:
    def __init__(self):
        self._data: ContextVar[Dict[str, Any]] = ContextVar("audit_ctx_data")
        self._reason_stack: ContextVar[list[str]] = ContextVar("audit_ctx_reason")

    def _ensure_data(self) -> dict:
        try:
            return self._data.get()
        except LookupError:
            value = {}
            self._data.set(value)
            return value

    def _ensure_reason_stack(self) -> list[str]:
        try:
            return self._reason_stack.get()
        except LookupError:
            value = []
            self._reason_stack.set(value)
            return value

    def set(self, key: str, value: Any):
        ctx = self._ensure_data().copy()
        ctx[key] = value
        self._data.set(ctx)

    def get(self, key: str) -> Any:
        return self._ensure_data().get(key)

    def push_change_reason(self, reason: str):
        stack = self._ensure_reason_stack().copy()
        stack.append(reason)
        self._reason_stack.set(stack)

    def get_change_reason(self) -> str:
        return " -> ".join(self._ensure_reason_stack())

    def build_sql(self) -> str:
        ctx = self._ensure_data().copy()
        reason = self.get_change_reason()
        if reason:
            ctx["change_reason"] = reason

        lines = []
        for k, v in ctx.items():
            key = f"session.myapp_{k}"
            val = str(v).replace("'", "''")
            lines.append(f"SET {key} = '{val}';")
        return "\n".join(lines)

    @contextmanager
    def use(self, reset: bool = False, **kwargs):
        from django.db import connection

        original_data = self._ensure_data().copy()
        ctx = original_data.copy()
        ctx.update(kwargs)
        self._data.set(ctx)

        # SET session variables
        sql = self.build_sql()
        with connection.cursor() as cursor:
            for stmt in sql.strip().split(";"):
                if stmt.strip():
                    cursor.execute(stmt)

        try:
            yield
        finally:
            self._data.set(original_data)
            if reset:
                with connection.cursor() as cursor:
                    for k in ctx.keys():
                        cursor.execute(f"RESET session.myapp_{k}")

    @contextmanager
    def use_change_reason(self, reason: str, reset: bool = False):
        from django.db import connection

        original_stack = self._ensure_reason_stack().copy()
        stack = original_stack.copy()
        stack.append(reason)
        self._reason_stack.set(stack)

        # SET session.myapp_change_reason
        sql = self.build_sql()
        with connection.cursor() as cursor:
            for stmt in sql.strip().split(";"):
                if stmt.strip():
                    cursor.execute(stmt)

        try:
            yield
        finally:
            self._reason_stack.set(original_stack)
            if reset:
                with connection.cursor() as cursor:
                    cursor.execute("RESET session.myapp_change_reason")


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
