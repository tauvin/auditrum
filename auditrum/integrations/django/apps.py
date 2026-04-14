from importlib import import_module

from django.apps import AppConfig, apps

from auditrum.context import audit_context
from auditrum.integrations.django.executor import DjangoExecutor


class PgAuditIntegrationConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "auditrum.integrations.django"
    label = "auditrum_django"
    verbose_name = "PostgreSQL Audit (auditrum)"

    def ready(self):
        audit_context.set_executor(DjangoExecutor())

        # Auto-discover per-app audit.py modules so their register() calls
        # attach pgtrigger Triggers to the target models before migrations run.
        for app_config in apps.get_app_configs():
            try:
                import_module(f"{app_config.name}.audit")
            except ModuleNotFoundError:
                continue
