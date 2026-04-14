"""Unit tests for AuditedModelMixin."""

from datetime import UTC, datetime
from unittest.mock import patch

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

from auditrum.integrations.django.mixins import AuditedModelMixin  # noqa: E402
from auditrum.integrations.django.models import AuditLogQuerySet  # noqa: E402


class TestAuditedModelMixin:
    def test_audit_events_returns_for_object_queryset(self):
        class FakeMeta:
            db_table = "orders"

        class Order(AuditedModelMixin):
            _meta = FakeMeta
            pk = 99

        order = Order()
        qs = order.audit_events
        assert isinstance(qs, AuditLogQuerySet)
        sql = str(qs.query)
        assert "orders" in sql
        assert "99" in sql

    def test_audit_history_class_level(self):
        class FakeMeta:
            db_table = "invoices"

        class Invoice(AuditedModelMixin):
            _meta = FakeMeta

        qs = Invoice.audit_history()
        assert isinstance(qs, AuditLogQuerySet)
        sql = str(qs.query)
        assert "invoices" in sql


class TestAuditAt:
    def _make_instance(self):
        class FakeMeta:
            db_table = "orders"

        class Order(AuditedModelMixin):
            _meta = FakeMeta
            pk = 42

        return Order()

    def test_returns_historical_row_when_data_present(self):
        from auditrum.timetravel import HistoricalRow

        at = datetime(2024, 1, 1, tzinfo=UTC)
        payload = {"id": 42, "status": "pending"}
        with patch(
            "auditrum.integrations.django.mixins.reconstruct_row",
            return_value=payload,
        ):
            result = self._make_instance().audit_at(at)
        assert isinstance(result, HistoricalRow)
        assert result.table == "orders"
        assert result.object_id == "42"
        assert result.at == at
        assert result.data == payload

    def test_returns_none_when_row_absent(self):
        at = datetime(2024, 1, 1, tzinfo=UTC)
        with patch(
            "auditrum.integrations.django.mixins.reconstruct_row",
            return_value=None,
        ):
            result = self._make_instance().audit_at(at)
        assert result is None

    def test_passes_db_table_and_pk(self):
        at = datetime(2024, 1, 1, tzinfo=UTC)
        with patch(
            "auditrum.integrations.django.mixins.reconstruct_row",
            return_value=None,
        ) as mock:
            self._make_instance().audit_at(at)
        kwargs = mock.call_args[1]
        assert kwargs["table"] == "orders"
        assert kwargs["object_id"] == "42"
        assert kwargs["at"] == at


class TestAuditFieldHistory:
    def _make_instance(self):
        class FakeMeta:
            db_table = "users"

        class User(AuditedModelMixin):
            _meta = FakeMeta
            pk = 7

        return User()

    def test_delegates_to_reconstruct_field_history(self):
        timeline = [
            (datetime(2024, 1, 1, tzinfo=UTC), "a@x.com"),
            (datetime(2024, 2, 1, tzinfo=UTC), "a2@x.com"),
        ]
        with patch(
            "auditrum.integrations.django.mixins.reconstruct_field_history",
            return_value=timeline,
        ) as mock:
            result = self._make_instance().audit_field_history("email")
        assert result == timeline
        kwargs = mock.call_args[1]
        assert kwargs["field"] == "email"
        assert kwargs["table"] == "users"
        assert kwargs["object_id"] == "7"


class TestAuditStateAsOf:
    def test_iterates_historical_rows(self):
        class FakeMeta:
            db_table = "products"

        class Product(AuditedModelMixin):
            _meta = FakeMeta

        at = datetime(2024, 1, 1, tzinfo=UTC)
        rows = [
            ("1", {"id": 1, "name": "a"}),
            ("2", {"id": 2, "name": "b"}),
        ]
        with patch(
            "auditrum.integrations.django.mixins.reconstruct_table",
            return_value=iter(rows),
        ):
            result = list(Product.audit_state_as_of(at))
        assert len(result) == 2
        assert result[0].object_id == "1"
        assert result[0].data == {"id": 1, "name": "a"}
        assert result[0].at == at
