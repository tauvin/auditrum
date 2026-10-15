"""Unit tests for the ``auditrum_verify_diff_chain`` management command."""

from io import StringIO
from unittest.mock import patch

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

from auditrum.diff_chain import DiffChainMismatch  # noqa: E402

MODULE = "auditrum.integrations.django.management.commands.auditrum_verify_diff_chain"

FINDING = DiffChainMismatch(
    table="items",
    object_id="1",
    audit_id=7,
    kind="live",
    column="name",
    expected="b",
    actual="X",
    detail="live value of name differs from event 7",
)


class TestAuditrumVerifyDiffChain:
    def test_clean_run_succeeds(self):
        out = StringIO()
        with (
            patch(f"{MODULE}.list_registered_tables", return_value=["items"]),
            patch(f"{MODULE}.verify_diff_chain", return_value=[]) as verify,
        ):
            call_command("auditrum_verify_diff_chain", stdout=out)
        assert "diff chain OK" in out.getvalue()
        assert verify.call_args.kwargs["since"] is not None

    def test_findings_raise_command_error(self):
        err = StringIO()
        with (
            patch(f"{MODULE}.list_registered_tables", return_value=["items"]),
            patch(f"{MODULE}.verify_diff_chain", return_value=[FINDING]),
            pytest.raises(CommandError, match="1 finding"),
        ):
            call_command("auditrum_verify_diff_chain", stderr=err)
        assert "live value of name differs from event 7" in err.getvalue()

    def test_options_are_passed_through(self):
        with patch(f"{MODULE}.verify_diff_chain", return_value=[]) as verify:
            call_command(
                "auditrum_verify_diff_chain",
                "--table",
                "a",
                "--full",
                "--object-id",
                "5",
                "--limit",
                "3",
            )
        kwargs = verify.call_args.kwargs
        assert kwargs["table"] == "a"
        assert kwargs["full"] is True and kwargs["since"] is None
        assert kwargs["object_id"] == "5" and kwargs["limit"] == 3

    def test_json_output(self):
        out = StringIO()
        with (
            patch(f"{MODULE}.verify_diff_chain", return_value=[FINDING]),
            pytest.raises(CommandError),
        ):
            call_command("auditrum_verify_diff_chain", "--table", "a", "--json", stdout=out)
        assert '"kind": "live"' in out.getvalue()
