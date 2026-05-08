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

    As of 0.4.4 the column also no longer fans out one SELECT per row.
    The default path renders a zero-query ``<table>#<id>`` link to the
    standard admin change view. Opt back in to live ``str(target)`` via
    ``AuditLogAdmin.resolve_linked_objects = True`` — that path batches
    fetches in :meth:`changelist_view` to one ``in_bulk`` per distinct
    ``table_name``.
    """

    def test_renders_plain_label_when_table_unknown(self):
        # When ``model_for_table`` doesn't recognise the table, fall back
        # to a plain ``<table>#<id>`` label. Pre-0.4.4 this was ``"-"``;
        # the label is strictly more useful for triage (cross-service
        # audit rows still surface their identity) and avoids the
        # surprise of dropped models silently disappearing from the UI.
        obj = MagicMock(spec=AuditLog)
        obj.table_name = "__no_such_table__"
        obj.object_id = "1"
        assert _auditlog_admin().linked_object(obj) == "__no_such_table__#1"

    def test_returns_dash_when_object_id_empty(self):
        obj = MagicMock(spec=AuditLog)
        obj.table_name = "auth_user"
        obj.object_id = ""
        assert _auditlog_admin().linked_object(obj) == "-"

    def test_returns_dash_when_table_name_empty(self):
        obj = MagicMock(spec=AuditLog)
        obj.table_name = ""
        obj.object_id = "1"
        assert _auditlog_admin().linked_object(obj) == "-"

    def test_default_path_does_not_hit_db(self):
        """Regression for the 0.4.3 N+1 in the AuditLog changelist:
        rendering ``linked_object`` must never call
        ``Manager.get`` / ``in_bulk`` on the default path. A page with N
        rows used to issue N extra single-row SELECTs, blowing past
        proxy/browser timeouts on filtered changelists.
        """

        class ExplodingManager:
            def get(self, *args, **kwargs):
                raise AssertionError("linked_object hit the DB on the default path")

            def in_bulk(self, *args, **kwargs):
                raise AssertionError("linked_object hit the DB on the default path")

        class FakeModel:
            _default_manager = ExplodingManager()
            DoesNotExist = Exception

            class _meta:
                app_label = "fake"
                model_name = "thing"

        obj = MagicMock(spec=AuditLog)
        obj.table_name = "orders"
        obj.object_id = "99999999"

        with (
            patch(
                "auditrum.integrations.django.admin.model_for_table",
                return_value=FakeModel,
            ),
            patch(
                "auditrum.integrations.django.admin.reverse",
                return_value="/admin/fake/thing/99999999/change/",
            ),
        ):
            html = _auditlog_admin().linked_object(obj)

        assert "orders#99999999" in html
        assert "/admin/fake/thing/99999999/change/" in html

    def test_default_path_falls_back_to_label_when_no_admin_route(self):
        """When the model exists but isn't admin-registered (so
        ``reverse`` raises ``NoReverseMatch``), drop the ``<a>`` wrap
        and emit the bare ``<table>#<id>`` label.
        """
        from django.urls import NoReverseMatch

        class FakeModel:
            class _meta:
                app_label = "fake"
                model_name = "thing"

        obj = MagicMock(spec=AuditLog)
        obj.table_name = "orders"
        obj.object_id = "1"

        with (
            patch(
                "auditrum.integrations.django.admin.model_for_table",
                return_value=FakeModel,
            ),
            patch(
                "auditrum.integrations.django.admin.reverse",
                side_effect=NoReverseMatch(),
            ),
        ):
            assert _auditlog_admin().linked_object(obj) == "orders#1"

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

    def test_resolved_path_renders_get_absolute_url_when_prefetched(self):
        """``resolve_linked_objects = True`` opts the admin into the
        pre-0.4.4 ``str(target)`` rendering. The instance is provided
        out-of-band (via ``_attach_linked_targets``) on the row as
        ``_auditrum_linked``; ``linked_object`` then prefers it over
        the cheap link.
        """
        target = MagicMock()
        target.get_absolute_url = MagicMock(return_value="/orders/1/")
        target.__str__ = MagicMock(return_value="Order #1")

        obj = MagicMock(spec=AuditLog)
        obj.table_name = "orders"
        obj.object_id = "1"
        obj._auditrum_linked = target

        html = _auditlog_admin().linked_object(obj)
        assert "/orders/1/" in html
        assert "Order #1" in html

    def test_resolved_path_falls_back_to_str_when_no_url(self):
        # ``MagicMock(spec=[])`` blocks every attr including ``__str__``,
        # which we still need — use a real class with no
        # ``get_absolute_url`` attribute instead.
        class _Target:
            def __str__(self) -> str:
                return "Order #2"

        obj = MagicMock(spec=AuditLog)
        obj.table_name = "orders"
        obj.object_id = "2"
        obj._auditrum_linked = _Target()

        assert _auditlog_admin().linked_object(obj) == "Order #2"


class TestAuditLogAdminBatchedResolve:
    """The opt-in ``resolve_linked_objects`` path must keep changelist
    rendering bounded by ``O(distinct table_names)`` SELECTs — the whole
    point of the 0.4.4 fix is that adopters can recover the live
    ``str(target)`` column without paying the per-row N+1.
    """

    def test_batches_in_bulk_per_table_name(self):
        """50 rows across two distinct ``table_name`` values must hit
        ``Manager.in_bulk`` exactly twice (once per table), not 50
        times.
        """
        in_bulk_calls: list[tuple[str, set[str]]] = []

        def _make_model(table: str):
            class _Manager:
                @staticmethod
                def in_bulk(ids):
                    in_bulk_calls.append((table, set(ids)))
                    return {pk: f"{table}#{pk}" for pk in ids}

            class _Model:
                _default_manager = _Manager()

            return _Model

        models_by_table = {"orders": _make_model("orders"), "users": _make_model("users")}

        rows = []
        for i in range(40):
            row = MagicMock(spec=AuditLog)
            row.table_name = "orders"
            row.object_id = str(i)
            rows.append(row)
        for i in range(10):
            row = MagicMock(spec=AuditLog)
            row.table_name = "users"
            row.object_id = str(i)
            rows.append(row)

        with patch(
            "auditrum.integrations.django.admin.model_for_table",
            side_effect=lambda table: models_by_table.get(table),
        ):
            AuditLogAdmin._attach_linked_targets(rows)

        # Exactly one ``in_bulk`` call per distinct table name, even
        # though there are 50 rows on the page.
        assert len(in_bulk_calls) == 2
        tables_called = {call[0] for call in in_bulk_calls}
        assert tables_called == {"orders", "users"}
        # Every row got its target stashed for the renderer.
        for row in rows:
            assert row._auditrum_linked == f"{row.table_name}#{row.object_id}"

    def test_skips_table_when_pk_type_mismatches(self):
        """Mismatched PK types (e.g. integer ``object_id`` against a
        UUID PK) raise ``ValueError`` from ``in_bulk``. The whole page
        must keep rendering — only that one table is skipped.
        """

        class _BadManager:
            @staticmethod
            def in_bulk(ids):
                raise ValueError("badly formed UUID")

        class _BadModel:
            _default_manager = _BadManager()

        row = MagicMock(spec=AuditLog)
        row.table_name = "orders"
        row.object_id = "not-a-uuid"

        with patch(
            "auditrum.integrations.django.admin.model_for_table",
            return_value=_BadModel,
        ):
            AuditLogAdmin._attach_linked_targets([row])

        assert row._auditrum_linked is None

    def test_changelist_view_skips_resolve_when_disabled(self):
        """The default ``resolve_linked_objects = False`` must NOT call
        ``_attach_linked_targets`` — that's what keeps the default page
        render at zero extra SELECTs.
        """
        admin_ = _auditlog_admin()
        assert admin_.resolve_linked_objects is False

        with (
            patch.object(
                AuditLogAdmin,
                "_attach_linked_targets",
            ) as attach,
            patch.object(
                django_admin.ModelAdmin,
                "changelist_view",
                return_value=MagicMock(context_data={"cl": MagicMock()}),
            ),
        ):
            admin_.changelist_view(MagicMock())

        attach.assert_not_called()
