from django.contrib import admin
from django.utils.html import format_html

from auditrum.integrations.django.models import AuditLog


@admin.register(AuditLog)
class AuditLogAdmin(admin.ModelAdmin):
    list_display = (
        "changed_at",
        "operation",
        "table_name",
        "object_id",
        "user_id",
        "source",
        "request_id",
        "linked_object",
    )
    list_filter = ("operation", "table_name", "source")
    search_fields = ("object_id", "request_id", "change_reason")
    readonly_fields = [f.name for f in AuditLog._meta.fields]
    ordering = ("-changed_at",)

    def linked_object(self, obj):
        if obj.content_object:
            return format_html(
                '<a href="{}">{}</a>',
                obj.content_object.get_absolute_url(),
                obj.content_object,
            )
        return "-"

    linked_object.allow_tags = True
    linked_object.short_description = "Linked Object"
