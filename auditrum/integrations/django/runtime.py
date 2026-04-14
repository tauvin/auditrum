"""Request/job-scoped context tracking for auditrum via Django execute_wrapper.

This module ports django-pghistory's pattern but with a strictly
immutable context object so async tasks sharing the same outermost
context cannot lose-write each other's metadata:

1. ``auditrum_context(**metadata)`` is a :class:`contextlib.ContextDecorator`
   that generates a new UUID for the context and registers a Django
   ``connection.execute_wrapper`` for the duration of the block.
2. :func:`_inject_audit_context` is the wrapper callback. Before each user
   query, it prepends a ``SELECT set_config('auditrum.context_id', %s, true),
   set_config('auditrum.context_metadata', %s, true); `` statement, binding
   the current context UUID and JSON-serialized metadata. Because the config
   call and the real statement are in the **same** SQL submission, they
   share a transaction so ``is_local=true`` (``SET LOCAL`` semantics) applies
   without needing ``transaction.atomic()``.
3. Nested entries push a **new** ``_Context`` onto the ``ContextVar`` with
   merged metadata. The outermost context is restored on inner exit. No
   shared mutable state — safe under concurrent async tasks.
4. The stored procedure ``_audit_attach_context()`` (see
   :func:`auditrum.schema.generate_audit_attach_context_sql`) reads those
   GUCs via ``current_setting()`` and lazily upserts a row into
   ``audit_context`` only when an audit trigger actually fires. Read-only
   requests pay zero write cost.

Named cursors, queries inside errored transactions, and non-injectable
statement prefixes (``SELECT``, ``CREATE``, ``ALTER``, ``VACUUM``, …) are
skipped — matching django-pghistory's safety rules.
"""

from __future__ import annotations

import contextlib
import json
import uuid
from collections.abc import Mapping
from contextvars import ContextVar
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any

from django.db import connection

from auditrum.integrations.django.settings import audit_settings


@dataclass(frozen=True)
class _Context:
    """Immutable per-request audit context.

    Both ``id`` and ``metadata`` are read-only after construction.
    ``metadata`` is wrapped in a :class:`types.MappingProxyType` so even
    code that has a reference cannot mutate it. To "extend" a context,
    construct a new ``_Context`` and push it onto the :data:`_tracker`
    ``ContextVar``.
    """

    id: uuid.UUID
    metadata: Mapping[str, Any] = field(default_factory=lambda: MappingProxyType({}))


_tracker: ContextVar[_Context | None] = ContextVar("auditrum_tracker", default=None)


IGNORED_SQL_PREFIXES: tuple[str, ...] = (
    "select",
    "with",
    "vacuum",
    "analyze",
    "checkpoint",
    "discard",
    "load",
    "cluster",
    "reindex",
    "create",
    "alter",
    "drop",
    "set transaction",
    "set session",
    "begin",
    "start",
    "commit",
    "rollback",
    "savepoint",
    "release",
)


def _is_ignored_statement(sql: str | bytes) -> bool:
    if not sql:
        return True
    text = sql.decode() if isinstance(sql, bytes) else sql
    return text.strip().lower().startswith(IGNORED_SQL_PREFIXES)


def _is_transaction_errored(cursor) -> bool:
    try:
        import psycopg.pq

        return cursor.connection.info.transaction_status == psycopg.pq.TransactionStatus.INERROR
    except Exception:
        return False


def _can_inject_variable(cursor, sql: str | bytes) -> bool:
    """Match pghistory's injection safety rules."""
    return (
        not _is_ignored_statement(sql)
        and not getattr(cursor, "name", None)
        and not _is_transaction_errored(cursor)
    )


