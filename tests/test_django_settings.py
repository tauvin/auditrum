"""Unit tests for AuditSettings — GUC name validation, defaults, overrides."""

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

from auditrum.integrations.django.settings import (  # noqa: E402
    _validate_guc_name,
    audit_settings,
)


class TestValidateGucName:
    @pytest.mark.parametrize(
        "valid",
        [
            "auditrum.context_id",
            "myapp.user_id",
            "_internal.something",
            "abc.xyz",
            "a.b",
            "_._",
        ],
    )
    def test_accepts_valid_guc_form(self, valid):
        assert _validate_guc_name(valid, "test") == valid

    @pytest.mark.parametrize(
        "invalid",
        [
            "no_dot",
            "two.dots.here",
            "auditrum.context_id; DROP TABLE users",
            "auditrum.context_id', NULL); DROP TABLE auditlog; --",
            "1bad.start",
            "good.1bad",
            "bad-dash.value",
            "UPPERCASE.x",
            "",
            ".start",
            "end.",
            "no spaces.allowed",
        ],
    )
    def test_rejects_invalid_guc_form(self, invalid):
        with pytest.raises(ValueError, match="Invalid test"):
            _validate_guc_name(invalid, "test")

    def test_rejects_non_string(self):
        with pytest.raises(ValueError, match="Invalid test"):
            _validate_guc_name(None, "test")  # type: ignore[arg-type]


class TestAuditSettingsGucProperties:
    def test_default_guc_id(self):
        assert audit_settings.guc_id == "auditrum.context_id"

    def test_default_guc_metadata(self):
        assert audit_settings.guc_metadata == "auditrum.context_metadata"

    def test_override_via_django_settings(self, monkeypatch):
        monkeypatch.setattr(
            django_settings, "PGAUDIT_GUC_ID", "myapp.audit_id", raising=False
        )
        assert audit_settings.guc_id == "myapp.audit_id"

    def test_malicious_override_rejected(self, monkeypatch):
        """Defence in depth: even if Django settings are compromised, an
        injection attempt via PGAUDIT_GUC_ID is caught at access time."""
        monkeypatch.setattr(
            django_settings,
            "PGAUDIT_GUC_ID",
            "auditrum.context_id', NULL); DROP TABLE auditlog; --",
            raising=False,
        )
        with pytest.raises(ValueError, match="Invalid PGAUDIT_GUC_ID"):
            _ = audit_settings.guc_id

    def test_malicious_metadata_override_rejected(self, monkeypatch):
        monkeypatch.setattr(
            django_settings,
            "PGAUDIT_GUC_METADATA",
            "x.y'; DELETE FROM auditlog; --",
            raising=False,
        )
        with pytest.raises(ValueError, match="Invalid PGAUDIT_GUC_METADATA"):
            _ = audit_settings.guc_metadata
