"""Cover the lazy re-exports in auditrum.integrations.django.__init__."""

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

import auditrum.integrations.django as django_pkg  # noqa: E402


class TestLazyReExports:
    def test_track_is_exposed_lazily(self):
        from auditrum.integrations.django.tracking import track as canonical

        assert django_pkg.track is canonical

    def test_register_is_exposed_lazily(self):
        from auditrum.integrations.django.tracking import register as canonical

        assert django_pkg.register is canonical

    def test_audit_log_is_exposed_lazily(self):
        from auditrum.integrations.django.models import AuditLog

        assert django_pkg.AuditLog is AuditLog

    def test_audit_context_is_exposed_lazily(self):
        from auditrum.integrations.django.models import AuditContext

        assert django_pkg.AuditContext is AuditContext

    def test_auditrum_context_is_exposed_lazily(self):
        from auditrum.integrations.django.runtime import auditrum_context

        assert django_pkg.auditrum_context is auditrum_context

    def test_current_context_is_exposed_lazily(self):
        from auditrum.integrations.django.runtime import current_context

        assert django_pkg.current_context is current_context

    def test_update_current_context_is_exposed_lazily(self):
        from auditrum.integrations.django.runtime import update_current_context

        assert django_pkg.update_current_context is update_current_context

    def test_unknown_attr_raises_attribute_error(self):
        with pytest.raises(AttributeError):
            django_pkg.this_name_does_not_exist  # noqa: B018
