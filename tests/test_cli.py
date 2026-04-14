from typer.testing import CliRunner

from auditrum.cli import app


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
