from django.conf import settings


class AuditSettings:
    @property
    def table_name(self) -> str:
        return getattr(settings, "PGAUDIT_TABLE_NAME", "auditlog")

    @property
    def enabled(self) -> bool:
        return getattr(settings, "PGAUDIT_ENABLED", True)


audit_settings = AuditSettings()
