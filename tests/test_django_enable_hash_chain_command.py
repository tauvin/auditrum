"""Unit tests for the ``auditrum_enable_hash_chain`` management command."""

from io import StringIO
from unittest.mock import MagicMock, patch

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
        DATABASES={
            "default": {
                "ENGINE": "django.db.backends.sqlite3",
                "NAME": ":memory:",
            }
        },
        ROOT_URLCONF="django.contrib.contenttypes.urls",
        TEMPLATES=[
            {
                "BACKEND": "django.template.backends.django.DjangoTemplates",
                "DIRS": [],
                "APP_DIRS": True,
                "OPTIONS": {"context_processors": []},
            }
        ],
        USE_TZ=True,
        TIME_ZONE="UTC",
    )
    django.setup()

from django.core.management import call_command  # noqa: E402

MODULE = "auditrum.integrations.django.management.commands.auditrum_enable_hash_chain"


class TestAuditrumEnableHashChain:
    def test_executes_generated_ddl(self):
        """The command must run the generate_hash_chain_sql DDL against
        the configured connection."""
        cursor = MagicMock()
        conn_cm = MagicMock()
        conn_cm.__enter__.return_value = cursor
        conn_cm.__exit__.return_value = False

        connection_mock = MagicMock()
        connection_mock.cursor.return_value = conn_cm

        with patch(f"{MODULE}.connection", connection_mock):
            call_command("auditrum_enable_hash_chain")

        executed = [call.args[0] for call in cursor.execute.call_args_list]
        joined = "\n".join(executed)

        assert "CREATE EXTENSION IF NOT EXISTS pgcrypto" in joined
        assert "ADD COLUMN IF NOT EXISTS row_hash" in joined
        assert "auditlog_hash_chain" in joined

    def test_uses_configured_table_name(self):
        """The DDL targets ``audit_settings.table_name``."""
        cursor = MagicMock()
        conn_cm = MagicMock()
        conn_cm.__enter__.return_value = cursor

        connection_mock = MagicMock()
        connection_mock.cursor.return_value = conn_cm

        settings_mock = MagicMock()
        settings_mock.table_name = "custom_audit"

        with (
            patch(f"{MODULE}.connection", connection_mock),
            patch(f"{MODULE}.audit_settings", settings_mock),
        ):
            call_command("auditrum_enable_hash_chain")

        joined = "\n".join(call.args[0] for call in cursor.execute.call_args_list)
        assert "ALTER TABLE custom_audit" in joined

    def test_dry_run_skips_execute(self):
        """``--dry-run`` prints the SQL but does not touch the DB."""
        cursor = MagicMock()
        conn_cm = MagicMock()
        conn_cm.__enter__.return_value = cursor

        connection_mock = MagicMock()
        connection_mock.cursor.return_value = conn_cm

        out = StringIO()
        with patch(f"{MODULE}.connection", connection_mock):
            call_command("auditrum_enable_hash_chain", "--dry-run", stdout=out)

        assert not cursor.execute.called
        assert "CREATE EXTENSION IF NOT EXISTS pgcrypto" in out.getvalue()

    def test_warns_when_setting_disabled(self):
        """Enabling DDL while PGAUDIT_HASH_CHAIN is False should warn but
        still proceed (the command is the explicit opt-in action)."""
        cursor = MagicMock()
        conn_cm = MagicMock()
        conn_cm.__enter__.return_value = cursor

        connection_mock = MagicMock()
        connection_mock.cursor.return_value = conn_cm

        settings_mock = MagicMock()
        settings_mock.table_name = "auditlog"
        settings_mock.hash_chain = False

        err = StringIO()
        with (
            patch(f"{MODULE}.connection", connection_mock),
            patch(f"{MODULE}.audit_settings", settings_mock),
        ):
            call_command("auditrum_enable_hash_chain", stderr=err)

        assert "PGAUDIT_HASH_CHAIN" in err.getvalue()
        # Still proceeded.
        assert cursor.execute.called
