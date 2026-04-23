from django.db import migrations

from auditrum.integrations.django.settings import audit_settings


class Migration(migrations.Migration):
    dependencies = [
        ("auditrum_django", "0001_initial"),
    ]

    operations = [
        migrations.RunSQL(
            sql=(
                f"ALTER TABLE {audit_settings.table_name} "
                "DROP COLUMN IF EXISTS content_type_id;"
            ),
            reverse_sql=(
                f"ALTER TABLE {audit_settings.table_name} "
                "ADD COLUMN IF NOT EXISTS content_type_id integer;"
            ),
        ),
    ]
