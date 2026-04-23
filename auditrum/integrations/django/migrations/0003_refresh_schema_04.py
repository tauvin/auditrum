"""Re-emit the schema helper functions whose bodies changed in 0.4.

The 0.1-era ``0001_initial`` migration installs ``jsonb_diff`` /
``_audit_attach_context`` / ``_audit_current_user_id`` /
``_audit_reconstruct_*`` by calling the ``generate_*_sql`` factories
once. If the factory output changes in a later release (as it did
in 0.4 for ``jsonb_diff`` — paired ``{field: {old, new}}`` replaces
the old values-only shape), **no new migration gets shipped by
default**. Users who ``pip install -U auditrum && migrate`` end up
with a DB that still runs the 0.3 function bodies — so new audit
rows are written in the old format despite the library being on
the new release.

This migration closes the gap for 0.4. From 0.4 onwards, every
release that changes a ``generate_*_sql`` body ships a migration
like this one alongside the version bump. The refresh is
idempotent: all helpers are emitted as ``CREATE OR REPLACE FUNCTION``
so re-running just replaces the body with the current Python code's
version.

The table-DDL helpers (``generate_auditlog_table_sql``,
``generate_audit_context_table_sql``, ``generate_auditlog_partitions_sql``)
are deliberately **not** re-run here — they use ``CREATE TABLE IF
NOT EXISTS``, which is a no-op on existing tables and can't update
the schema. Column-level changes ride their own explicit ALTER
migrations (see ``0002_drop_content_type_id``).

Reverse path is a no-op: we no longer ship the 0.3 function bodies,
and a user rolling back from 0.4 should either ``pip install
auditrum==0.3.x`` and re-migrate (which re-installs the 0.3 bodies
via its own ``0001_initial``), or accept that the function bodies
are frozen at the 0.4 revision.
"""

from django.db import migrations

from auditrum.integrations.django.settings import audit_settings
from auditrum.schema import (
    generate_audit_attach_context_sql,
    generate_audit_current_user_id_sql,
    generate_audit_reconstruct_sql,
    generate_jsonb_diff_function_sql,
)


def _refresh_schema(apps, schema_editor):
    """Re-execute the current release's PL/pgSQL helper bodies.

    Runs under the migration role's privileges (the admin role per
    the two-role deployment model in ``docs/hardening.md``), so the
    ``CREATE OR REPLACE FUNCTION`` calls succeed even if the app
    role has been stripped of DDL permissions via ``auditrum harden``.
    """
    sqls = [
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
    ]
    with schema_editor.connection.cursor() as cur:
        for sql in sqls:
            cur.execute(sql)


class Migration(migrations.Migration):
    dependencies = [
        ("auditrum_django", "0002_drop_content_type_id"),
    ]

    operations = [
        migrations.RunPython(_refresh_schema, migrations.RunPython.noop),
    ]
