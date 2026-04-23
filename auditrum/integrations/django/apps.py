from importlib import import_module

from django.apps import AppConfig, apps
from django.db import connections
from django.db.backends.signals import connection_created

from auditrum.context import audit_context
from auditrum.integrations.django.executor import DjangoExecutor
from auditrum.integrations.django.runtime import (
    _ensure_wrapper_registered,
    _on_connection_created,
)

__all__ = ["PgAuditIntegrationConfig"]


class PgAuditIntegrationConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "auditrum.integrations.django"
    label = "auditrum_django"
    verbose_name = "PostgreSQL Audit (auditrum)"

    def ready(self):
        audit_context.set_executor(DjangoExecutor())

        # Wire the audit-context execute wrapper onto every database
        # connection — present and future. Registering once via the
        # ``connection_created`` signal covers thread-pool connections
        # that ``sync_to_async`` creates on demand (the async ORM
        # code path); explicitly registering on ``connections.all()``
        # covers any connection that was already instantiated before
        # ``ready`` ran. The wrapper is a no-op when there is no
        # active ``auditrum_context``, so permanent registration is
        # safe.
        #
        # ``dispatch_uid`` prevents double-registration if ``ready``
        # runs twice (some test runners reimport apps across
        # scenarios).
        connection_created.connect(
            _on_connection_created,
            dispatch_uid="auditrum.integrations.django.runtime._on_connection_created",
        )
        for conn in connections.all():
            _ensure_wrapper_registered(conn)

        # Auto-discover per-app audit.py modules so their register() calls
        # attach pgtrigger Triggers to the target models before migrations run.
        for app_config in apps.get_app_configs():
            try:
                import_module(f"{app_config.name}.audit")
            except ModuleNotFoundError:
                continue
