from collections.abc import Iterator
from datetime import datetime
from typing import TYPE_CHECKING

from django.contrib.admin.utils import unquote
from django.core.paginator import EmptyPage, PageNotAnInteger, Paginator
from django.db import connection as _django_connection
from django.shortcuts import render
from django.urls import path

from auditrum.integrations.django.models import AuditLog, AuditLogQuerySet
from auditrum.timetravel import (
    HistoricalRow,
    reconstruct_field_history,
    reconstruct_row,
    reconstruct_table,
)

# The mixins below are designed to be combined with Django classes at the
# call-site (AuditedModelMixin with models.Model, AuditHistoryMixin with
# admin.ModelAdmin). We swap the base under TYPE_CHECKING so a type checker
# sees the attributes those parent classes provide (_meta, pk, admin_site,
# model, get_object, get_urls...) while at runtime the mixin stays a plain
# object base — so `class Foo(MyMixin, models.Model)` still gives models.Model
# metaclass precedence, not a MRO conflict.
if TYPE_CHECKING:
    from django.contrib.admin import ModelAdmin
    from django.db.models import Model

    _ModelBase = Model
    _ModelAdminBase = ModelAdmin
else:
    _ModelBase = object
    _ModelAdminBase = object

__all__ = [
    "AuditHistoryMixin",
    "AuditedModelMixin",
]


class AuditedModelMixin(_ModelBase):
    """Model mixin exposing per-instance audit history, time-travel, and field timelines.

    All methods are read-only helpers built on top of the framework-agnostic
    :mod:`auditrum.tracking` / :mod:`auditrum.timetravel` core. No schema
    changes required on the decorated model — everything routes through the
    shared ``auditlog`` table and the ``(table_name, object_id, changed_at
    DESC)`` composite index.

    Usage::

        class Order(AuditedModelMixin, models.Model):
            status = models.CharField(max_length=32)
            ...

        order = Order.objects.get(pk=1)

        # all events on this row, newest first
        order.audit_events.order_by('-changed_at')

        # state at a specific moment
        snapshot = order.audit_at(datetime(2024, 6, 1, tzinfo=UTC))
        print(snapshot.status, snapshot.data)

        # timeline of a single field
        for ts, email in order.audit_field_history('email'):
            print(ts, email)

        # every surviving row at a given time
        for row in Order.audit_state_as_of(datetime(2024, 1, 1, tzinfo=UTC)):
            print(row.object_id, row.data)
    """

    @property
    def audit_events(self) -> AuditLogQuerySet:
        return AuditLog.objects.for_object(self)

    @classmethod
    def audit_history(cls) -> AuditLogQuerySet:
        """All events for this model across all instances (class-level helper)."""
        return AuditLog.objects.for_model(cls)

    def audit_at(self, at: datetime) -> HistoricalRow | None:
        """Return this instance's state at ``at``, or ``None`` if it didn't exist."""
        data = reconstruct_row(
            _django_connection,
            table=self._meta.db_table,
            object_id=str(self.pk),
            at=at,
        )
        if data is None:
            return None
        return HistoricalRow(
            table=self._meta.db_table,
            object_id=str(self.pk),
            at=at,
            data=data,
        )

    def audit_field_history(self, field: str) -> list[tuple[datetime, object]]:
        """``(changed_at, value)`` timeline for a single column on this row.

        Only events that actually changed ``field`` are included; a final
        ``(timestamp, None)`` entry marks the row's deletion.
        """
        return reconstruct_field_history(
            _django_connection,
            table=self._meta.db_table,
            object_id=str(self.pk),
            field=field,
        )

    @classmethod
    def audit_state_as_of(cls, at: datetime) -> Iterator[HistoricalRow]:
        """Iterate every surviving row of this model at the target timestamp.

        Wraps :func:`auditrum.timetravel.reconstruct_table`. DELETE'd rows
        are skipped. Each yielded :class:`HistoricalRow` can be converted to
        an unsaved model instance via ``.to_model(cls)`` if needed.
        """
        for obj_id, data in reconstruct_table(
            _django_connection, table=cls._meta.db_table, at=at
        ):
            yield HistoricalRow(
                table=cls._meta.db_table,
                object_id=str(obj_id),
                at=at,
                data=data,
            )


class AuditHistoryMixin(_ModelAdminBase):
    def get_urls(self):
        urls = super().get_urls()
        custom_urls = [
            path(
                "<path:object_id>/history/",
                self.admin_site.admin_view(self.object_history_view),
                name=f"{self.model._meta.app_label}_{self.model._meta.model_name}_audit_history",
            ),
        ]
        return custom_urls + urls

    def object_history_view(self, request, object_id):
        obj = self.get_object(request, unquote(object_id))
        if obj is None:
            # django-stubs doesn't expose the underscore-prefixed helper
            # even though ModelAdmin ships it — same pattern Django's own
            # admin.options.ModelAdmin.history_view uses.
            return self._get_obj_does_not_exist_redirect(  # ty: ignore[unresolved-attribute]
                request, self.model._meta, object_id
            )
        logs = AuditLog.objects.for_object(obj).order_by("-changed_at")

        paginator = Paginator(logs, 20)
        page_number = request.GET.get("page", 1)

        try:
            page_obj = paginator.page(page_number)
        except PageNotAnInteger:
            page_obj = paginator.page(1)
        except EmptyPage:
            page_obj = paginator.page(paginator.num_pages)

        context = dict(
            self.admin_site.each_context(request),
            title=f"History: {obj}",
            object=obj,
            opts=self.model._meta,
            audit_logs=page_obj.object_list,
            page_obj=page_obj,
            is_paginated=True,
        )
        return render(request, "audit/object_history.html", context)
