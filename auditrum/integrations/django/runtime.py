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

__all__ = [
    "auditrum_context",
    "current_context",
]


def _ensure_wrapper_registered(conn) -> None:
    """Register :func:`_inject_audit_context` on a ``DatabaseWrapper``
    if not already present.

    Idempotent — safe to call repeatedly on the same connection, and
    safe to call on a partially-initialised wrapper
    (``execute_wrappers`` is always a plain list on Django's
    ``BaseDatabaseWrapper``, but we guard with ``getattr`` for the
    edge case of custom backends).
    """
    wrappers = getattr(conn, "execute_wrappers", None)
    if wrappers is None:
        return
    if _inject_audit_context not in wrappers:
        wrappers.append(_inject_audit_context)


def _on_connection_created(sender, connection, **kwargs) -> None:  # noqa: ARG001
    """``django.db.backends.signals.connection_created`` handler.

    Auto-wires the audit context wrapper onto every freshly-
    connected ``DatabaseWrapper``, including the per-thread wrappers
    that ``sync_to_async`` lazily creates for async ORM calls. The
    module-level ``AppConfig.ready`` walk of
    ``connections.all()`` handles any wrappers that were connected
    before the signal was wired up; from that point forward the
    signal keeps new ones covered.
    """
    _ensure_wrapper_registered(connection)


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

    Registered once per ``DatabaseWrapper`` via the
    ``connection_created`` signal handler in
    :class:`~auditrum.integrations.django.apps.PgAuditIntegrationConfig`,
    rather than per-request via ``connection.execute_wrapper`` inside
    :class:`auditrum_context`. The rationale is async ORM coverage:
    ``sync_to_async`` dispatches SQL onto thread-pool workers, each
    with its own thread-local ``DatabaseWrapper``. Registering
    per-context on ``django.db.connection`` (which resolves to the
    *current* thread's wrapper) leaves every worker thread's
    connection without a wrapper and silently drops ``context_id``
    on async writes.

    Permanent registration is safe because this function is a no-op
    when ``_tracker.get() is None`` — the context-var check below
    short-circuits before any SQL is rewritten, so the wrapper
    costs one dict lookup per query outside an active
    :class:`auditrum_context` block.
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

    result = execute(new_sql, new_params, many, context)

    # The injection turns a single user statement into a two-statement
    # submission. psycopg3 leaves the cursor positioned on the FIRST
    # result set after ``execute`` — the ``SELECT set_config(...)``
    # row — so any downstream ``cursor.fetchone()`` /
    # ``cursor.fetchall()`` call by Django's ORM reads the GUC values
    # instead of the user query's rows. For ``INSERT … RETURNING id``
    # this means ``instance.pk`` ends up set to the context UUID
    # string rather than the ``bigint`` the database actually
    # generated — breaking ``.save()``, FK assignment, and every
    # ``filter(pk=…)`` lookup downstream (eridan-catalog team hit
    # this in four separate code paths on 0.4.2).
    #
    # Advancing to the next result set via ``nextset()`` before
    # returning moves the cursor to the user's query results, so the
    # rest of Django's pipeline sees exactly what it would without
    # the wrapper. ``nextset()`` is a DB-API 2.0 method; psycopg3
    # supports it, and it returns ``None`` (or ``False``) harmlessly
    # for statements that produced only one result set (DDL, bare
    # UPDATE/DELETE without RETURNING).
    cursor = context.get("cursor")
    if cursor is not None:
        # Defensive: if a custom backend's cursor doesn't support
        # ``nextset()``, fall through rather than crash. Better a
        # cursor-state issue downstream than a hard failure here on
        # every query.
        with contextlib.suppress(Exception):
            cursor.nextset()

    return result


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
        self._token = None

    def __enter__(self) -> _Context:
        existing = _tracker.get()

        if existing is None:
            # Outermost context: enrich, push tracker. The execute
            # wrapper is registered at AppConfig.ready time on every
            # connection via the connection_created signal, so we do
            # not touch it here. As a belt-and-braces safety for the
            # case where ``ready`` ran before the current thread's
            # connection existed (e.g. very early management commands),
            # we also explicitly register on whatever the current
            # resolved ``connection`` is now.
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
            _ensure_wrapper_registered(connection)
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
        # The wrapper stays registered on the connection for its
        # lifetime — it's a no-op when ``_tracker.get() is None``, so
        # there's nothing to "clean up" on exit beyond resetting the
        # ContextVar. Leaving it registered is what makes async ORM
        # work: a thread-pool worker's connection only ever sees the
        # wrapper through the signal handler, not through anything
        # this block could set up.
        if self._token is not None:
            try:
                _tracker.reset(self._token)
            finally:
                self._token = None


def current_context() -> _Context | None:
    """Return the currently-active audit context, if any."""
    return _tracker.get()
