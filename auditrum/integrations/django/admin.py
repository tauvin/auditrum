from collections.abc import Iterable
from typing import TYPE_CHECKING, Any

from django.contrib import admin
from django.http import HttpRequest, HttpResponse
from django.urls import NoReverseMatch, reverse
from django.utils.html import format_html
from django.utils.safestring import SafeString

from auditrum.integrations.django.models import AuditContext, AuditLog
from auditrum.integrations.django.utils import model_for_table

__all__ = [
    "AuditContextAdmin",
    "AuditLogAdmin",
]

# django-stubs makes ``ModelAdmin`` generic in the administered model, but the
# real class has no ``__class_getitem__`` — subscripting it at runtime raises
# ``TypeError``. Bind the parameter for the checker only.
if TYPE_CHECKING:
    _ContextAdminBase = admin.ModelAdmin[AuditContext]
    _LogAdminBase = admin.ModelAdmin[AuditLog]
else:
    _ContextAdminBase = admin.ModelAdmin
    _LogAdminBase = admin.ModelAdmin


def _admin_change_url(table_name: str, object_id: object) -> str | None:
    """Return the admin ``change`` URL for ``(table_name, object_id)``,
    or ``None`` if the table doesn't map to a registered admin model or
    the URL pattern can't be reversed for that object id.

    Resolves the model via the memoised :func:`model_for_table`, so the
    table → URL-name lookup is O(1) per distinct table_name on the page.
    """
    model_cls = model_for_table(table_name)
    if model_cls is None:
        return None
    opts = model_cls._meta
    url_name = f"admin:{opts.app_label}_{opts.model_name}_change"
    try:
        return reverse(url_name, args=[object_id])
    except NoReverseMatch:
        return None


@admin.register(AuditContext)
class AuditContextAdmin(_ContextAdminBase):
    list_display = (
        "id",
        "created_at",
        "source",
        "user_label",
        "change_reason",
        "event_count",
    )
    readonly_fields = (
        "id",
        "metadata",
        "created_at",
        "updated_at",
        "events_link",
    )
    ordering = ("-created_at",)
    search_fields = ("id",)

    @admin.display(description="Events")
    def event_count(self, obj: AuditContext) -> int:
        return obj.events.count()

    @admin.display(description="Source")
    def source(self, obj: AuditContext) -> str:
        return (obj.metadata or {}).get("source") or "—"

    @admin.display(description="User")
    def user_label(self, obj: AuditContext) -> object:
        # ``username`` is the human-readable label the middleware stamps
        # when available; ``user_id`` is the typed integer — fall back
        # to whichever is present so the admin row always renders
        # something meaningful for event-by-user triage.
        metadata = obj.metadata or {}
        return metadata.get("username") or metadata.get("user_id") or "—"

    @admin.display(description="Change Reason")
    def change_reason(self, obj: AuditContext) -> str:
        return (obj.metadata or {}).get("change_reason") or "—"

    @admin.display(description="Events in this context")
    def events_link(self, obj: AuditContext) -> SafeString:
        # Render a link to the pre-filtered AuditLog changelist rather
        # than embedding an inline — bulk operations under a single
        # context can produce thousands of rows, which the inline would
        # try to load in one page and OOM the admin. The filtered
        # changelist is paginated and cheap. The URL uses the FK
        # field name (``context``), not the DB column, so Django's
        # changelist maps it to ``filter(context=<uuid>)`` directly.
        count = obj.events.count()
        url = reverse("admin:auditrum_django_auditlog_changelist")
        return format_html(
            '<a href="{}?context={}">View {} events</a>',
            url,
            obj.id,
            count,
        )


