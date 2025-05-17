import structlog
from django.db import connection

from auditrum.integrations.django.audit import registry
from auditrum.integrations.django.settings import audit_settings
from auditrum.triggers import generate_trigger_sql

log = structlog.get_logger()


def register_post_migrate_hook(sender, **kwargs):
    with connection.cursor() as cursor:
        for _model_cls, config in registry.items():
            table_name = config["table_name"]
            track_only = config["track_only"]
            exclude_fields = config["exclude_fields"]
            log_conditions = config["log_conditions"]
            meta_fields = config["meta_fields"]

            sql = generate_trigger_sql(
                table_name=table_name,
                track_only=track_only,
                exclude_fields=exclude_fields,
                log_conditions=log_conditions,
                meta_fields=meta_fields,
                audit_table=audit_settings.table_name,
            )
            log.info("Applying audit trigger", table=table_name)
            cursor.execute(sql)
