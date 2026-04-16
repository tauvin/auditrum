"""Unit test for the ``auditrum_makemigrations``-adjacent
``audit_add_partitions`` management command.

We mock ``psycopg.connect`` so the test runs without a live database;
the generation path (``generate_auditlog_partitions_sql``) is exercised
and the ``# ty: ignore[invalid-argument-type]`` trust-boundary line is
verified to call ``cursor.execute`` with the generated SQL.
"""

from unittest.mock import MagicMock, patch

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
        DATABASES={
            "default": {
                "ENGINE": "django.db.backends.postgresql",
                "NAME": "audit_db",
                "USER": "audit_user",
                "PASSWORD": "pw",
                "HOST": "db.internal",
                "PORT": "5432",
            }
        },
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


class TestAuditAddPartitions:
    def test_invokes_psycopg_execute_with_generated_sql(self):
        cursor = MagicMock()
        conn = MagicMock()
        conn.__enter__.return_value = conn
        conn.cursor.return_value.__enter__.return_value = cursor

        with patch("psycopg.connect", return_value=conn) as mock_connect:
            call_command("audit_add_partitions", "--months", "3")

        # Don't pin the exact DSN — Django settings may have been
        # configured by another test in the process. We just confirm the
        # command actually reached psycopg.connect and dispatched SQL
        # through the mocked cursor.
        assert mock_connect.called
        assert cursor.execute.called
        sql = cursor.execute.call_args[0][0]
        assert "PARTITION" in sql.upper()
