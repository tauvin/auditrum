"""Unit tests for AuditLogAdmin / AuditContextAdmin @admin.display methods."""

from unittest.mock import MagicMock, patch

import pytest

django = pytest.importorskip("django")

from django.conf import settings as django_settings  # noqa: E402

if not django_settings.configured:
    django_settings.configure(
        SECRET_KEY="test-secret",
        INSTALLED_APPS=[
            "django.contrib.contenttypes",
            "django.contrib.auth",
            "django.contrib.admin",
            "django.contrib.sessions",
            "django.contrib.messages",
            "auditrum.integrations.django",
        ],
        DATABASES={
            "default": {"ENGINE": "django.db.backends.sqlite3", "NAME": ":memory:"}
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
        USE_TZ=True,
        TIME_ZONE="UTC",
    )
    django.setup()

from django.contrib import admin as django_admin  # noqa: E402

from auditrum.integrations.django.admin import (  # noqa: E402
    AuditContextAdmin,
    AuditLogAdmin,
)
from auditrum.integrations.django.models import AuditContext, AuditLog  # noqa: E402


def _context_admin():
    return AuditContextAdmin(AuditContext, django_admin.site)


def _auditlog_admin():
    return AuditLogAdmin(AuditLog, django_admin.site)


class TestAuditContextAdminMetadataDisplays:
    """The metadata-derived ``@admin.display`` columns surface the most-
    common per-context attributes (source, user, change_reason) directly
    on the changelist so an operator can triage without clicking into
    every row.
    """

    def test_source_reads_metadata(self):
        obj = MagicMock(spec=AuditContext)
        obj.metadata = {"source": "http"}
        assert _context_admin().source(obj) == "http"

    def test_source_missing_returns_em_dash(self):
        obj = MagicMock(spec=AuditContext)
        obj.metadata = {}
        assert _context_admin().source(obj) == "—"

    def test_source_metadata_none(self):
        obj = MagicMock(spec=AuditContext)
        obj.metadata = None
        assert _context_admin().source(obj) == "—"

    def test_user_label_prefers_username(self):
        obj = MagicMock(spec=AuditContext)
        obj.metadata = {"username": "alice", "user_id": 42}
        assert _context_admin().user_label(obj) == "alice"

    def test_user_label_falls_back_to_user_id(self):
        obj = MagicMock(spec=AuditContext)
        obj.metadata = {"user_id": 42}
        assert _context_admin().user_label(obj) == 42

    def test_user_label_em_dash_when_anonymous(self):
        obj = MagicMock(spec=AuditContext)
        obj.metadata = {}
        assert _context_admin().user_label(obj) == "—"

    def test_change_reason_reads_metadata(self):
        obj = MagicMock(spec=AuditContext)
        obj.metadata = {"change_reason": "GDPR erasure"}
        assert _context_admin().change_reason(obj) == "GDPR erasure"

    def test_change_reason_missing(self):
        obj = MagicMock(spec=AuditContext)
        obj.metadata = {}
        assert _context_admin().change_reason(obj) == "—"


class TestAuditContextAdminEventsLink:
    """The events link replaces a potential inline — bulk operations can
    attach thousands of events to one context and embedding them inline
    would OOM the admin page. A filtered changelist is paginated and
    cheap.
    """

    def test_renders_anchor_with_filter(self):
        obj = MagicMock(spec=AuditContext)
        obj.id = "abc12345-0000-0000-0000-000000000000"
        obj.events.count.return_value = 123

        # ``reverse`` needs the admin URLconf loaded, which this test
        # module doesn't configure. Patch at the import site so the
        # test focuses on the HTML shape, not URL routing.
        with patch(
            "auditrum.integrations.django.admin.reverse",
            return_value="/admin/auditrum_django/auditlog/",
        ):
            html = _context_admin().events_link(obj)

        assert "<a " in html
        assert "href=" in html
        assert obj.id in html
        assert "?context=" in html
        assert "View 123 events" in html

    def test_zero_events_still_renders_link(self):
        obj = MagicMock(spec=AuditContext)
        obj.id = "ffff0000-0000-0000-0000-000000000000"
        obj.events.count.return_value = 0

        with patch(
            "auditrum.integrations.django.admin.reverse",
            return_value="/admin/auditrum_django/auditlog/",
        ):
            html = _context_admin().events_link(obj)
        assert "View 0 events" in html


class TestAuditLogAdminLinkedObject:
    """Regression for the 0.4 content_type bug: ``linked_object`` now
    resolves the instance via ``table_name`` (using ``model_for_table``)
    instead of the always-NULL ``content_object`` GenericForeignKey.
    """

    def test_returns_dash_when_table_unknown(self):
        obj = MagicMock(spec=AuditLog)
        obj.table_name = "__no_such_table__"
        obj.object_id = "1"
        assert _auditlog_admin().linked_object(obj) == "-"

    def test_returns_dash_when_object_id_empty(self):
        obj = MagicMock(spec=AuditLog)
        obj.table_name = "auth_user"
        obj.object_id = ""
        assert _auditlog_admin().linked_object(obj) == "-"

    def test_returns_dash_when_instance_missing(self):
        """When ``model_for_table`` finds a matching model but the
        ``object_id`` refers to a deleted / never-existed row, resolve
        gracefully instead of raising to the admin changelist.
        """

        class FakeDoesNotExist(Exception):
            pass

        class FakeManager:
            def get(self, pk):
                raise FakeDoesNotExist()

        class FakeModel:
            _default_manager = FakeManager()
            DoesNotExist = FakeDoesNotExist

        obj = MagicMock(spec=AuditLog)
        obj.table_name = "orders"
        obj.object_id = "99999999"

        with patch(
            "auditrum.integrations.django.admin.model_for_table",
            return_value=FakeModel,
        ):
            assert _auditlog_admin().linked_object(obj) == "-"

    def test_search_fields_excludes_raw_context_id(self):
        """Regression for the 0.4.1 crash: ``context_id`` is only the
        DB column of the ``context`` FK, not a concrete model field,
        so Django's default ``__icontains`` lookup raised ``FieldError``
        the moment an operator typed into the admin search box. The
        fix traverses the FK via ``context__id__exact`` — UUIDs are
        unique identifiers, substring search has no use, and
        ``exact`` avoids UUIDField casting weirdness that ``iexact``
        would trigger on PG.
        """
        admin_ = _auditlog_admin()
        assert "context_id" not in admin_.search_fields
        assert "context__id__exact" in admin_.search_fields

    def test_search_uses_explicit_exact_lookup(self):
        """Simulate what Django's ModelAdmin.get_search_results does
        internally: it calls ``construct_search`` on each entry in
        ``search_fields`` to pick the lookup. With the fix in place,
        ``context__id__exact`` is used verbatim (Django 4+ respects
        explicit lookups in search_fields) instead of getting a
        spurious ``__icontains`` tail that would crash at build time.
        """
        from django.contrib.admin.options import ModelAdmin

        admin_ = _auditlog_admin()
        # Use ``.all()`` (not ``.none()``) so SQL compilation of the
        # search clause is exercised; ``.none()`` short-circuits to
        # ``EmptyResultSet`` before the WHERE is generated.
        qs = AuditLog.objects.all()
        request = MagicMock()
        request.user = MagicMock()
        qs, _ = ModelAdmin.get_search_results(
            admin_,
            request,
            qs,
            "00000000-0000-0000-0000-000000000001",
        )
        # If ``search_fields`` still referenced the bare FK column,
        # building the SQL would raise ``FieldError`` for icontains.
        # Forcing the SQL compile here is what makes the assertion
        # meaningful.
        sql = str(qs.query)
        # Django optimises ``context__id__exact`` into a direct
        # comparison on ``auditlog.context_id`` — the FK column IS the
        # foreign key's ``id`` value, so no JOIN to ``audit_context``
        # is needed. The SQL below would have been
        # ``auditlog.context_id = <uuid>`` (possibly CAST-wrapped).
        assert "context_id" in sql
        # object_id participates in the OR via icontains for
        # non-UUID terms.
        assert "object_id" in sql
        assert "LIKE" in sql.upper()

    def test_renders_link_when_instance_has_get_absolute_url(self):
        class FakeManager:
            def get(self, pk):
                target = MagicMock()
                target.get_absolute_url = MagicMock(return_value="/orders/1/")
                target.__str__ = MagicMock(return_value="Order #1")
                return target

        class FakeModel:
            _default_manager = FakeManager()
            DoesNotExist = Exception

        obj = MagicMock(spec=AuditLog)
        obj.table_name = "orders"
        obj.object_id = "1"

        with patch(
            "auditrum.integrations.django.admin.model_for_table",
            return_value=FakeModel,
        ):
            html = _auditlog_admin().linked_object(obj)
        assert "/orders/1/" in html
        assert "Order #1" in html
