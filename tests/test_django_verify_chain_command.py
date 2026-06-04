"""Unit tests for the ``auditrum_verify_chain`` management command."""

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

from django.core.management import CommandError, call_command  # noqa: E402

MODULE = "auditrum.integrations.django.management.commands.auditrum_verify_chain"


EMPTY_TIP = {"id": None, "chain_seq": None, "row_hash": None, "changed_at": None}


class TestAuditrumVerifyChain:
    @pytest.fixture(autouse=True)
    def _mock_tip(self):
        """get_chain_tip hits the DB; stub it out for these unit tests."""
        with patch(f"{MODULE}.get_chain_tip", return_value=EMPTY_TIP):
            yield

    def test_intact_chain_exits_zero(self):
        """An intact chain prints a success line and does not raise."""
        out = StringIO()
        result = {"checked": 12, "ok": True, "broken": []}

        with patch(f"{MODULE}.verify_chain", return_value=result) as mock_verify:
            call_command("auditrum_verify_chain", stdout=out)

        assert mock_verify.called
        assert "OK" in out.getvalue().upper()
        assert "12" in out.getvalue()

    def test_prints_chain_tip(self):
        """The current chain tip is reported for external anchoring."""
        out = StringIO()
        result = {"checked": 1, "ok": True, "broken": []}
        tip = {
            "id": 7,
            "chain_seq": 7,
            "row_hash": "deadbeef",
            "changed_at": None,
        }

        with (
            patch(f"{MODULE}.verify_chain", return_value=result),
            patch(f"{MODULE}.get_chain_tip", return_value=tip),
        ):
            call_command("auditrum_verify_chain", stdout=out)

        output = out.getvalue()
        assert "deadbeef" in output
        assert "id=7" in output

    def test_broken_chain_raises_command_error(self):
        """A broken chain must exit non-zero (CommandError) so cron /
        monitoring can detect tampering."""
        result = {
            "checked": 5,
            "ok": False,
            "broken": [(3, "row_hash mismatch")],
        }

        with (
            patch(f"{MODULE}.verify_chain", return_value=result),
            pytest.raises(CommandError),
        ):
            call_command("auditrum_verify_chain")

    def test_broken_chain_reports_rows(self):
        """The failure output names the broken rows and reasons."""
        result = {
            "checked": 5,
            "ok": False,
            "broken": [(3, "row_hash mismatch"), (4, "prev_hash mismatch")],
        }

        err = StringIO()
        with (
            patch(f"{MODULE}.verify_chain", return_value=result),
            pytest.raises(CommandError),
        ):
            call_command("auditrum_verify_chain", stderr=err)

        output = err.getvalue()
        assert "row_hash mismatch" in output
        assert "prev_hash mismatch" in output

    def test_uses_configured_table_name(self):
        """verify_chain is called with ``audit_settings.table_name``."""
        result = {"checked": 0, "ok": True, "broken": []}
        settings_mock = MagicMock()
        settings_mock.table_name = "custom_audit"

        with (
            patch(f"{MODULE}.verify_chain", return_value=result) as mock_verify,
            patch(f"{MODULE}.audit_settings", settings_mock),
        ):
            call_command("auditrum_verify_chain")

        assert mock_verify.call_args.args[1] == "custom_audit"
