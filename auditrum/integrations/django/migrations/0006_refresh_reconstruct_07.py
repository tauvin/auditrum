"""Re-emit the time-travel helpers rewritten in 0.7.

0.7 replaces ``_audit_reconstruct_row`` / ``_audit_reconstruct_table``
with one algorithm for full and diff storage and adds four helpers
(``_audit_filter_row``, ``_audit_tracked_spec``, ``_audit_live_row``,
``_audit_live_rows``). The functions become ``SECURITY INVOKER``. Same
pattern as ``0003_refresh_schema_04``: forwards replaces the bodies,
backwards is a no-op.
"""

from django.db import migrations
from django.db.backends.base.schema import BaseDatabaseSchemaEditor
from django.db.migrations.state import StateApps

from auditrum.integrations.django.settings import audit_settings
from auditrum.schema import generate_audit_reconstruct_sql


def _refresh_reconstruct(apps: StateApps, schema_editor: BaseDatabaseSchemaEditor) -> None:
    with schema_editor.connection.cursor() as cur:
        cur.execute(generate_audit_reconstruct_sql(audit_settings.table_name))


class Migration(migrations.Migration):
    dependencies = [
        ("auditrum_django", "0005_model_state"),
    ]

    operations = [
        migrations.RunPython(_refresh_reconstruct, migrations.RunPython.noop),
    ]
