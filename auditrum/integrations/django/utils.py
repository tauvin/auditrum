from datetime import datetime
from typing import Any, Protocol

from django.contrib.admin.templatetags.admin_urls import admin_urlname
from django.db import models
from django.shortcuts import resolve_url
from django.utils.formats import date_format
from django.utils.html import format_html
from django.utils.safestring import mark_safe

from auditrum.utils import audit_tracked  # re-exported for backwards compatibility


class _ExecuteCursor(Protocol):
    def execute(self, query: str, params: Any = ..., /) -> Any: ...

__all__ = [
    "audit_tracked",
    "link",
    "link_to_related_object",
    "resolve_field_value",
    "get_user_display",
    "render_log_changes",
    "set_var",
]


def link(href: str, text: str) -> str:
    return format_html('<a href="{}">{}</a>', href, text)


def link_to_related_object(obj: models.Model, name: str | None = None) -> str:
    url = resolve_url(admin_urlname(obj._meta, "change"), obj.pk)  # ty: ignore[invalid-argument-type]
    return link(url, name or str(obj))


def resolve_field_value(model_class, field_name, value):
    try:
        field = model_class._meta.get_field(field_name)
        label = field.verbose_name.title()

        # Handle choices
        if field.choices and value is not None:
            value = dict(field.choices).get(value, value)

        # ForeignKey → str(obj)
        elif isinstance(field, models.ForeignKey) and value:
            try:
                value = str(field.remote_field.model.objects.get(pk=value))
            except field.remote_field.model.DoesNotExist:
                value = f"[{value}]"

        # Date / Time fields
        elif isinstance(field, (models.DateTimeField, models.DateField, models.TimeField)) and value:
            try:
                parsed = value
                if isinstance(value, str):
                    parsed = datetime.fromisoformat(value)
                if isinstance(field, models.DateTimeField):
                    value = date_format(parsed, format="DATETIME_FORMAT")
                elif isinstance(field, models.DateField):
                    value = date_format(parsed, format="DATE_FORMAT")
                elif isinstance(field, models.TimeField):
                    value = date_format(parsed, format="TIME_FORMAT")
            except Exception:
                pass

    except Exception:
        label = field_name
    return label, value or "—"


def get_user_display(log):
    return getattr(log.user, "username", None) or log.meta.get("username") or "System"


def render_log_changes(log):
    if log.content_type is None:
        return "—"

    model_class = log.content_type.model_class()

    if log.operation == "INSERT" and log.new_data:
        return format_html(
            "<ul class='space-y-1'>{}</ul>",
            mark_safe(
                "".join(
                    format_html("<li><strong>{}</strong>: {}</li>", *resolve_field_value(model_class, k, v))
                    for k, v in log.new_data.items()
                )
            ),
        )

    elif log.operation == "DELETE" and log.old_data:
        return format_html(
            "<ul class='space-y-1 text-red-700'>{}</ul>",
            mark_safe(
                "".join(
                    format_html(
                        "<li><strong>{}</strong>: <span class='line-through'>{}</span></li>",
                        *resolve_field_value(model_class, k, v),
                    )
                    for k, v in log.old_data.items()
                )
            ),
        )

    elif log.old_data and log.new_data:
        diffs = []
        for key in sorted(set(log.old_data) | set(log.new_data)):
            old_val = log.old_data.get(key)
            new_val = log.new_data.get(key)
            if old_val != new_val:
                label, old_str = resolve_field_value(model_class, key, old_val)
                _, new_str = resolve_field_value(model_class, key, new_val)
                diffs.append(
                    format_html(
                        "<li><strong>{}</strong>: <span class='line-through text-red-600'>{}</span> <span class='text-gray-400 px-1'>→</span> <span class='text-green-700 font-medium'>{}</span></li>",
                        label,
                        old_str,
                        new_str,
                    )
                )
        return (
            format_html("<ul class='space-y-1'>{}</ul>", mark_safe("".join(diffs)))
            if diffs
            else mark_safe("<em class='text-gray-500'>No changes</em>")
        )

    return mark_safe("<em class='text-gray-500'>No changes</em>")


def set_var(cursor: _ExecuteCursor, key: str, value: Any) -> None:
    # set_config(name, value, is_local=false) is the parameterised form
    # of SET — both the GUC name and value go through psycopg's binding
    # layer, so an attacker-controlled `key` can't break out into DDL.
    # The f-string version that used to live here was a real injection
    # surface any time `key` wasn't a compile-time constant.
    if value is not None:
        cursor.execute("SELECT set_config(%s, %s, false)", [key, str(value)])
