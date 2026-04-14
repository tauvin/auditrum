"""Django ``@track`` decorator for declarative audit trigger registration.

The decorator captures an audit :class:`~auditrum.tracking.TrackSpec` on
the decorated model class and stores it in a process-wide registry. The
management command ``auditrum_makemigrations`` walks the registry and
generates Django migration files containing :class:`InstallTrigger`
operations; runtime ``migrate`` then calls into
:class:`~auditrum.tracking.TriggerManager` via those operations.

Example::

    from auditrum.integrations.django import track

    @track(fields=["status", "total"], extra_meta=["tenant_id"])
    class Order(models.Model):
        status = models.CharField(max_length=32)
        total = models.DecimalField(max_digits=10, decimal_places=2)
        tenant_id = models.IntegerField()
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING, Any, TypeVar

from auditrum.tracking import FieldFilter, TrackSpec

if TYPE_CHECKING:
    from django.db.models import Model


T = TypeVar("T", bound="type[Model]")


# Keyed by fully-qualified Python class path so that re-imports don't
# register the same spec twice. Value is the :class:`TrackSpec` to install.
_registry: dict[str, TrackSpec] = {}


def _model_key(model_cls: type[Model]) -> str:
    return f"{model_cls.__module__}.{model_cls.__qualname__}"


def _build_filter(
    fields: list[str] | None, exclude: list[str] | None
) -> FieldFilter:
    if fields is not None and exclude is not None:
        raise ValueError(
            "track(): cannot pass both `fields` and `exclude` — choose one"
        )
    if fields is not None:
        return FieldFilter.only(*fields)
    if exclude is not None:
        return FieldFilter.exclude(*exclude)
    return FieldFilter.all()


def track(
    *,
    fields: list[str] | None = None,
    exclude: list[str] | None = None,
    extra_meta: list[str] | None = None,
    log_condition: str | None = None,
    audit_table: str | None = None,
    trigger_name: str | None = None,
) -> Callable[[T], T]:
    """Declarative decorator: attach an audit :class:`TrackSpec` to a model.

    Args mirror :class:`auditrum.tracking.TrackSpec` but accept a Django
    model class as decorator target and auto-resolve ``table`` from
    ``model_cls._meta.db_table``.

    Re-decorating the same class overwrites its existing spec, which is
    usually what you want during development (reload-friendly).
    """
    # Validate eagerly before we see the model class so a call like
    # ``@track(fields=[...], exclude=[...])`` fails at decoration time with
    # a clear error, not deep inside model introspection.
    field_filter = _build_filter(fields, exclude)

    def decorator(model_cls: T) -> T:
        from auditrum.integrations.django.settings import audit_settings

        spec = TrackSpec(
            table=model_cls._meta.db_table,
            audit_table=audit_table or audit_settings.table_name,
            fields=field_filter,
            extra_meta_fields=tuple(extra_meta or ()),
            log_condition=log_condition,
            trigger_name=trigger_name,
        )
        _registry[_model_key(model_cls)] = spec
        # Store spec on the class too, so introspection via
        # ``Order.audit_spec`` works even without reaching into the registry.
        model_cls.audit_spec = spec  # type: ignore[attr-defined]
        return model_cls

    return decorator


def register(model_cls: type[Model], **kwargs: Any) -> None:
    """Imperative equivalent of :func:`track` for use in ``audit.py`` modules.

    Accepts the same kwargs as :func:`track` plus legacy ``track_only`` /
    ``exclude_fields`` / ``meta_fields`` aliases for backwards compat.
    """
    # Legacy aliases from pre-tracking API
    if "track_only" in kwargs and "fields" not in kwargs:
        kwargs["fields"] = kwargs.pop("track_only")
    if "exclude_fields" in kwargs and "exclude" not in kwargs:
        kwargs["exclude"] = kwargs.pop("exclude_fields")
    if "meta_fields" in kwargs and "extra_meta" not in kwargs:
        kwargs["extra_meta"] = kwargs.pop("meta_fields")
    track(**kwargs)(model_cls)


def get_registered_specs() -> list[tuple[str, TrackSpec]]:
    """Return ``(model_key, spec)`` pairs for all registered models.

    Stable ordering by key for deterministic migration output.
    """
    return sorted(_registry.items())


def specs_by_app_label() -> dict[str, list[tuple[str, TrackSpec]]]:
    """Group registered specs by the ``app_label`` of the tracked model.

    Used by ``auditrum_makemigrations`` to decide which app gets each
    generated migration file.

    Builds a single ``{db_table: model}`` lookup up front rather than
    scanning ``apps.get_models()`` per spec — O(M + N) instead of O(M×N)
    for projects with many tracked models. If a spec references a
    ``db_table`` that no longer matches any installed model (e.g. the
    user renamed the model after decorating it), a warning is logged
    and the spec is silently dropped from the grouped output — the old
    behaviour was to drop without warning.
    """
    import logging

    from django.apps import apps

    logger = logging.getLogger("auditrum.makemigrations")
    models_by_table = {m._meta.db_table: m for m in apps.get_models()}

    grouped: dict[str, list[tuple[str, TrackSpec]]] = {}
    for key, spec in get_registered_specs():
        model = models_by_table.get(spec.table)
        if model is None:
            logger.warning(
                "auditrum: spec %r references db_table %r which is not a "
                "currently-installed model. Skipping. If you renamed the "
                "model, update the @track decorator to point at the new class.",
                key,
                spec.table,
            )
            continue
        grouped.setdefault(model._meta.app_label, []).append((key, spec))
    return grouped


def clear_registry() -> None:
    """Reset the registry. Test-only helper."""
    _registry.clear()
