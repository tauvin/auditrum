"""Tests for trigger change detection (autodetector, auditrum_makemigrations, W001).

Migration history is supplied through a hand-built :class:`MigrationGraph`
so each test controls exactly which InstallTrigger / UninstallTrigger
operations the "applied" migrations contain.
"""

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

from django.contrib.auth.models import Group, User  # noqa: E402
from django.core.management import call_command  # noqa: E402
from django.db import migrations  # noqa: E402
from django.db.migrations.graph import MigrationGraph  # noqa: E402

from auditrum.integrations.django import autodetector  # noqa: E402
from auditrum.integrations.django.autodetector import (  # noqa: E402
    ChangeKind,
    detect_changes,
    migrated_trigger_state,
)
from auditrum.integrations.django.checks import check_trigger_migrations  # noqa: E402
from auditrum.integrations.django.operations import (  # noqa: E402
    InstallTrigger,
    UninstallTrigger,
)
from auditrum.integrations.django.tracking import clear_registry, track  # noqa: E402
from auditrum.tracking import FieldFilter, TrackSpec  # noqa: E402


class _FakeLoader:
    def __init__(self, *ops_per_migration):
        self.graph = MigrationGraph()
        prev = None
        for i, ops in enumerate(ops_per_migration, start=1):
            key = ("auth", f"{i:04d}_auditrum")
            migration = migrations.Migration(key[1], key[0])
            migration.operations = list(ops)
            self.graph.add_node(key, migration)
            if prev is not None:
                self.graph.add_dependency(migration, key, prev)
            prev = key


def _checksum(**kwargs):
    return InstallTrigger(**kwargs).spec.build().checksum


def _install(**kwargs):
    return InstallTrigger(**kwargs, checksum=_checksum(**kwargs))


@pytest.fixture(autouse=True)
def _reset_registry():
    clear_registry()
    yield
    clear_registry()


@pytest.fixture
def use_loader(monkeypatch):
    def _use(*ops_per_migration):
        loader = _FakeLoader(*ops_per_migration)
        monkeypatch.setattr(autodetector, "MigrationLoader", lambda *a, **kw: loader)
        return loader

    return _use


class TestMigratedTriggerState:
    def test_replays_install_then_uninstall(self):
        loader = _FakeLoader(
            [_install(table="auth_user"), _install(table="auth_group")],
            [UninstallTrigger(table="auth_group")],
        )
        state = migrated_trigger_state(loader)
        assert set(state) == {"audit_auth_user_trigger"}
        assert state["audit_auth_user_trigger"].app_label == "auth"

    def test_later_install_overrides_earlier(self):
        loader = _FakeLoader(
            [_install(table="auth_user")],
            [_install(table="auth_user", fields_kind="exclude", fields=["password"])],
        )
        spec = migrated_trigger_state(loader)["audit_auth_user_trigger"].spec
        assert spec.fields == FieldFilter.exclude("password")


