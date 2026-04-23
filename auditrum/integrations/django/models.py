import uuid
from typing import TYPE_CHECKING

from django.db import models

from auditrum.integrations.django.settings import audit_settings

if TYPE_CHECKING:
    from django.contrib.auth.models import AbstractBaseUser
    from django.db.models import Model

__all__ = [
    "AuditContext",
    "AuditLog",
    "AuditLogManager",
    "AuditLogQuerySet",
]


class AuditLogQuerySet(models.QuerySet):
    """QuerySet with single-table-aware helpers for audit history lookups.

    All methods return chainable querysets so callers can compose them with
    ``order_by``, ``values``, pagination, etc. The single backing table means
    history survives model-field rename/delete, and the
    ``(table_name, object_id, changed_at DESC)`` composite index handles the
    most common queries without jsonb traversals.
    """

    def for_model(self, model_cls: "type[Model]") -> "AuditLogQuerySet":
        """All events for rows of ``model_cls``."""
        return self.filter(table_name=model_cls._meta.db_table)

    def for_object(self, instance: "Model") -> "AuditLogQuerySet":
        """All events for a specific model instance (by pk)."""
        return self.for_model(type(instance)).filter(object_id=str(instance.pk))

    def for_user(self, user: "AbstractBaseUser | int | None") -> "AuditLogQuerySet":
        """All events authored by ``user``.

        Accepts a Django user instance or a raw ``user_id`` integer. Uses the
        dedicated ``auditlog.user_id`` column populated by
        ``_audit_current_user_id()``, so the lookup hits a plain btree index
        instead of traversing ``context.metadata->>'user_id'``.
        """
        if user is None:
            return self.filter(user_id__isnull=True)
        user_id = getattr(user, "pk", user)
        return self.filter(user_id=user_id)

    def for_context(self, context_id: "uuid.UUID | str") -> "AuditLogQuerySet":
        """All events sharing the given audit context UUID (per-request/job)."""
        return self.filter(context_id=str(context_id))

    def by_table(self, table_name: str) -> "AuditLogQuerySet":
        """All events for a raw ``table_name`` (use when the Python model is
        not imported, e.g. for cross-service auditing)."""
        return self.filter(table_name=table_name)

    def recent(self, limit: int = 100) -> "AuditLogQuerySet":
        """Most recent ``limit`` events overall, newest first."""
        return self.order_by("-changed_at")[:limit]


class AuditLogManager(models.Manager.from_queryset(AuditLogQuerySet)):  # ty: ignore[unsupported-base]
    # Manager.from_queryset() returns a dynamically-built subclass whose
    # identity type checkers can't statically infer. The pattern is the
    # standard Django idiom for copying queryset methods onto a manager,
    # so we accept the base-class unknown here rather than duplicate
    # every QuerySet method on the manager by hand.
    """Default manager for :class:`AuditLog` exposing the queryset helpers."""

    def get_queryset(self) -> AuditLogQuerySet:
        return AuditLogQuerySet(self.model, using=self._db)


class AuditContext(models.Model):
    """Request/job-scoped context shared by multiple audit events.

    Rows are inserted lazily by the ``_audit_attach_context()`` PL/pgSQL
    function when the first audit event in a transaction fires. If the
    request/job does not produce any audit events, no row is written.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    metadata = models.JSONField(default=dict)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = audit_settings.context_table_name
        managed = False
        verbose_name = "Audit Context"
        verbose_name_plural = "Audit Contexts"

    def __str__(self) -> str:
        return f"AuditContext({self.id})"


class AuditLog(models.Model):
    operation = models.CharField(max_length=16)
    changed_at = models.DateTimeField(auto_now_add=True)
    object_id = models.CharField(max_length=255)
    table_name = models.CharField(max_length=255)
    user_id = models.IntegerField(null=True, blank=True)
    old_data = models.JSONField(null=True, blank=True)
    new_data = models.JSONField(null=True, blank=True)
    diff = models.JSONField(null=True, blank=True)
    context = models.ForeignKey(
        AuditContext,
        null=True,
        blank=True,
        db_constraint=False,
        on_delete=models.DO_NOTHING,
        related_name="events",
        db_column="context_id",
    )
    meta = models.JSONField(null=True, blank=True)

    objects = AuditLogManager()

    class Meta:
        db_table = audit_settings.table_name
        verbose_name = "Audit Log"
        verbose_name_plural = "Audit Logs"
        managed = False

    def __str__(self) -> str:
        return f"{self.operation} on {self.table_name} ({self.object_id})"
