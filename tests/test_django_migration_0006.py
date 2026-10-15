"""Test migration 0006: re-emit reconstruct functions for 0.7."""

import importlib
from unittest.mock import MagicMock

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

from auditrum.schema import generate_audit_reconstruct_sql  # noqa: E402


def test_0006_reemits_reconstruct_functions():
    module = importlib.import_module(
        "auditrum.integrations.django.migrations.0006_refresh_reconstruct_07"
    )
    cursor = MagicMock()
    cursor.__enter__ = MagicMock(return_value=cursor)
    cursor.__exit__ = MagicMock(return_value=False)
    schema_editor = MagicMock()
    schema_editor.connection.cursor.return_value = cursor

    module._refresh_reconstruct(MagicMock(), schema_editor)

    cursor.execute.assert_called_once_with(generate_audit_reconstruct_sql("auditlog"))


def test_0006_depends_on_0005():
    module = importlib.import_module(
        "auditrum.integrations.django.migrations.0006_refresh_reconstruct_07"
    )
    assert ("auditrum_django", "0005_model_state") in module.Migration.dependencies
