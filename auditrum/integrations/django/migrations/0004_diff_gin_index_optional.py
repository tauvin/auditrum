"""Make the audit log ``diff`` GIN index opt-in (issue #6).

The GIN index on the ``diff`` jsonb column was created unconditionally by
``0001_initial``. Production measurements on two independent deployments
(catalog, bidwise) found it gets **0 scans** yet costs **700-860 MB per
monthly partition** and is re-maintained on every audited write. It only
helps apps that run ``WHERE diff @> '{...}'`` jsonb-containment queries,
which neither deployment does — so it is now opt-in via the
``PGAUDIT_DIFF_GIN_INDEX`` setting (default ``False``).

This migration is a **one-shot convergence**: on first apply it brings the
existing deployment to whatever ``PGAUDIT_DIFF_GIN_INDEX`` is configured to
at apply time:

* ``PGAUDIT_DIFF_GIN_INDEX = False`` (default) → drop the index if present.
* ``PGAUDIT_DIFF_GIN_INDEX = True`` → create it if absent.

The setting is read **lazily inside** :func:`forward` / :func:`reverse`
(mirroring ``0003_refresh_schema_04``), not at import time, so
``sqlmigrate`` and the migration loader reflect the live configuration.

Once applied, Django records this migration as done and will **not** re-run
it. Flipping ``PGAUDIT_DIFF_GIN_INDEX`` and running ``migrate`` again does
**nothing** here — to change the index later, create a new migration that
issues the DDL, or run the ``CREATE INDEX`` / ``DROP INDEX`` statement
manually.

The audit log table is RANGE-partitioned by month, so ``DROP INDEX IF
EXISTS`` / ``CREATE INDEX IF NOT EXISTS`` is issued against the partitioned
**parent**; Postgres cascades the change to every partition. We do not use
``CONCURRENTLY`` here because it cannot run inside a migration's
transaction. ``IF EXISTS`` / ``IF NOT EXISTS`` keep the DDL idempotent.

There is no model-state operation: :class:`AuditLog` is ``managed = False``
and never declared this index in its ``Meta.indexes``, so Django's
autodetector has nothing to reconcile — ``makemigrations`` stays clean.
"""

from django.db import migrations


def forward(apps, schema_editor):
    """Converge the diff GIN index to the configured state (read at apply time)."""
    from auditrum.integrations.django.settings import audit_settings
    from auditrum.tracking.spec import validate_identifier

    table = audit_settings.table_name
    validate_identifier(table, "table_name")
    if audit_settings.diff_gin_index:
        sql = f"CREATE INDEX IF NOT EXISTS {table}_diff_gin_idx ON {table} USING GIN (diff);"
    else:
        sql = f"DROP INDEX IF EXISTS {table}_diff_gin_idx;"
    schema_editor.execute(sql)


def reverse(apps, schema_editor):
    """Undo :func:`forward` for the same configured state (read at apply time)."""
    from auditrum.integrations.django.settings import audit_settings
    from auditrum.tracking.spec import validate_identifier

    table = audit_settings.table_name
    validate_identifier(table, "table_name")
    if audit_settings.diff_gin_index:
        sql = f"DROP INDEX IF EXISTS {table}_diff_gin_idx;"
    else:
        sql = f"CREATE INDEX IF NOT EXISTS {table}_diff_gin_idx ON {table} USING GIN (diff);"
    schema_editor.execute(sql)


class Migration(migrations.Migration):
    dependencies = [
        ("auditrum_django", "0003_refresh_schema_04"),
    ]

    operations = [
        migrations.RunPython(forward, reverse),
    ]