class TestDetectChanges:
    def test_new_spec_is_install(self):
        track(exclude=["password"])(User)
        changes = detect_changes(_FakeLoader())
        [change] = changes["auth"]
        assert change.kind is ChangeKind.INSTALL
        assert change.spec.fields == FieldFilter.exclude("password")

    def test_unchanged_spec_is_no_change(self):
        track(exclude=["password"])(User)
        loader = _FakeLoader(
            [_install(table="auth_user", fields_kind="exclude", fields=["password"])]
        )
        assert detect_changes(loader) == {}

    def test_changed_exclude_list_is_update(self):
        track(exclude=["password", "last_login"])(User)
        loader = _FakeLoader(
            [_install(table="auth_user", fields_kind="exclude", fields=["password"])]
        )
        [change] = detect_changes(loader)["auth"]
        assert change.kind is ChangeKind.UPDATE
        assert change.reason == "spec changed"
        assert change.spec.fields == FieldFilter.exclude("password", "last_login")

    def test_changed_trigger_body_is_update(self):
        track()(User)
        loader = _FakeLoader([InstallTrigger(table="auth_user", checksum="stale")])
        [change] = detect_changes(loader)["auth"]
        assert change.kind is ChangeKind.UPDATE
        assert change.reason == "trigger body changed"

    def test_missing_checksum_is_update(self):
        track()(User)
        loader = _FakeLoader([InstallTrigger(table="auth_user")])
        [change] = detect_changes(loader)["auth"]
        assert change.kind is ChangeKind.UPDATE
        assert change.reason == "no checksum recorded"

    def test_untracked_model_is_uninstall(self):
        loader = _FakeLoader([_install(table="auth_user", fields_kind="only", fields=["email"])])
        [change] = detect_changes(loader)["auth"]
        assert change.kind is ChangeKind.UNINSTALL
        assert change.spec.fields == FieldFilter.only("email")

    def test_renamed_trigger_uninstalls_old_before_installing_new(self):
        track(trigger_name="user_audit")(User)
        loader = _FakeLoader([_install(table="auth_user")])
        changes = detect_changes(loader)["auth"]
        assert [(c.kind, c.trigger_name) for c in changes] == [
            (ChangeKind.UNINSTALL, "audit_auth_user_trigger"),
            (ChangeKind.INSTALL, "user_audit"),
        ]


class TestMakemigrationsCommand:
    def test_writes_only_changed_specs(self, use_loader):
        track(exclude=["password", "last_login"])(User)
        track()(Group)
        use_loader(
            [
                _install(table="auth_user", fields_kind="exclude", fields=["password"]),
                _install(table="auth_group"),
            ]
        )
        out = StringIO()
        call_command("auditrum_makemigrations", "--dry-run", stdout=out)
        content = out.getvalue()
        assert "table='auth_user'" in content
        assert "fields=['password', 'last_login']" in content
        assert (
            f"checksum='{_checksum(table='auth_user', fields_kind='exclude', fields=['password', 'last_login'])}'"
            in content
        )
        assert "auth_group" not in content

    def test_uninstall_rendered(self, use_loader):
        use_loader([_install(table="auth_user")])
        out = StringIO()
        call_command("auditrum_makemigrations", "--dry-run", stdout=out)
        content = out.getvalue()
        assert "import UninstallTrigger" in content
        assert "UninstallTrigger(\n            table='auth_user'," in content

    def test_no_changes(self, use_loader):
        track()(User)
        use_loader([_install(table="auth_user")])
        out = StringIO()
        call_command("auditrum_makemigrations", "--check", stdout=out)
        assert "No audit trigger changes detected" in out.getvalue()

    def test_check_exits_nonzero_on_changes(self, use_loader):
        track(exclude=["password"])(User)
        use_loader([_install(table="auth_user")])
        with pytest.raises(SystemExit) as exc:
            call_command("auditrum_makemigrations", "--check", stdout=StringIO())
        assert exc.value.code == 1


class TestSystemCheck:
    def test_warns_when_migrations_missing(self, use_loader):
        track(exclude=["password"])(User)
        use_loader([_install(table="auth_user")])
        [warning] = check_trigger_migrations()
        assert warning.id == "auditrum.W001"
        assert "audit_auth_user_trigger" in warning.msg

    def test_silent_when_up_to_date(self, use_loader):
        track()(User)
        use_loader([_install(table="auth_user")])
        assert check_trigger_migrations() == []


def test_trackspec_equality_covers_all_migration_kwargs():
    """detect_changes relies on TrackSpec ``==``; guard against a field being
    dropped from equality (e.g. ``compare=False``) and edits going unnoticed."""
    base = TrackSpec(table="t")
    assert base != TrackSpec(table="t", audit_table="other")
    assert base != TrackSpec(table="t", fields=FieldFilter.exclude("a"))
    assert base != TrackSpec(table="t", extra_meta_fields=("a",))
    assert base != TrackSpec(table="t", log_condition="TRUE")
    assert base != TrackSpec(table="t", trigger_name="x")
