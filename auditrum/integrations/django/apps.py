from django.apps import AppConfig
from django.db.models.signals import post_migrate


class PgAuditIntegrationConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "auditrum.integrations.django"
    label = "auditrum_django"
    verbose_name = "PostgreSQL Audit (auditrum)"

    def ready(self):
        from .hooks import register_post_migrate_hook
        from django.apps import apps
        from importlib import import_module

        post_migrate.connect(register_post_migrate_hook, sender=self)

        # Automatically discover and import audit.py from all installed apps
        for app_config in apps.get_app_configs():
            try:
                import_module(f"{app_config.name}.audit")
            except ModuleNotFoundError:
                continue