def _inject_audit_context(execute, sql, params, many, context):
    """Prepend ``SET LOCAL``-style config calls to every query.

    Added as a :meth:`django.db.connection.execute_wrapper` by
    :class:`auditrum_context` on enter and removed on exit.
    """
    tracker = _tracker.get()
    if tracker is None:
        return execute(sql, params, many, context)

    is_bytes = isinstance(sql, bytes)
    sql_str = sql.decode() if is_bytes else sql

    if not _can_inject_variable(context["cursor"], sql_str):
        return execute(sql, params, many, context)

    serialized_metadata = json.dumps(dict(tracker.metadata), default=str)
    # Bind both the GUC *name* and the value as parameters. The settings
    # accessor validates the name against an identifier regex so this is
    # belt-and-braces, but doing it via bound params eliminates the
    # f-string interpolation path entirely.
    ctx_params = {
        "auditrum__guc_id_name": audit_settings.guc_id,
        "auditrum__context_id": str(tracker.id),
        "auditrum__guc_metadata_name": audit_settings.guc_metadata,
        "auditrum__context_metadata": serialized_metadata,
    }

    if isinstance(params, dict):
        id_name_ph = "%(auditrum__guc_id_name)s"
        id_val_ph = "%(auditrum__context_id)s"
        md_name_ph = "%(auditrum__guc_metadata_name)s"
        md_val_ph = "%(auditrum__context_metadata)s"
        new_params: Any = {**params, **ctx_params}
    else:
        id_name_ph = id_val_ph = md_name_ph = md_val_ph = "%s"
        new_params = (*ctx_params.values(), *(params or ()))

    inject = (
        f"SELECT set_config({id_name_ph}, {id_val_ph}, true), "
        f"set_config({md_name_ph}, {md_val_ph}, true); "
    )
    new_sql: Any = inject + sql_str
    if is_bytes:
        new_sql = new_sql.encode()

    return execute(new_sql, new_params, many, context)


class auditrum_context(contextlib.ContextDecorator):
    """Group DB changes under a single audit context.

    Works as a context manager or a decorator::

        with auditrum_context(user_id=42, source="http"):
            User.objects.create(...)

    Nested entries merge metadata into a **new** immutable context that
    inherits the outer ``id``. After the inner block exits, the outer
    context is restored verbatim — inner-block keys do not leak out.
    The merge is one-shot and copy-on-push, so there is no shared mutable
    state and concurrent async tasks cannot lose-write each other.
    """

    def __init__(self, **metadata: Any) -> None:
        self.metadata: dict[str, Any] = dict(metadata)
        self._hook = None
        self._token = None

    def __enter__(self) -> _Context:
        existing = _tracker.get()

        if existing is None:
            # Outermost context: enrich, register hook, push tracker.
            metadata: dict[str, Any] = dict(self.metadata)

            # Auto-enrich with OTel trace context + optional Sentry breadcrumb.
            # Both are soft deps and no-op when the libraries aren't installed.
            from auditrum.observability.otel import enrich_metadata
            from auditrum.observability.sentry import add_breadcrumb_for_context

            enrich_metadata(metadata)
            add_breadcrumb_for_context(metadata)

            new_ctx = _Context(
                id=uuid.uuid4(),
                metadata=MappingProxyType(metadata),
            )
            self._hook = connection.execute_wrapper(_inject_audit_context)
            self._hook.__enter__()
            self._token = _tracker.set(new_ctx)
            return new_ctx

        # Nested entry: copy outer metadata, merge inner kwargs, push a
        # *new* immutable context with the same id. The outer Context
        # object is untouched — restored on __exit__ via reset().
        merged: dict[str, Any] = {**existing.metadata, **self.metadata}
        merged_ctx = _Context(
            id=existing.id,
            metadata=MappingProxyType(merged),
        )
        self._token = _tracker.set(merged_ctx)
        return merged_ctx

    def __exit__(self, *exc) -> None:
        if self._token is not None:
            try:
                _tracker.reset(self._token)
            finally:
                self._token = None
        if self._hook is not None:
            self._hook.__exit__(*exc)
            self._hook = None


def current_context() -> _Context | None:
    """Return the currently-active audit context, if any."""
    return _tracker.get()
