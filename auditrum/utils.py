from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from auditrum.context import audit_context


@contextmanager
def audit_tracked(**kwargs: Any) -> Iterator[None]:
    """Set transaction-local audit context for manual or automated actions.

    Useful for management commands, cron jobs, shell sessions, and other
    non-HTTP code paths.

    .. warning::
       The GUCs set by this helper are bound to the **current
       transaction** (``set_config(..., is_local=true)``). If the caller
       runs in autocommit mode without an explicit transaction, the
       audit context will be reset before any subsequent statement sees
       it and audit rows will lack metadata.

       For management commands wrap the work in
       ``django.db.transaction.atomic()``::

           from django.db import transaction

           with transaction.atomic(), audit_tracked(source="cron"):
               ...

       For HTTP requests prefer
       :class:`auditrum.integrations.django.runtime.auditrum_context`
       or the :class:`AuditrumMiddleware`, which use
       ``connection.execute_wrapper`` to prefix each statement so
       autocommit works without an explicit transaction.

    Example::

        with transaction.atomic(), audit_tracked(change_reason="Sync job", source="cron"):
            ...
    """
    change_reason = kwargs.pop("change_reason", None)
    with audit_context.use(**kwargs):
        if change_reason is not None:
            with audit_context.use_change_reason(change_reason):
                yield
        else:
            yield
