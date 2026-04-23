from django.contrib import admin
from django.urls import reverse
from django.utils.html import format_html

from auditrum.integrations.django.models import AuditContext, AuditLog
from auditrum.integrations.django.utils import model_for_table

__all__ = [
    "AuditContextAdmin",
    "AuditLogAdmin",
]


@admin.register(AuditContext)
class AuditContextAdmin(admin.ModelAdmin):
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
    def event_count(self, obj):
        return obj.events.count()

    @admin.display(description="Source")
    def source(self, obj):
        return (obj.metadata or {}).get("source") or "—"

    @admin.display(description="User")
    def user_label(self, obj):
        # ``username`` is the human-readable label the middleware stamps
        # when available; ``user_id`` is the typed integer — fall back
        # to whichever is present so the admin row always renders
        # something meaningful for event-by-user triage.
        metadata = obj.metadata or {}
        return (
            metadata.get("username")
            or metadata.get("user_id")
            or "—"
        )

    @admin.display(description="Change Reason")
    def change_reason(self, obj):
        return (obj.metadata or {}).get("change_reason") or "—"

    @admin.display(description="Events in this context")
    def events_link(self, obj):
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
class AuditLogAdmin(admin.ModelAdmin):
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
    search_fields = ("object_id", "context_id")
    readonly_fields = [f.name for f in AuditLog._meta.fields]
    ordering = ("-changed_at",)

    @admin.display(description="Linked Object")
    def linked_object(self, obj):
        model_class = model_for_table(obj.table_name)
        if model_class is None or not obj.object_id:
            return "-"
        try:
            target = model_class._default_manager.get(pk=obj.object_id)
        except (model_class.DoesNotExist, ValueError, TypeError):
            return "-"
        get_url = getattr(target, "get_absolute_url", None)
        if callable(get_url):
            try:
                return format_html('<a href="{}">{}</a>', get_url(), str(target))
            except Exception:
                return str(target)
        return str(target)
