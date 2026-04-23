"""Audit-context helpers for Celery / RQ / any task runner.

The Django middleware covers HTTP requests. Background tasks need
their own entry point — either a per-task decorator
(:func:`audit_task`) or a one-call signal wiring (:func:`install_celery_signals`)
that auto-wraps every task in the application.

Both hooks route through :class:`auditrum_context` so the trigger-side
``set_config`` propagation, OTel enrichment, and Sentry breadcrumbs
all behave exactly like they do for a real request.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from functools import wraps
from typing import Any

from auditrum.integrations.django.runtime import auditrum_context

__all__ = [
    "audit_task",
    "install_celery_signals",
]


def audit_task(*, source: str = "task", **metadata: Any):
    """Wrap a task function in :class:`auditrum_context`.

    Works with any runner that invokes the decorated callable directly
    (Celery, RQ, Dramatiq, APScheduler). ``source`` defaults to
    ``"task"`` so the audit trail can tell background-job events apart
    from HTTP requests. Extra keyword arguments land in the context
    metadata (and therefore in ``audit_context.metadata``).

    ``async def`` targets get an ``async`` wrapper so the context
    survives ``await`` boundaries — the sync wrapper would close the
    context before the coroutine ran.

    Example::

        @celery_app.task
        @audit_task(source="celery", queue="emails")
        def send_reminder(order_id):
            Order.objects.filter(id=order_id).update(reminder_sent=True)

    The ``@audit_task`` decorator must sit **inside** the task runner's
    own decorator (closer to the function) so the context is entered on
    the worker at actual execution time, not on the producer that
    enqueues the task.
    """

    def decorator(func: Callable) -> Callable:
        if asyncio.iscoroutinefunction(func):

            @wraps(func)
            async def awrapper(*args: Any, **kwargs: Any) -> Any:
                with auditrum_context(source=source, **metadata):
                    return await func(*args, **kwargs)

            return awrapper

        @wraps(func)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            with auditrum_context(source=source, **metadata):
                return func(*args, **kwargs)

        return wrapper

    return decorator


def install_celery_signals(*, source: str = "celery") -> None:
    """Auto-wrap every Celery task in :class:`auditrum_context`.

    One-shot alternative to decorating each task with
    :func:`audit_task`. Uses Celery's ``task_prerun`` / ``task_postrun``
    / ``task_failure`` signals to push and pop the context per task
    execution. Captures ``task_name`` and ``task_id`` in the context
    metadata automatically.

    Usage::

        # celery_config.py
        from auditrum.integrations.django.tasks import install_celery_signals
        install_celery_signals()

    Call this once at application startup. Calling it multiple times
    registers the handlers multiple times, which will push duplicate
    contexts per task — guard the call with a module-level flag if
    your startup path runs more than once.

    Raises :class:`ImportError` if Celery is not installed. ``auditrum``
    does not depend on Celery; users opt in by installing it themselves.
    """
    # Imported lazily so auditrum stays a non-Celery-dep package. ty
    # doesn't see Celery in the typecheck env — the import is a runtime
    # concern, not a typing one.
    from celery.signals import (  # ty: ignore[unresolved-import]  # noqa: PLC0415
        task_failure,
        task_postrun,
        task_prerun,
    )

    # Per-process map of in-flight task_id → open context manager.
    # Celery guarantees prerun/postrun fire on the same worker for a
    # given task_id, so a plain dict is enough; concurrent tasks get
    # distinct ids. ContextVar would sound cleaner but doesn't help:
    # the two signal handlers run in different call frames, and
    # Context.reset() must happen in the frame that called set().
    active: dict[str, auditrum_context] = {}

    def _pre(
        sender: Any = None,
        task_id: str | None = None,
        task: Any = None,
        **_: Any,
    ) -> None:
        if task_id is None or task_id in active:
            return
        ctx = auditrum_context(
            source=source,
            task_name=getattr(task, "name", None),
            task_id=task_id,
        )
        ctx.__enter__()
        active[task_id] = ctx

    def _post(
        sender: Any = None,
        task_id: str | None = None,
        **_: Any,
    ) -> None:
        if task_id is None:
            return
        ctx = active.pop(task_id, None)
        if ctx is not None:
            ctx.__exit__(None, None, None)

    def _fail(
        sender: Any = None,
        task_id: str | None = None,
        **_: Any,
    ) -> None:
        # task_postrun fires after task_failure, so postrun will do the
        # real cleanup. This handler exists only so we stay attached to
        # failure diagnostics for future extensions (Sentry breadcrumb,
        # failure reason in context.metadata, etc.).
        return

    task_prerun.connect(_pre, weak=False)
    task_postrun.connect(_post, weak=False)
    task_failure.connect(_fail, weak=False)
