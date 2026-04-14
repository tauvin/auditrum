"""Legacy trigger SQL generation facade.

Historical users imported :func:`generate_trigger_sql` and
:func:`build_trigger_spec` from this module. The canonical home for
trigger generation is now :mod:`auditrum.tracking`, but these thin
wrappers remain for backwards compatibility with existing code and the
CLI / raw-SQL path.
"""

from __future__ import annotations

from dataclasses import dataclass

from auditrum.tracking.spec import (
    FieldFilter,
    TrackSpec,
    _validate_ident,
    validate_identifier,
)

__all__ = [
    "TriggerSpec",
    "build_trigger_spec",
    "generate_trigger_sql",
    "validate_identifier",
    # Legacy alias, kept for callers that imported the underscore-prefixed
    # name during 0.2 / 0.3 development.
    "_validate_ident",
]


@dataclass(frozen=True)
class TriggerSpec:
    """Legacy rendered trigger view.

    Preserved for callers that consumed the ``(declare, body, sql)`` shape
    from the old :func:`build_trigger_spec` return value. New code should
    use :class:`auditrum.tracking.TrackSpec` and its ``build()`` method.
    """

    table_name: str
    audit_table: str
    function_name: str
    trigger_name: str
    declare: tuple[tuple[str, str], ...]
    body: str
    sql: str


def _to_track_spec(
    table_name: str,
    audit_table: str,
    track_only: list[str] | None,
    exclude_fields: list[str] | None,
    log_conditions: str | None,
    extra_meta_fields: list[str] | None,
) -> TrackSpec:
    if track_only is not None and exclude_fields is not None:
        raise ValueError(
            f"Cannot specify both track_only and exclude_fields for table {table_name}"
        )
    # Preserve historical error labels for the legacy facade. The tracking
    # module uses terser labels internally; users of the raw ``generate_*``
    # helpers see the original parameter names in the error.
    validate_identifier(table_name, "table_name")
    validate_identifier(audit_table, "audit_table")
    if track_only is not None:
        for f in track_only:
            validate_identifier(f, "track_only field")
        fields = FieldFilter.only(*track_only)
    elif exclude_fields is not None:
        for f in exclude_fields:
            validate_identifier(f, "exclude_fields field")
        fields = FieldFilter.exclude(*exclude_fields)
    else:
        fields = FieldFilter.all()
    if extra_meta_fields:
        for f in extra_meta_fields:
            validate_identifier(f, "extra_meta_fields field")
    return TrackSpec(
        table=table_name,
        audit_table=audit_table,
        fields=fields,
        log_condition=log_conditions,
        extra_meta_fields=tuple(extra_meta_fields or ()),
    )


def build_trigger_spec(
    table_name: str,
    audit_table: str = "auditlog",
    track_only: list[str] | None = None,
    exclude_fields: list[str] | None = None,
    log_conditions: str | None = None,
    extra_meta_fields: list[str] | None = None,
) -> TriggerSpec:
    """Build a legacy :class:`TriggerSpec` (identifier-validated)."""
    spec = _to_track_spec(
        table_name,
        audit_table,
        track_only,
        exclude_fields,
        log_conditions,
        extra_meta_fields,
    )
    bundle = spec.build()
    # Legacy callers consumed ``declare`` as a tuple of ``(name, type)`` pairs;
    # the template no longer exposes these as structured data, so we rebuild a
    # minimal view here for callers that still want it.
    declare: tuple[tuple[str, str], ...] = (
        ("data", "JSONB"),
        ("diff", "JSONB"),
        ("ignored_keys", f"TEXT[] := {spec.fields.to_ignored_keys_expr()}"),
        ("old_filtered", "jsonb := to_jsonb(OLD)"),
        ("new_filtered", "jsonb := to_jsonb(NEW)"),
        ("key", "text"),
    )
    return TriggerSpec(
        table_name=spec.table,
        audit_table=spec.audit_table,
        function_name=bundle.function_name,
        trigger_name=bundle.trigger_name,
        declare=declare,
        body="",  # body is no longer exposed as a separate string
        sql=bundle.install_sql,
    )


def generate_trigger_sql(
    table_name: str,
    audit_table: str = "auditlog",
    track_only: list[str] | None = None,
    exclude_fields: list[str] | None = None,
    log_conditions: str | None = None,
    extra_meta_fields: list[str] | None = None,
) -> str:
    """Render full ``CREATE FUNCTION`` + ``CREATE TRIGGER`` SQL for one table.

    Thin wrapper around :meth:`TrackSpec.build`. Identifiers are validated
    via :func:`_validate_ident` (reused from :mod:`auditrum.tracking.spec`)
    so SQL injection through user-supplied names is blocked at construction
    time. ``log_conditions`` is trusted PL/pgSQL — never pass user input.
    """
    spec = _to_track_spec(
        table_name,
        audit_table,
        track_only,
        exclude_fields,
        log_conditions,
        extra_meta_fields,
    )
    return spec.build().install_sql
