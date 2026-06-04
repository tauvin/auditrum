"""Unit tests for auditrum.integrations.django.templatetags.audit_tags."""

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

from auditrum.integrations.django.templatetags.audit_tags import (  # noqa: E402
    changed_fields,
    render_diff,
)


class TestRenderDiff:
    def test_empty_diff_returns_dash(self):
        assert render_diff({}) == "-"

    def test_none_returns_dash(self):
        assert render_diff(None) == "-"

    def test_renders_each_field_on_its_own_line(self):
        out = render_diff({"name": ["old", "new"], "status": ["a", "b"]})
        assert "name: old → new" in out
        assert "status: a → b" in out
        assert out.count("\n") == 1  # two lines, one separator

    def test_preserves_order_of_fields(self):
        out = render_diff({"a": [1, 2], "b": [3, 4]})
        assert out.index("a:") < out.index("b:")


class TestChangedFields:
    def test_hides_null_to_null(self):
        diff = {"a": {"old": None, "new": None}, "b": {"old": None, "new": "x"}}
        assert changed_fields(diff) == {"b": {"old": None, "new": "x"}}

    def test_keeps_falsy_changes(self):
        diff = {"sold": {"old": None, "new": False}, "odo": {"old": None, "new": 0}}
        assert changed_fields(diff) == diff

    def test_empty_diff_returns_empty_dict(self):
        assert changed_fields({}) == {}
        assert changed_fields(None) == {}
