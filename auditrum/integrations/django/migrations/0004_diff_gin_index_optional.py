"""Make the audit log ``diff`` GIN index opt-in (issue #6).

The GIN index on the ``diff`` jsonb column was created unconditionally by
``0001_initial``. Production measurements on two independent deployments
(catalog, bidwise) found it gets **0 scans** yet costs **700-860 MB per
monthly partition** and is re-maintained on every audited write. It only
helps apps that run ``WHERE diff @> '{...}'`` jsonb-containment queries,
which neither deployment does — so it is now opt-in via the
``PGAUDIT_DIFF_GIN_INDEX`` setting (default ``False``).

This migration converges existing deployments to the new default:

* ``PGAUDIT_DIFF_GIN_INDEX = False`` (default) → drop the index if present.
* ``PGAUDIT_DIFF_GIN_INDEX = True`` → create it if absent.

The audit log table is RANGE-partitioned by month, so ``DROP INDEX IF
EXISTS`` / ``CREATE INDEX IF NOT EXISTS`` is issued against the partitioned
**parent**; Postgres cascades the change to every partition. We do not use
``CONCURRENTLY`` here because it cannot run inside a migration's
transaction. The DDL is idempotent, so re-running (or flipping the setting
and re-migrating) is safe.

There is no model-state operation: :class:`AuditLog` is ``managed = False``
and never declared this index in its ``Meta.indexes``, so Django's
autodetector has nothing to reconcile — ``makemigrations`` stays clean.
"""

from django.db import migrations

from auditrum.integrations.django.settings import audit_settings

_table = audit_settings.table_name
_create_sql = f"CREATE INDEX IF NOT EXISTS {_table}_diff_gin_idx ON {_table} USING GIN (diff);"
_drop_sql = f"DROP INDEX IF EXISTS {_table}_diff_gin_idx;"

if audit_settings.diff_gin_index:
    _forward_sql = _create_sql
    _reverse_sql = _drop_sql
else:
    _forward_sql = _drop_sql
    _reverse_sql = _create_sql


class Migration(migrations.Migration):
    dependencies = [
        ("auditrum_django", "0003_refresh_schema_04"),
    ]

    operations = [
        migrations.RunSQL(sql=_forward_sql, reverse_sql=_reverse_sql),
    ]
