import dataclasses
import json
from datetime import datetime
from unittest.mock import MagicMock, patch

from typer.testing import CliRunner

from auditrum.cli import app
from auditrum.compaction import CompactionResult
from auditrum.diff_chain import DiffChainMismatch


class TestCli:
    def setup_method(self):
        self.runner = CliRunner()

    def test_help(self):
        result = self.runner.invoke(app, ["--help"])
        assert result.exit_code == 0
        assert "generate-trigger" in result.stdout
        assert "init-schema" in result.stdout
        assert "create-partitions" in result.stdout
        assert "revert" in result.stdout
        assert "status" in result.stdout

    def test_init_schema_dry_run_no_db(self):
        result = self.runner.invoke(app, ["init-schema", "--dry-run"])
        assert result.exit_code == 0
        assert "CREATE TABLE IF NOT EXISTS auditlog" in result.stdout
        assert "PARTITION OF auditlog DEFAULT" in result.stdout

    def test_generate_trigger_dry_run_no_db(self):
        result = self.runner.invoke(app, ["generate-trigger", "users", "--dry-run"])
        assert result.exit_code == 0
        assert "CREATE TRIGGER audit_users_trigger" in result.stdout

    def test_create_partitions_dry_run_no_db(self):
        result = self.runner.invoke(app, ["create-partitions", "--months", "2", "--dry-run"])
        assert result.exit_code == 0
        assert result.stdout.count("PARTITION OF auditlog") == 2


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


class TestVerifyDiffChainCommand:
    def setup_method(self):
        self.runner = CliRunner()

    def _invoke(self, args, findings=(), tables=("items",), side_effect=None):
        with (
            patch("auditrum.cli.connect") as connect,
            patch("auditrum.cli.list_registered_tables", return_value=list(tables)) as listed,
            patch(
                "auditrum.cli.verify_diff_chain",
                return_value=list(findings),
                side_effect=side_effect,
            ) as verify,
        ):
            self.conn = MagicMock()
            connect.return_value.__enter__.return_value = self.conn
            result = self.runner.invoke(
                app, ["verify-diff-chain", "--db-dsn", "postgresql://x", *args]
            )
        return result, listed, verify

    def test_clean_exits_zero(self):
        result, _, verify = self._invoke([])
        assert result.exit_code == 0
        assert verify.call_args.kwargs["table"] == "items"
        assert verify.call_args.kwargs["since"] is not None
        assert verify.call_args.kwargs["full"] is False

    def test_findings_exit_one(self):
        result, _, _ = self._invoke([], findings=[FINDING])
        assert result.exit_code == 1
        assert "live value of name differs from event 7" in result.stdout

    def test_human_output_escapes_markup(self):
        finding = dataclasses.replace(FINDING, object_id="[bold]1[/bold]", detail="x [/red] y")
        result, _, _ = self._invoke([], findings=[finding])
        assert result.exit_code == 1
        assert "items:[bold]1[/bold]" in result.output
        assert "x [/red] y" in result.output

    def test_json_output(self):
        result, _, _ = self._invoke(["--json"], findings=[FINDING])
        assert result.exit_code == 1
        [row] = json.loads(result.stdout)
        assert row["kind"] == "live" and row["object_id"] == "1" and row["actual"] == "X"

    def test_explicit_tables_skip_registry_listing(self):
        result, listed, verify = self._invoke(["--table", "a", "--table", "b"])
        assert result.exit_code == 0
        listed.assert_not_called()
        assert [c.kwargs["table"] for c in verify.call_args_list] == ["a", "b"]

    def test_full_passes_no_since(self):
        result, _, verify = self._invoke(["--full"])
        assert result.exit_code == 0
        assert verify.call_args.kwargs["full"] is True
        assert verify.call_args.kwargs["since"] is None

    def test_limit_caps_total(self):
        result, _, _ = self._invoke(
            ["--table", "a", "--table", "b", "--limit", "1", "--json"], findings=[FINDING]
        )
        assert len(json.loads(result.stdout)) == 1

    def test_error_exits_two(self):
        with (
            patch("auditrum.cli.connect", side_effect=RuntimeError("boom")),
        ):
            result = self.runner.invoke(app, ["verify-diff-chain", "--db-dsn", "postgresql://x"])
        assert result.exit_code == 2

    def test_failing_table_does_not_stop_the_others(self):
        def verify(conn, *, table, **kwargs):
            if table == "broken":
                raise RuntimeError("column t.id does not exist")
            return [FINDING]

        result, _, mock = self._invoke([], tables=("broken", "items"), side_effect=verify)
        assert result.exit_code == 2
        assert [c.kwargs["table"] for c in mock.call_args_list] == ["broken", "items"]
        assert "live value of name differs from event 7" in result.output
        assert "broken" in result.output
        self.conn.rollback.assert_called_once()

    def test_failing_table_keeps_json_stdout_valid(self):
        def verify(conn, *, table, **kwargs):
            if table == "broken":
                raise RuntimeError("column t.id does not exist")
            return [FINDING]

        self.runner = CliRunner(mix_stderr=False)
        result, _, _ = self._invoke(["--json"], tables=("broken", "items"), side_effect=verify)
        assert result.exit_code == 2
        [row] = json.loads(result.stdout)
        assert row["object_id"] == "1"
        assert "broken" in result.stderr and "column t.id does not exist" in result.stderr

    def test_empty_registry_exits_two(self):
        result, _, verify = self._invoke([], tables=())
        assert result.exit_code == 2
        assert (
            "no tracked tables registered for audit table auditlog — nothing checked"
            in result.output
        )
        verify.assert_not_called()

    def test_bad_since_exits_two(self):
        result, _, _ = self._invoke(["--since", "whenever"])
        assert result.exit_code == 2