@admin.register(AuditLog)
class AuditLogAdmin(_LogAdminBase):
    list_display = (
        "changed_at",
        "operation",
        "table_name",
        "object_id",
        "user_id",
        "context_id",
        "linked_object",
    )
    list_filter = ("operation", "table_name")
    # ``context_id`` is only the ``db_column`` of the ``context`` FK —
    # not a concrete field on the model — so Django's default
    # ``__icontains`` search lookup fails with ``FieldError: Unsupported
    # lookup 'icontains' for ForeignKey``. Traverse via ``context__id``
    # with an explicit ``__exact`` lookup: UUIDs are unique identifiers
    # so substring matching has no use, and ``exact`` dodges the
    # ``UPPER(uuid)`` casting that ``iexact`` would trigger. Django 4+
    # honours explicit lookup suffixes in ``search_fields``.
    #
    # The ORM optimises ``context__id__exact`` into a direct
    # ``auditlog.context_id = <uuid>`` comparison — it detects that
    # ``id`` is the PK of the referenced ``AuditContext`` and the FK
    # column already holds that value, so no JOIN is emitted. The
    # query hits the ``auditlog_context_id_idx`` btree directly.
    search_fields = ("object_id", "context__id__exact")
    readonly_fields = [f.name for f in AuditLog._meta.fields]
    # `-id` (serial) breaks ties: `now()` gives all events in one transaction the
    # same `changed_at`, so id (write order) keeps the changelist chronological.
    ordering = ("-changed_at", "-id")

    # When ``False`` (default) the ``linked_object`` column renders a
    # cheap ``<table>#<id>`` link to the standard admin change view —
    # zero SQL queries per row, regardless of page size. When ``True``,
    # the changelist additionally batch-fetches every referenced target
    # via :meth:`Manager.in_bulk` (one SELECT per distinct ``table_name``
    # on the page, **not** one per row) so the column can render the
    # live ``str(target)``. The default switched in 0.4.4 to fix an N+1
    # that made changelists with hundreds of events cancellable under
    # ASGI.
    resolve_linked_objects: bool = False

    def changelist_view(
        self, request: HttpRequest, extra_context: dict[str, Any] | None = None
    ) -> HttpResponse:
        response = super().changelist_view(request, extra_context)
        if not self.resolve_linked_objects:
            return response
        # ``response.context_data`` only exists for TemplateResponse —
        # it isn't there on redirects (e.g. action POST → 302) or on
        # responses already rendered by middleware. The early-return
        # also covers ``cl is None`` when the changelist short-circuits.
        context_data = getattr(response, "context_data", None) or {}
        cl = context_data.get("cl")
        if cl is not None:
            self._attach_linked_targets(cl.result_list)
        return response

    @staticmethod
    def _attach_linked_targets(rows: Iterable[Any]) -> None:
        """Resolve every ``(table_name, object_id)`` tuple on the page
        in O(distinct table_names) SELECTs and stash the result on each
        row as ``_auditrum_linked``. ``linked_object`` then prefers the
        stashed instance over the cheap link.

        ``rows`` is typed loosely because this method *adds* the
        ``_auditrum_linked`` attribute to each :class:`AuditLog` row —
        it is a render-time annotation owned by this admin, not a model
        field, so it deliberately doesn't exist on the model's type.

        Mismatched PK types (e.g. an int ``object_id`` against a UUID
        column) raise from ``in_bulk`` — the per-table block is skipped
        rather than aborting the whole page. Models that aren't
        installed at all are simply skipped.
        """
        buckets: dict[str, set[str]] = {}
        for row in rows:
            if row.table_name and row.object_id:
                buckets.setdefault(row.table_name, set()).add(str(row.object_id))

        resolved: dict[tuple[str, str], object] = {}
        for table_name, ids in buckets.items():
            model_cls = model_for_table(table_name)
            if model_cls is None:
                continue
            try:
                fetched = model_cls._default_manager.in_bulk(ids)
            except (ValueError, TypeError):
                continue
            for pk, instance in fetched.items():
                resolved[(table_name, str(pk))] = instance

        for row in rows:
            row._auditrum_linked = resolved.get((row.table_name, str(row.object_id)))

    @admin.display(description="Linked Object")
    def linked_object(self, obj: AuditLog) -> str | SafeString:
        if not obj.object_id or not obj.table_name:
            return "-"
        # Tier 2: when ``resolve_linked_objects`` is enabled and
        # ``changelist_view`` has stashed a pre-fetched target, render
        # ``str(target)`` (preserving the pre-0.4.4 behaviour).
        prefetched = getattr(obj, "_auditrum_linked", None)
        if prefetched is not None:
            get_url = getattr(prefetched, "get_absolute_url", None)
            if callable(get_url):
                try:
                    return format_html(
                        '<a href="{}">{}</a>',
                        get_url(),
                        str(prefetched),
                    )
                except Exception:
                    return str(prefetched)
            return str(prefetched)
        # Tier 1 default: zero-query link to the standard admin change
        # view. Keeps the column useful for navigation without paying
        # one SELECT per row at render time.
        label = f"{obj.table_name}#{obj.object_id}"
        url = _admin_change_url(obj.table_name, obj.object_id)
        if url is None:
            return label
        return format_html('<a href="{}">{}</a>', url, label)
