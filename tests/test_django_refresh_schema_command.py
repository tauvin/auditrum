"""Unit tests for the ``auditrum_refresh_schema`` management command."""

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


class TestAuditrumRefreshSchema:
    def test_executes_all_four_helpers(self):
        """The command must re-emit ``jsonb_diff``,
        ``_audit_attach_context``, ``_audit_current_user_id``, and
        the ``_audit_reconstruct_*`` pair. That's the full set whose
        bodies are version-dependent — anything missing here means
        a release that changes e.g. ``_audit_attach_context`` won't
        self-heal on ``migrate`` / ``auditrum_refresh_schema``.
        """
        cursor = MagicMock()
        conn_cm = MagicMock()
        conn_cm.__enter__.return_value = cursor
        conn_cm.__exit__.return_value = False

        connection_mock = MagicMock()
        connection_mock.cursor.return_value = conn_cm

        with patch(
            "auditrum.integrations.django.management.commands.auditrum_refresh_schema.connection",
            connection_mock,
        ):
            call_command("auditrum_refresh_schema")

        executed = [call.args[0] for call in cursor.execute.call_args_list]
        joined = "\n".join(executed)

        assert "CREATE OR REPLACE FUNCTION jsonb_diff" in joined
        assert "CREATE OR REPLACE FUNCTION _audit_attach_context" in joined
        assert "CREATE OR REPLACE FUNCTION _audit_current_user_id" in joined
        assert "CREATE OR REPLACE FUNCTION _audit_reconstruct_row" in joined
        assert "CREATE OR REPLACE FUNCTION _audit_reconstruct_table" in joined

    def test_dry_run_skips_execute(self):
        """``--dry-run`` prints the SQL but does not touch the DB.
        Useful for CI dry-validation and for users verifying what
        a refresh would do before running it against production.
        """
        cursor = MagicMock()
        conn_cm = MagicMock()
        conn_cm.__enter__.return_value = cursor

        connection_mock = MagicMock()
        connection_mock.cursor.return_value = conn_cm

        with patch(
            "auditrum.integrations.django.management.commands.auditrum_refresh_schema.connection",
            connection_mock,
        ):
            call_command("auditrum_refresh_schema", "--dry-run")

        # Dry run must not invoke execute() at all.
        assert not cursor.execute.called

    def test_jsonb_diff_in_dry_run_uses_paired_shape(self):
        """Guards against a future release accidentally shipping the
        pre-0.4 jsonb_diff body — the whole point of the migration is
        to guarantee the paired shape survives an upgrade.
        """
        from io import StringIO

        out = StringIO()
        call_command("auditrum_refresh_schema", "--dry-run", stdout=out)

        output = out.getvalue()
        assert "jsonb_build_object('old', old -> key, 'new', value)" in output
        assert "jsonb_object_agg(key, value)\n" not in output
