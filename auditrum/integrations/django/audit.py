from typing import Any, Callable, Dict, Type

from django.db.models import Model

registry: Dict[Type[Model], Dict[str, Any]] = {}


def register(
    model_cls: Type[Model],
    *,
    track_only=None,
    exclude_fields=None,
    log_conditions=None,
    meta_fields=None
) -> None:
    if track_only and exclude_fields:
        raise ValueError(
            f"Cannot define both `track_only` and `exclude_fields` on model {model_cls.__name__}"
        )
    registry[model_cls] = {
        "table_name": model_cls._meta.db_table,
        "fields": [f.name for f in model_cls._meta.get_fields() if f.concrete],
        "track_only": track_only,
        "exclude_fields": exclude_fields,
        "log_conditions": log_conditions,
        "meta_fields": meta_fields,
    }
