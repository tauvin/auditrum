"""Framework-agnostic audit trigger tracking.

Public API::

    from auditrum.tracking import (
        TrackSpec,
        FieldFilter,
        TriggerBundle,
        TriggerManager,
        TriggerStatus,
        SyncReport,
    )

    spec = TrackSpec(
        table="users",
        fields=FieldFilter.only("name", "email"),
        extra_meta_fields=("tenant_id",),
    )
    mgr = TriggerManager(executor)
    mgr.bootstrap()
    mgr.sync([spec])

The core is framework-agnostic: it takes any
:class:`auditrum.executor.ConnectionExecutor` and a list of declarative
:class:`TrackSpec` instances. Framework bridges (Django, SQLAlchemy, raw
psycopg) just translate their native model definitions into ``TrackSpec``
and call into this module.
"""

from auditrum.tracking.manager import (
    DiffEntry,
    SyncReport,
    TriggerAction,
    TriggerManager,
    TriggerStatus,
)
from auditrum.tracking.spec import FieldFilter, TrackSpec, TriggerBundle

__all__ = [
    "DiffEntry",
    "FieldFilter",
    "SyncReport",
    "TrackSpec",
    "TriggerAction",
    "TriggerBundle",
    "TriggerManager",
    "TriggerStatus",
]