def _result(table="items", compacted=3, skipped_hashed=0, excluded=(), dry_run=False):
    return CompactionResult(
        table=table,
        compacted=compacted,
        skipped_hashed=skipped_hashed,
        excluded=tuple(excluded),
        dry_run=dry_run,
    )


class TestCompactSnapshotsCommand:
    def setup_method(self):
        self.runner = CliRunner()

    def _invoke(self, args, results=None, side_effect=None):
        with (
            patch("auditrum.cli.connect") as connect,
            patch(
                "auditrum.cli.compact_snapshots",
                return_value=results if results is not None else _result(),
                side_effect=side_effect,
            ) as compact,
        ):
            connect.return_value.__enter__.return_value = MagicMock()
            result = self.runner.invoke(
                app, ["compact-snapshots", "--db-dsn", "postgresql://x", *args]
            )
        self.connect = connect
        return result, compact

    def test_table_is_required(self):
        result, _ = self._invoke([])
        assert result.exit_code == 2

    def test_no_dsn_exits_two(self):
        with patch("auditrum.cli.get_db_dsn", return_value=None):
            result = self.runner.invoke(app, ["compact-snapshots", "--table", "items"])
        assert result.exit_code == 2

    def test_bad_older_than_exits_two(self):
        result, compact = self._invoke(["--table", "items", "--older-than", "bogus"])
        assert result.exit_code == 2
        compact.assert_not_called()

    def test_success(self):
        result, compact = self._invoke(["--table", "items"])
        assert result.exit_code == 0
        assert "items: cleared snapshots of 3 row(s)" in result.output
        assert "VACUUM (ANALYZE) auditlog" in result.output
        assert self.connect.call_args.kwargs["autocommit"] is True
        assert compact.call_args.kwargs["batch_size"] == 10_000
        assert compact.call_args.kwargs["before"] is None

    def test_dry_run(self):
        result, compact = self._invoke(
            ["--table", "items", "--dry-run"], results=_result(dry_run=True)
        )
        assert result.exit_code == 0
        assert "items: would clear snapshots of 3 row(s)" in result.output
        assert "VACUUM" not in result.output
        assert compact.call_args.kwargs["dry_run"] is True

    def test_hashed_rows_reported(self):
        result, _ = self._invoke(["--table", "items"], results=_result(skipped_hashed=2))
        assert "items: kept 2 row(s) covered by the hash chain" in result.output

    def test_excluded_findings_capped_at_ten(self):
        findings = [dataclasses.replace(FINDING, object_id=str(i)) for i in range(12)]
        result, _ = self._invoke(["--table", "items"], results=_result(excluded=findings))
        assert result.exit_code == 0
        assert "items: kept 12 object(s) with diff-chain findings" in result.output
        assert "items:9 event 7 live name: live value of name differs from event 7" in result.output
        assert "items:10 event 7" not in result.output
        assert "and 2 more" in result.output

    def test_failing_table_does_not_stop_the_others(self):
        def compact(conn, *, table, **kwargs):
            if table == "broken":
                raise ValueError("not a diff table")
            return _result(table=table)

        result, mock = self._invoke(["--table", "broken", "--table", "items"], side_effect=compact)
        assert result.exit_code == 2
        assert [c.kwargs["table"] for c in mock.call_args_list] == ["broken", "items"]
        assert "items: cleared snapshots of 3 row(s)" in result.output
        assert "broken" in result.output

    def test_older_than_passes_datetime(self):
        result, compact = self._invoke(["--table", "items", "--older-than", "6 months"])
        assert result.exit_code == 0
        assert isinstance(compact.call_args.kwargs["before"], datetime)

    def test_batch_size_passed_through(self):
        _, compact = self._invoke(["--table", "items", "--batch-size", "5"])
        assert compact.call_args.kwargs["batch_size"] == 5
