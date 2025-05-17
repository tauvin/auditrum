from django.contrib.contenttypes.fields import GenericForeignKey
from django.contrib.contenttypes.models import ContentType
from django.db import models

from auditrum.integrations.django.settings import audit_settings


class AuditLog(models.Model):
    operation = models.CharField(max_length=16)
    changed_at = models.DateTimeField(auto_now_add=True)
    content_type = models.ForeignKey(ContentType, null=True, blank=True, on_delete=models.SET_NULL)
    object_id = models.CharField(max_length=255)
    content_object = GenericForeignKey("content_type", "object_id")
    table_name = models.CharField(max_length=255)
    user_id = models.IntegerField(null=True, blank=True)
    old_data = models.JSONField(null=True, blank=True)
    new_data = models.JSONField(null=True, blank=True)
    diff = models.JSONField(null=True, blank=True)
    meta = models.JSONField(null=True, blank=True)
    request_id = models.CharField(max_length=255, null=True, blank=True)
    change_reason = models.TextField(null=True, blank=True)
    source = models.CharField(max_length=255, null=True, blank=True)

    class Meta:
        db_table = audit_settings.table_name
        verbose_name = "Audit Log"
        verbose_name_plural = "Audit Logs"
        managed = False

    def __str__(self):
        return f"{self.operation} on {self.table_name} ({self.object_id})"
