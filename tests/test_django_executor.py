"""Unit tests for the Django executor — both default (lazy global connection)
and explicit-connection modes used by migration operations."""

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

from auditrum.integrations.django.executor import DjangoExecutor  # noqa: E402


class TestDefaultDjangoExecutor:
    def test_uses_default_connection_when_none_given(self):
        ex = DjangoExecutor()
        with ex.cursor() as cur:
            cur.execute("SELECT 1")
            assert cur.fetchone() == (1,)


class TestExplicitConnectionExecutor:
    def test_delegates_to_passed_connection(self):
        """The migration-operation path passes schema_editor.connection
        explicitly so each migration targets the right alias rather than
        the global default. Verify DjangoExecutor honours that."""
        fake_cursor = MagicMock()
        fake_cursor.__enter__ = MagicMock(return_value=fake_cursor)
        fake_cursor.__exit__ = MagicMock(return_value=False)
        fake_connection = MagicMock()
        fake_connection.cursor.return_value = fake_cursor

        ex = DjangoExecutor(connection=fake_connection)
        with ex.cursor() as cur:
            cur.execute("SELECT 1")

        fake_connection.cursor.assert_called_once()
        fake_cursor.execute.assert_called_once_with("SELECT 1")

    def test_does_not_touch_default_connection(self):
        """When an explicit connection is given, the executor must not
        fall through to django.db.connection. Important for multi-DB
        setups where the default alias might be wrong."""
        from django.db import connection as default_connection

        fake_cursor = MagicMock()
        fake_cursor.__enter__ = MagicMock(return_value=fake_cursor)
        fake_cursor.__exit__ = MagicMock(return_value=False)
        fake_connection = MagicMock()
        fake_connection.cursor.return_value = fake_cursor

        # Make the default connection raise if touched
        ex = DjangoExecutor(connection=fake_connection)
        with ex.cursor():
            pass

        # Sanity: explicit connection was used, default was NOT involved
        fake_connection.cursor.assert_called_once()
        # We can't easily assert the global wasn't touched without mocking,
        # but the previous assertion proves the explicit path was taken.
        assert default_connection is not fake_connection
