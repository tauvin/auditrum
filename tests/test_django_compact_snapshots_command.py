"""Unit tests for the ``auditrum_compact_snapshots`` management command."""

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

from auditrum.compaction import CompactionResult  # noqa: E402
from auditrum.diff_chain import DiffChainMismatch  # noqa: E402

MODULE = "auditrum.integrations.django.management.commands.auditrum_compact_snapshots"


def finding(i=1):
    return DiffChainMismatch(
        table="items",
        object_id=str(i),
        audit_id=7,
        kind="live",
        column="name",
        expected="b",
        actual="X",
        detail="live value of name differs from event 7",
    )


def result(table="items", compacted=3, skipped=0, excluded=(), dry_run=False):
    return CompactionResult(table, compacted, skipped, tuple(excluded), dry_run)


class TestAuditrumCompactSnapshots:
    def test_cleared_output_and_vacuum_hint(self):
        out = StringIO()
        with patch(f"{MODULE}.compact_snapshots", return_value=result(skipped=2)) as mock:
            call_command("auditrum_compact_snapshots", "--table", "items", stdout=out)
        text = out.getvalue()
        assert "items: cleared snapshots of 3 row(s)" in text
        assert "items: kept 2 row(s) covered by the hash chain" in text
        assert "Run VACUUM (ANALYZE) auditlog" in text
        kwargs = mock.call_args.kwargs
        assert kwargs["before"] is None and kwargs["batch_size"] == 10_000
        assert kwargs["dry_run"] is False and kwargs["audit_table"] == "auditlog"

    def test_dry_run_has_no_vacuum_hint(self):
        out = StringIO()
        with patch(f"{MODULE}.compact_snapshots", return_value=result(dry_run=True)) as mock:
            call_command(
                "auditrum_compact_snapshots",
                "--table",
                "items",
                "--dry-run",
                "--older-than",
                "6 months",
                "--batch-size",
                "5",
                "--audit-table",
                "other_log",
                stdout=out,
            )
        text = out.getvalue()
        assert "items: would clear snapshots of 3 row(s)" in text
        assert "VACUUM" not in text
        kwargs = mock.call_args.kwargs
        assert kwargs["dry_run"] is True and kwargs["before"] is not None
        assert kwargs["batch_size"] == 5 and kwargs["audit_table"] == "other_log"

    def test_excluded_findings_are_listed_and_capped(self):
        out = StringIO()
        res = result(excluded=[finding(i) for i in range(12)])
        with patch(f"{MODULE}.compact_snapshots", return_value=res):
            call_command("auditrum_compact_snapshots", "--table", "items", stdout=out)
        text = out.getvalue()
        assert "items: kept 12 object(s) with diff-chain findings" in text
        assert "items:0 event 7 live name: live value of name differs from event 7" in text
        assert "items:9 event" in text and "items:10 event" not in text
        assert "… and 2 more" in text

    def test_failing_table_does_not_stop_the_others(self):
        def compact(conn, *, table, **kwargs):
            if table == "broken":
                raise ValueError("not a diff table")
            return result(table=table)

        out, err = StringIO(), StringIO()
        with (
            patch(f"{MODULE}.compact_snapshots", side_effect=compact) as mock,
            pytest.raises(CommandError, match="broken"),
        ):
            call_command(
                "auditrum_compact_snapshots",
                "--table",
                "broken",
                "--table",
                "items",
                stdout=out,
                stderr=err,
            )
        assert [c.kwargs["table"] for c in mock.call_args_list] == ["broken", "items"]
        assert "broken: not a diff table" in err.getvalue()
        assert "items: cleared snapshots" in out.getvalue()

    def test_missing_table_raises(self):
        with pytest.raises(CommandError, match="--table"):
            call_command("auditrum_compact_snapshots")

    def test_bad_older_than_raises(self):
        with (
            patch(f"{MODULE}.compact_snapshots") as mock,
            pytest.raises(CommandError),
        ):
            call_command("auditrum_compact_snapshots", "--table", "items", "--older-than", "soon")
        mock.assert_not_called()
