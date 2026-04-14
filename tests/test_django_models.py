"""Unit tests for AuditLogQuerySet / AuditLogManager.

These tests don't touch a real Postgres — they use Django's in-memory SQLite
backend and drive the AuditLog model as ``managed=False``. The queryset
helpers are pure ORM filters so they're verifiable without running triggers.
"""

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

from auditrum.integrations.django.models import (  # noqa: E402
    AuditLog,
    AuditLogManager,
    AuditLogQuerySet,
)


class TestAuditLogManager:
    def test_is_instance_on_model(self):
        assert isinstance(AuditLog.objects, AuditLogManager)

    def test_returns_auditlog_queryset(self):
        qs = AuditLog.objects.all()
        assert isinstance(qs, AuditLogQuerySet)


class TestForModel:
    def test_filters_by_db_table(self):
        class FakeMeta:
            db_table = "users"

        model_cls = MagicMock()
        model_cls._meta = FakeMeta

        sql = str(AuditLog.objects.for_model(model_cls).query)
        assert 'auditlog"."table_name" = users' in sql or "table_name = 'users'" in sql.replace(
            '"', ""
        )


class TestForObject:
    def test_filters_by_table_and_object_id(self):
        class FakeMeta:
            db_table = "users"

        instance = MagicMock()
        instance._meta = FakeMeta
        instance.pk = 42
        # for_object calls type(instance) — bypass by patching
        original_for_model = AuditLog.objects.for_model
        captured = {}

        def patched_for_model(model_cls):
            captured["model_cls"] = model_cls
            return original_for_model.__func__(AuditLog.objects.get_queryset(), model_cls)

        # Simpler: stub `type(instance)` via a real subclass trick. Use a
        # lightweight class instead.
        class Stub:
            class _meta:
                db_table = "users"

            pk = 42

        sql = str(AuditLog.objects.for_object(Stub()).query)
        assert "users" in sql
        assert "42" in sql


class TestForUser:
    def test_user_instance_with_pk(self):
        class FakeUser:
            pk = 7

        sql = str(AuditLog.objects.for_user(FakeUser()).query)
        assert "user_id" in sql
        assert "7" in sql

    def test_raw_int(self):
        sql = str(AuditLog.objects.for_user(99).query)
        assert "99" in sql

    def test_none_matches_isnull(self):
        sql = str(AuditLog.objects.for_user(None).query)
        assert "IS NULL" in sql


class TestForContext:
    def test_filters_by_context_id(self):
        ctx = "00000000-0000-0000-0000-000000000001"
        sql = str(AuditLog.objects.for_context(ctx).query)
        assert "context_id" in sql
        # Django's SQLite backend strips hyphens from UUID literals
        assert ctx.replace("-", "") in sql or ctx in sql


class TestByTable:
    def test_filters_by_raw_table_name(self):
        sql = str(AuditLog.objects.by_table("payments").query)
        assert "payments" in sql


class TestRecent:
    def test_orders_desc_and_limits(self):
        qs = AuditLog.objects.recent(limit=5)
        sql = str(qs.query)
        assert "ORDER BY" in sql
        assert "changed_at" in sql
        assert "DESC" in sql
        assert "LIMIT 5" in sql


class TestChaining:
    def test_for_user_then_for_model(self):
        class FakeUser:
            pk = 1

        class FakeMeta:
            db_table = "orders"

        model_cls = type("M", (), {"_meta": FakeMeta})
        qs = AuditLog.objects.for_user(FakeUser()).for_model(model_cls)
        sql = str(qs.query)
        assert "orders" in sql
        assert "user_id" in sql
        assert "1" in sql
