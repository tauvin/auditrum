from django.db import migrations

from auditrum.integrations.django.settings import audit_settings
from auditrum.schema import (
    generate_audit_attach_context_sql,
    generate_audit_context_table_sql,
    generate_audit_current_user_id_sql,
    generate_audit_reconstruct_sql,
    generate_auditlog_partitions_sql,
    generate_auditlog_table_sql,
    generate_jsonb_diff_function_sql,
)


class Migration(migrations.Migration):
    initial = True
    dependencies = []

    # Reading diff_gin_index/table_name at import time is intentional here:
    # 0001 runs once on a clean DB, and later changes to either are handled by
    # 0004_diff_gin_index_optional / the 0003 schema refresh.
    operations = [
        migrations.RunSQL(
            sql="\n\n".join(
                [
                    generate_audit_context_table_sql(audit_settings.context_table_name),
                    generate_auditlog_table_sql(
                        audit_settings.table_name,
                        diff_gin_index=audit_settings.diff_gin_index,
                    ),
                    generate_jsonb_diff_function_sql(),
                    generate_audit_attach_context_sql(
                        audit_settings.context_table_name,
                        guc_id=audit_settings.guc_id,
                        guc_metadata=audit_settings.guc_metadata,
                    ),
                    generate_audit_current_user_id_sql(
                        guc_metadata=audit_settings.guc_metadata,
                    ),
                    generate_audit_reconstruct_sql(audit_settings.table_name),
                    generate_auditlog_partitions_sql(audit_settings.table_name, months_ahead=3),
                ]
            ),
            reverse_sql=(
                "DROP FUNCTION IF EXISTS _audit_attach_context() CASCADE; "
                "DROP FUNCTION IF EXISTS _audit_current_user_id() CASCADE; "
                "DROP FUNCTION IF EXISTS _audit_reconstruct_row(text, text, timestamptz) CASCADE; "
                "DROP FUNCTION IF EXISTS _audit_reconstruct_table(text, timestamptz) CASCADE; "
                f"DROP TABLE IF EXISTS {audit_settings.table_name} CASCADE; "
                f"DROP TABLE IF EXISTS {audit_settings.context_table_name} CASCADE;"
            ),
        ),
    ]
