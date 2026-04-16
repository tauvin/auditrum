"""Custom Django migration operations for auditrum triggers.

:class:`InstallTrigger` and :class:`UninstallTrigger` are real
:class:`django.db.migrations.operations.base.Operation` subclasses, so
they participate in the normal ``migrate`` / ``showmigrations`` /
``sqlmigrate`` flows. They delegate install and rollback work to
:class:`auditrum.tracking.TriggerManager` so the same logic backs the
Django and non-Django paths.

Each operation stores a :class:`TrackSpec` inline in the migration file
(via :meth:`Operation.deconstruct`), so the migration is
self-contained — no import-time registry lookup at ``migrate`` time.
"""

from __future__ import annotations

from typing import Any

from django.db.migrations.operations.base import Operation

from auditrum.tracking import FieldFilter, TrackSpec, TriggerManager

__all__ = [
    "InstallTrigger",
    "UninstallTrigger",
]


def _spec_to_deconstruct_kwargs(spec: TrackSpec) -> dict[str, Any]:
    kwargs: dict[str, Any] = {"table": spec.table}
    if spec.audit_table != "auditlog":
        kwargs["audit_table"] = spec.audit_table
    if spec.fields.kind != "all":
        kwargs["fields_kind"] = spec.fields.kind
        kwargs["fields"] = list(spec.fields.fields)
    if spec.extra_meta_fields:
        kwargs["extra_meta_fields"] = list(spec.extra_meta_fields)
    if spec.log_condition is not None:
        kwargs["log_condition"] = spec.log_condition
    if spec.trigger_name is not None:
        kwargs["trigger_name"] = spec.trigger_name
    return kwargs


def _kwargs_to_spec(
    *,
    table: str,
    audit_table: str = "auditlog",
    fields_kind: str = "all",
    fields: list[str] | None = None,
    extra_meta_fields: list[str] | None = None,
    log_condition: str | None = None,
    trigger_name: str | None = None,
) -> TrackSpec:
    if fields_kind == "only":
        field_filter = FieldFilter.only(*(fields or ()))
    elif fields_kind == "exclude":
        field_filter = FieldFilter.exclude(*(fields or ()))
    else:
        field_filter = FieldFilter.all()
    return TrackSpec(
        table=table,
        audit_table=audit_table,
        fields=field_filter,
        extra_meta_fields=tuple(extra_meta_fields or ()),
        log_condition=log_condition,
        trigger_name=trigger_name,
    )


def _make_manager(schema_editor) -> TriggerManager:
    from auditrum.integrations.django.executor import DjangoExecutor

    # ``schema_editor.connection`` is a Django ``DatabaseWrapper`` for the
    # alias this migration is running against. Wrap it in DjangoExecutor
    # so we go through Django's cursor protocol (respecting custom
    # cursor wrappers, query logging, etc.) rather than reaching into
    # ``connection.connection`` for the raw psycopg handle.
    return TriggerManager(DjangoExecutor(connection=schema_editor.connection))


class InstallTrigger(Operation):
    """Install (or update) an auditrum trigger for one tracked table.

    Stored inline in the generated migration file so ``migrate`` has
    everything it needs without importing the project's model registry.
    """

    reversible = True
    reduces_to_sql = False
    atomic = True

    def __init__(
        self,
        *,
        table: str,
        audit_table: str = "auditlog",
        fields_kind: str = "all",
        fields: list[str] | None = None,
        extra_meta_fields: list[str] | None = None,
        log_condition: str | None = None,
        trigger_name: str | None = None,
    ) -> None:
        self.spec = _kwargs_to_spec(
            table=table,
            audit_table=audit_table,
            fields_kind=fields_kind,
            fields=fields,
            extra_meta_fields=extra_meta_fields,
            log_condition=log_condition,
            trigger_name=trigger_name,
        )

    def deconstruct(self):
        kwargs = _spec_to_deconstruct_kwargs(self.spec)
        return (self.__class__.__name__, [], kwargs)

    def state_forwards(self, app_label, state):
        # Triggers don't alter the Django model graph — nothing to do.
        pass

    def database_forwards(self, app_label, schema_editor, from_state, to_state):
        mgr = _make_manager(schema_editor)
        mgr.bootstrap()
        mgr.install(self.spec, force=True)

    def database_backwards(self, app_label, schema_editor, from_state, to_state):
        mgr = _make_manager(schema_editor)
        mgr.bootstrap()
        mgr.uninstall(self.spec)

    def describe(self) -> str:
        return f"Install auditrum trigger on {self.spec.table}"

    @property
    def migration_name_fragment(self) -> str:
        return f"auditrum_install_{self.spec.table}"


class UninstallTrigger(Operation):
    """Remove an auditrum trigger from a previously tracked table."""

    reversible = True
    reduces_to_sql = False
    atomic = True

    def __init__(
        self,
        *,
        table: str,
        audit_table: str = "auditlog",
        fields_kind: str = "all",
        fields: list[str] | None = None,
        extra_meta_fields: list[str] | None = None,
        log_condition: str | None = None,
        trigger_name: str | None = None,
    ) -> None:
        self.spec = _kwargs_to_spec(
            table=table,
            audit_table=audit_table,
            fields_kind=fields_kind,
            fields=fields,
            extra_meta_fields=extra_meta_fields,
            log_condition=log_condition,
            trigger_name=trigger_name,
        )

    def deconstruct(self):
        kwargs = _spec_to_deconstruct_kwargs(self.spec)
        return (self.__class__.__name__, [], kwargs)

    def state_forwards(self, app_label, state):
        pass

    def database_forwards(self, app_label, schema_editor, from_state, to_state):
        mgr = _make_manager(schema_editor)
        mgr.bootstrap()
        mgr.uninstall(self.spec)

    def database_backwards(self, app_label, schema_editor, from_state, to_state):
        mgr = _make_manager(schema_editor)
        mgr.bootstrap()
        mgr.install(self.spec, force=True)

    def describe(self) -> str:
        return f"Uninstall auditrum trigger from {self.spec.table}"

    @property
    def migration_name_fragment(self) -> str:
        return f"auditrum_uninstall_{self.spec.table}"
