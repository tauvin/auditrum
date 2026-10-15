import json
from unittest.mock import MagicMock, patch

from typer.testing import CliRunner

from auditrum.cli import app
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
