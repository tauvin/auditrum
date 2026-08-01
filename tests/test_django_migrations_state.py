"""Guard the model-state migration (``0005_model_state``).

Every other migration in the app is raw ``RunSQL`` / ``RunPython``, so the
autodetector used to see ``AuditLog`` / ``AuditContext`` as models that had
never been migrated and reported them on every ``makemigrations`` run in
downstream projects. ``0005_model_state`` declares them to the state graph
without emitting DDL. These tests pin both halves of that contract.
"""

import io

import pytest

django = pytest.importorskip("django")

from django.conf import settings as django_settings  # noqa: E402

if not django_settings.configured:
    django_settings.configure(
        INSTALLED_APPS=[
            "django.contrib.contenttypes",
            "django.contrib.auth",
            "auditrum.integrations.django",
        ],
        DATABASES={"default": {"ENGINE": "django.db.backends.sqlite3", "NAME": ":memory:"}},
        USE_TZ=True,
        DEFAULT_AUTO_FIELD="django.db.models.BigAutoField",
    )
    django.setup()

from django.core.management import call_command  # noqa: E402
from django.db import connection  # noqa: E402
from django.db.migrations.loader import MigrationLoader  # noqa: E402

from auditrum.integrations.django.settings import audit_settings  # noqa: E402

MIGRATION = ("auditrum_django", "0005_model_state")


class TestNoPendingMigrations:
    def test_makemigrations_check_is_clean(self):
        """``makemigrations --check`` must not want anything from our app.

        This is the regression the migration exists for: without the model
        state, the autodetector asks for ``Create model AuditContext`` /
        ``Create model AuditLog`` forever, and any downstream CI gating on
        ``--check`` fails through no fault of the project.
        """
        out = io.StringIO()
        # SystemExit(1) is how the command signals "changes are missing".
        call_command(
            "makemigrations",
            "auditrum_django",
            check=True,
            dry_run=True,
            stdout=out,
            verbosity=1,
        )
        assert "Create model" not in out.getvalue()


class TestModelStateMigration:
    def _migration(self):
        loader = MigrationLoader(None, ignore_no_migrations=True)
        return loader.disk_migrations[MIGRATION]

    def test_declares_both_models(self):
        names = {op.name for op in self._migration().operations}
        assert names == {"AuditContext", "AuditLog"}

    def test_every_model_is_unmanaged(self):
        """``managed = False`` is what keeps the migration state-only.

        ``CreateModel.database_forwards`` short-circuits on unmanaged models,
        so applying this migration against a live deployment issues no DDL
        and cannot disturb the partitioned tables ``0001_initial`` built.
        """
        for op in self._migration().operations:
            assert op.options["managed"] is False

    def test_emits_no_ddl(self):
        """Apply the migration with a SQL-collecting schema editor: no statements.

        ``collect_sql`` still records a comment banner per operation, so the
        assertion is that every collected line is a comment — and that Django
        itself labelled both operations ``(no-op)``.
        """
        loader = MigrationLoader(None, ignore_no_migrations=True)
        migration = loader.disk_migrations[MIGRATION]
        before = loader.project_state(("auditrum_django", "0004_diff_gin_index_optional"))
        with connection.schema_editor(collect_sql=True, atomic=False) as editor:
            migration.apply(before, editor, collect_sql=True)
            collected = editor.collected_sql

        assert collected, "expected the comment banner Django emits per operation"
        assert all(line.startswith("--") for line in collected), collected
        assert collected.count("-- (no-op)") == len(migration.operations)

    def test_db_table_follows_settings(self):
        """``db_table`` is read from settings, not frozen to the default.

        A project on a custom ``PGAUDIT_TABLE_NAME`` would otherwise trade the
        ``CreateModel`` noise for a permanent ``AlterModelTable``.
        """
        options = {op.name: op.options for op in self._migration().operations}
        assert options["AuditLog"]["db_table"] == audit_settings.table_name
        assert options["AuditContext"]["db_table"] == audit_settings.context_table_name
