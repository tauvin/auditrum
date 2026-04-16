"""Backwards-compatible registration API.

Historical users imported :func:`register` from this module. The canonical
API is now :func:`auditrum.integrations.django.tracking.track` (decorator)
or :func:`auditrum.integrations.django.tracking.register` (imperative),
both backed by a pure :class:`auditrum.tracking.TrackSpec` registry. This
module re-exports them for drop-in compatibility.

``registry`` is exposed as a compatibility view — each entry is a dict
matching the old shape but the underlying state lives in
``auditrum.integrations.django.tracking._registry``.
"""

from __future__ import annotations

from typing import Any

from django.db.models import Model

from auditrum.integrations.django.tracking import _registry as _spec_registry
from auditrum.integrations.django.tracking import (
    register as _imperative_register,
)

__all__ = [
    "register",
    "registry",
]


def register(model_cls: type[Model], **kwargs: Any) -> None:
    """Legacy imperative register. See ``integrations.django.tracking.register``."""
    _imperative_register(model_cls, **kwargs)


class _LegacyRegistryView(dict):
    """Dict proxy exposing the tracking registry in the old dict-of-dicts shape."""

    def _snapshot(self) -> dict:
        # Build a Django-model-keyed view of the current spec registry
        from django.apps import apps

        out = {}
        models_by_table = {m._meta.db_table: m for m in apps.get_models()}
        for _, spec in _spec_registry.items():
            model = models_by_table.get(spec.table)
            if model is None:
                continue
            out[model] = {
                "table_name": spec.table,
                "fields": [f.name for f in model._meta.get_fields() if f.concrete],
                "track_only": (
                    list(spec.fields.fields) if spec.fields.kind == "only" else None
                ),
                "exclude_fields": (
                    list(spec.fields.fields) if spec.fields.kind == "exclude" else None
                ),
                "log_conditions": spec.log_condition,
                "extra_meta_fields": list(spec.extra_meta_fields),
            }
        return out

    def __iter__(self):
        return iter(self._snapshot())

    def __len__(self):
        return len(self._snapshot())

    def __contains__(self, key):
        return key in self._snapshot()

    def __getitem__(self, key):
        return self._snapshot()[key]

    def keys(self):  # type: ignore[override]
        return self._snapshot().keys()

    def values(self):  # type: ignore[override]
        return self._snapshot().values()

    def items(self):  # type: ignore[override]
        return self._snapshot().items()


registry = _LegacyRegistryView()
