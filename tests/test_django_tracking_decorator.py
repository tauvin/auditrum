"""Unit tests for the @track decorator + auditrum_makemigrations command."""

from io import StringIO

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

from django.core.management import call_command  # noqa: E402

from auditrum.integrations.django.tracking import (  # noqa: E402
    clear_registry,
    get_registered_specs,
    track,
)


@pytest.fixture(autouse=True)
def _reset_registry():
    clear_registry()
    yield
    clear_registry()


class TestTrackDecorator:
    def test_registers_spec_on_class(self):
        from django.db import models

        @track(fields=["username", "email"])
        class User2(models.Model):
            username = models.CharField(max_length=32)
            email = models.CharField(max_length=64)

            class Meta:
                app_label = "tests"

        specs = get_registered_specs()
        assert len(specs) == 1
        _, spec = specs[0]
        assert spec.table == User2._meta.db_table
        assert spec.fields.kind == "only"
        assert spec.fields.fields == ("username", "email")
        assert User2.audit_spec == spec

    def test_exclude_mode(self):
        from django.db import models

        @track(exclude=["password"])
        class Account(models.Model):
            password = models.CharField(max_length=64)

            class Meta:
                app_label = "tests"

        _, spec = get_registered_specs()[0]
        assert spec.fields.kind == "exclude"
        assert spec.fields.fields == ("password",)

    def test_fields_and_exclude_together_rejected(self):
        with pytest.raises(ValueError, match="cannot pass both"):

            @track(fields=["a"], exclude=["b"])
            class Bad:  # pragma: no cover
                class Meta:
                    app_label = "tests"

    def test_extra_meta_captured(self):
        from django.db import models

        @track(extra_meta=["tenant_id"])
        class Item(models.Model):
            tenant_id = models.IntegerField()

            class Meta:
                app_label = "tests"

        _, spec = get_registered_specs()[0]
        assert spec.extra_meta_fields == ("tenant_id",)

    def test_trigger_name_override(self):
        from django.db import models

        @track(trigger_name="custom_trig")
        class Thing(models.Model):
            class Meta:
                app_label = "tests"

        _, spec = get_registered_specs()[0]
        assert spec.trigger_name == "custom_trig"
        assert spec.effective_trigger_name == "custom_trig"


class TestAuditrumMakemigrationsCommand:
    def test_dry_run_outputs_migration_content(self):
        from django.contrib.auth.models import User

        track(fields=["username"])(User)

        out = StringIO()
        call_command("auditrum_makemigrations", "--dry-run", stdout=out)
        content = out.getvalue()
        assert "InstallTrigger" in content
        assert "class Migration" in content
        assert "from auditrum.integrations.django.operations import InstallTrigger" in content
        assert "table='auth_user'" in content
        assert "fields_kind='only'" in content
        assert "fields=['username']" in content

    def test_dry_run_no_specs_registered(self):
        out = StringIO()
        call_command("auditrum_makemigrations", "--dry-run", stdout=out)
        assert "No @track-decorated models" in out.getvalue()

    def test_dependencies_include_auditrum_initial(self):
        from django.contrib.auth.models import User

        track()(User)
        out = StringIO()
        call_command("auditrum_makemigrations", "--dry-run", stdout=out)
        content = out.getvalue()
        assert "auditrum_django" in content
        assert "0001_initial" in content

    def test_custom_name_suffix(self):
        from django.contrib.auth.models import User

        track()(User)
        out = StringIO()
        call_command("auditrum_makemigrations", "--dry-run", "--name", "my_suffix", stdout=out)
        assert "my_suffix" in out.getvalue()


class TestAuditrumMakemigrationsLoadability:
    """The dry-run substring tests above verify the *string content* of
    generated migrations. These tests verify the generated file actually
    parses as Python and re-imports as a real Django Migration class —
    catching trailing-comma bugs, unquoted-string bugs, and any other
    string-concat fragility in the generator that wouldn't surface in
    a dry-run substring match."""

    def _patch_app_to_tmp_dir(self, monkeypatch, tmp_path):
        from django.apps import apps as django_apps

        auth_config = django_apps.get_app_config("auth")
        monkeypatch.setattr(auth_config, "path", str(tmp_path))
        migrations_dir = tmp_path / "migrations"
        migrations_dir.mkdir(exist_ok=True)
        (migrations_dir / "__init__.py").write_text("", encoding="utf-8")
        return migrations_dir

    def _load_module(self, path):
        import importlib.util

        spec = importlib.util.spec_from_file_location(
            "_auditrum_test_generated_migration", str(path)
        )
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def test_generated_file_parses_as_python(self, tmp_path, monkeypatch):
        from django.contrib.auth.models import User

        track(fields=["username", "email"])(User)
        migrations_dir = self._patch_app_to_tmp_dir(monkeypatch, tmp_path)

        out = StringIO()
        call_command(
            "auditrum_makemigrations", "--name", "loadable_test", stdout=out
        )

        files = list(migrations_dir.glob("*_loadable_test.py"))
        assert len(files) == 1, f"expected one generated file, got {files}"
        content = files[0].read_text(encoding="utf-8")

        # Must compile to bytecode without syntax errors
        compile(content, str(files[0]), "exec")

    def test_generated_file_imports_and_exposes_migration(
        self, tmp_path, monkeypatch
    ):
        from django.contrib.auth.models import User

        track(fields=["username"])(User)
        migrations_dir = self._patch_app_to_tmp_dir(monkeypatch, tmp_path)

        out = StringIO()
        call_command("auditrum_makemigrations", "--name", "import_test", stdout=out)

        files = list(migrations_dir.glob("*_import_test.py"))
        module = self._load_module(files[0])

        # Must expose a Migration class with one InstallTrigger operation
        assert hasattr(module, "Migration"), "no Migration class in generated file"
        migration = module.Migration

        from auditrum.integrations.django.operations import InstallTrigger

        assert len(migration.operations) == 1
        op = migration.operations[0]
        assert isinstance(op, InstallTrigger)
        assert op.spec.table == "auth_user"
        assert op.spec.fields.kind == "only"
        assert op.spec.fields.fields == ("username",)

    def test_log_condition_with_quotes_round_trips(self, tmp_path, monkeypatch):
        """Tricky: log_condition can contain single quotes and other
        Python-meaningful characters. repr() must escape them so the
        generated file is still loadable."""
        from django.contrib.auth.models import User

        track(log_condition="NEW.username <> 'system' AND NEW.is_active")(User)
        migrations_dir = self._patch_app_to_tmp_dir(monkeypatch, tmp_path)

        out = StringIO()
        call_command("auditrum_makemigrations", "--name", "quoted_test", stdout=out)

        files = list(migrations_dir.glob("*_quoted_test.py"))
        module = self._load_module(files[0])

        op = module.Migration.operations[0]
        assert op.spec.log_condition == "NEW.username <> 'system' AND NEW.is_active"

    def test_extra_meta_fields_round_trip(self, tmp_path, monkeypatch):
        from django.contrib.auth.models import User

        track(extra_meta=["last_login", "date_joined"])(User)
        migrations_dir = self._patch_app_to_tmp_dir(monkeypatch, tmp_path)

        out = StringIO()
        call_command(
            "auditrum_makemigrations", "--name", "extra_meta_test", stdout=out
        )

        files = list(migrations_dir.glob("*_extra_meta_test.py"))
        module = self._load_module(files[0])

        op = module.Migration.operations[0]
        assert op.spec.extra_meta_fields == ("last_login", "date_joined")
