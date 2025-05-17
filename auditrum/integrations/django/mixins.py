from apps.audit.models import AuditLog
from django.contrib.contenttypes.models import ContentType
from django.core.paginator import EmptyPage, PageNotAnInteger, Paginator
from django.shortcuts import render
from django.urls import path


class AuditHistoryMixin:
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
        obj = self.get_object(request, object_id)
        content_type = ContentType.objects.get_for_model(self.model)
        logs = AuditLog.objects.filter(
            content_type=content_type, object_id=str(object_id)
        ).order_by("-changed_at")

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
