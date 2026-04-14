"""Unit tests for InstallTrigger / UninstallTrigger migration operations.

Operations are exercised against a minimal fake ``schema_editor`` that
wraps a :class:`FakeExecutor` from :mod:`tests.test_tracking_manager`. We
verify that ``database_forwards`` triggers an install, ``database_backwards``
triggers an uninstall, and ``deconstruct`` round-trips through the exact
set of kwargs the migration file expects.
"""

from unittest.mock import MagicMock

import pytest

django = pytest.importorskip("django")

from django.conf import settings as django_settings  # noqa: E402

if not django_settings.configured:
    django_settings.configure(
        INSTALLED_APPS=[
            "django.contrib.contenttypes",
            "django.contrib.auth",
            "django.contrib.admin",
            "django.contrib.sessions",
            "django.contrib.messages",
            "auditrum.integrations.django",
        ],
        DATABASES={"default": {"ENGINE": "django.db.backends.sqlite3", "NAME": ":memory:"}},
        ROOT_URLCONF="django.contrib.contenttypes.urls",
        TEMPLATES=[
            {
                "BACKEND": "django.template.backends.django.DjangoTemplates",
                "DIRS": [],
                "APP_DIRS": True,
                "OPTIONS": {"context_processors": []},
            }
        ],
    )
    django.setup()

from auditrum.integrations.django.operations import (  # noqa: E402
    InstallTrigger,
    UninstallTrigger,
)


class _FakeCursor:
    def __init__(self, state):
        self._state = state
        self._result = []

    def execute(self, sql, params=None):
        self._state["executed"].append((sql, params))
        sql_lower = sql.strip().lower()
        if sql_lower.startswith("select pg_advisory_xact_lock"):
            self._result = [(1,)]
            return
        if sql_lower.startswith("create table"):
            self._result = []
            return
        if sql_lower.startswith("select checksum"):
            row = self._state["tracking"].get(params[0])
            self._result = [row] if row else []
            return
        if sql_lower.startswith("select trigger_name"):
            self._result = []
            return
        if sql_lower.startswith("insert into"):
            import json

            name, _table, checksum, fp = params
            self._state["tracking"][name] = (checksum, json.loads(fp))
            self._result = []
            return
        if sql_lower.startswith("delete from"):
            self._state["tracking"].pop(params[0], None)
            self._result = []
            return
        self._result = []

    def fetchone(self):
        return self._result[0] if self._result else None

    def fetchall(self):
        return list(self._result)

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


class _FakeDjangoConnection:
    """Mimics Django's database wrapper closely enough for PsycopgExecutor."""

    def __init__(self):
        self.state = {"executed": [], "tracking": {}}

    def cursor(self):
        return _FakeCursor(self.state)


class _FakeSchemaEditor:
    def __init__(self, conn):
        self.connection = conn


class TestInstallTriggerDeconstruct:
    def test_roundtrip_minimal(self):
        op = InstallTrigger(table="users")
        name, args, kwargs = op.deconstruct()
        assert name == "InstallTrigger"
        assert args == []
        assert kwargs == {"table": "users"}
        # reconstruct and check equality of spec
        rebuilt = InstallTrigger(**kwargs)
        assert rebuilt.spec == op.spec

    def test_roundtrip_with_fields(self):
        op = InstallTrigger(
            table="users", fields_kind="only", fields=["name", "email"]
        )
        _, _, kwargs = op.deconstruct()
        assert kwargs["fields_kind"] == "only"
        assert kwargs["fields"] == ["name", "email"]
        rebuilt = InstallTrigger(**kwargs)
        assert rebuilt.spec == op.spec

    def test_roundtrip_with_all_options(self):
        op = InstallTrigger(
            table="orders",
            audit_table="custom_log",
            fields_kind="exclude",
            fields=["password"],
            extra_meta_fields=["tenant_id"],
            log_condition="NEW.active = TRUE",
            trigger_name="my_trigger",
        )
        _, _, kwargs = op.deconstruct()
        rebuilt = InstallTrigger(**kwargs)
        assert rebuilt.spec == op.spec

    def test_defaults_omitted_from_deconstruct(self):
        """Default values should not appear in kwargs — keeps migrations concise."""
        op = InstallTrigger(table="users")
        _, _, kwargs = op.deconstruct()
        assert "audit_table" not in kwargs
        assert "fields_kind" not in kwargs
        assert "fields" not in kwargs
        assert "extra_meta_fields" not in kwargs


class TestInstallTriggerApply:
    def test_database_forwards_installs(self):
        op = InstallTrigger(table="users", fields_kind="only", fields=["name"])
        conn = _FakeDjangoConnection()
        schema_editor = _FakeSchemaEditor(conn)
        op.database_forwards("myapp", schema_editor, MagicMock(), MagicMock())
        statements = [sql for sql, _ in conn.state["executed"]]
        assert any("CREATE OR REPLACE FUNCTION audit_users_trigger" in s for s in statements)
        assert any("CREATE TRIGGER audit_users_trigger" in s for s in statements)
        assert "audit_users_trigger" in conn.state["tracking"]

    def test_database_backwards_uninstalls(self):
        op = InstallTrigger(table="users")
        conn = _FakeDjangoConnection()
        schema_editor = _FakeSchemaEditor(conn)
        op.database_forwards("myapp", schema_editor, MagicMock(), MagicMock())
        assert "audit_users_trigger" in conn.state["tracking"]
        op.database_backwards("myapp", schema_editor, MagicMock(), MagicMock())
        assert "audit_users_trigger" not in conn.state["tracking"]

    def test_state_forwards_is_noop(self):
        op = InstallTrigger(table="users")
        state = MagicMock()
        op.state_forwards("myapp", state)
        state.add_model.assert_not_called()


class TestUninstallTrigger:
    def test_database_forwards_drops(self):
        # Prime: install via InstallTrigger, then UninstallTrigger should remove it
        conn = _FakeDjangoConnection()
        schema_editor = _FakeSchemaEditor(conn)
        InstallTrigger(table="users").database_forwards(
            "myapp", schema_editor, MagicMock(), MagicMock()
        )
        assert "audit_users_trigger" in conn.state["tracking"]

        UninstallTrigger(table="users").database_forwards(
            "myapp", schema_editor, MagicMock(), MagicMock()
        )
        assert "audit_users_trigger" not in conn.state["tracking"]

    def test_describe(self):
        op = UninstallTrigger(table="orders")
        assert "orders" in op.describe().lower()
