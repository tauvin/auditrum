from django.contrib import admin
from django.utils.html import format_html

from auditrum.integrations.django.models import AuditContext, AuditLog

__all__ = [
    "AuditContextAdmin",
    "AuditLogAdmin",
]


@admin.register(AuditContext)
class AuditContextAdmin(admin.ModelAdmin):
    list_display = ("id", "created_at", "updated_at", "event_count")
    readonly_fields = ("id", "metadata", "created_at", "updated_at")
    ordering = ("-created_at",)
    search_fields = ("id",)

    @admin.display(description="Events")
    def event_count(self, obj):
        return obj.events.count()


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
        try:
            target = obj.content_object
        except Exception:
            return "-"
        if target is None:
            return "-"
        get_url = getattr(target, "get_absolute_url", None)
        if callable(get_url):
            try:
                return format_html('<a href="{}">{}</a>', get_url(), str(target))
            except Exception:
                return str(target)
        return str(target)
