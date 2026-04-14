"""Declarative audit trigger specification + bundle rendering.

:class:`TrackSpec` is a pure value object: immutable, hashable, no side
effects. Call :meth:`TrackSpec.build` to get a :class:`TriggerBundle` with
install/uninstall SQL and a deterministic checksum for drift detection.

Identifier validation lives in the constructors of :class:`FieldFilter`
and :class:`TrackSpec` so you cannot build an invalid spec.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from typing import Literal

from auditrum.tracking._template import render

_IDENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def validate_identifier(name: str, label: str) -> str:
    """Validate that ``name`` looks like a SQL identifier.

    Raises :class:`ValueError` if it doesn't. Used at every boundary
    that interpolates a user-supplied table / column / role name into
    SQL — primary line of defence against injection via identifier
    paths. Pair with :mod:`psycopg.sql` for value-side parameter
    binding to cover the entire surface.

    The regex is intentionally strict (``^[A-Za-z_][A-Za-z0-9_]*$``):
    no schema-qualified names, no quoted identifiers, no extended
    Unicode. Generate the public-API identifiers yourself if you need
    those — never let user input through this function.
    """
    if not isinstance(name, str) or not _IDENT_RE.match(name):
        raise ValueError(
            f"Invalid {label}: {name!r} (must match {_IDENT_RE.pattern})"
        )
    return name


# Internal alias kept for backwards compatibility with code that imported
# the underscore-prefixed name during 0.2 / 0.3 development. New code
# should import :func:`validate_identifier` directly.
_validate_ident = validate_identifier


FieldFilterKind = Literal["all", "only", "exclude"]


@dataclass(frozen=True)
class FieldFilter:
    """Which columns of the tracked table participate in the diff.

    Three mutually exclusive modes:

    * ``FieldFilter.all()`` — include every column (default)
    * ``FieldFilter.only(*fields)`` — whitelist; columns outside the list
      are stripped from ``old_data`` / ``new_data`` / ``diff`` before the
      audit row is written
    * ``FieldFilter.exclude(*fields)`` — blacklist; listed columns are
      stripped

    Constructors validate identifiers so invalid filter specs raise at
    build time, not at SQL execution time.
    """

    kind: FieldFilterKind = "all"
    fields: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        for f in self.fields:
            validate_identifier(f, f"{self.kind}_field")
        if self.kind == "all" and self.fields:
            raise ValueError("FieldFilter.all() must not carry field names")
        if self.kind in ("only", "exclude") and not self.fields:
            raise ValueError(
                f"FieldFilter.{self.kind}() requires at least one field"
            )

    @classmethod
    def all(cls) -> FieldFilter:
        return cls(kind="all", fields=())

    @classmethod
    def only(cls, *fields: str) -> FieldFilter:
        return cls(kind="only", fields=tuple(fields))

    @classmethod
    def exclude(cls, *fields: str) -> FieldFilter:
        return cls(kind="exclude", fields=tuple(fields))

    def to_ignored_keys_expr(self) -> str:
        """Render the PL/pgSQL expression that computes ``ignored_keys``."""
        if self.kind == "all":
            return "ARRAY[]::text[]"
        if self.kind == "exclude":
            quoted = ", ".join(f"'{k}'" for k in self.fields)
            return f"ARRAY[{quoted}]::text[]"
        # kind == "only" — invert: ignore everything not in the whitelist
        keys_tuple = "(" + ", ".join(f"'{k}'" for k in self.fields) + ")"
        return (
            "ARRAY(SELECT key FROM jsonb_object_keys(to_jsonb(NEW)) AS key(key) "
            f"WHERE key.key NOT IN {keys_tuple})::text[]"
        )


@dataclass(frozen=True)
class TrackSpec:
    """Declarative description of an audit trigger for one table.

    Immutable value object. Call :meth:`build` to render SQL; hashable and
    equality-comparable for use as dict keys in registries.

    Args:
        table: name of the tracked table
        audit_table: destination audit log table
        fields: which columns to diff
        extra_meta_fields: ``NEW.<field>`` references to capture into the
            ``meta`` jsonb column for per-row custom metadata
        log_condition: optional PL/pgSQL expression; trigger is a no-op
            when the expression is false. **Trusted input** — never pass
            user-supplied strings here.
        trigger_name: override default ``audit_{table}_trigger`` name
    """

    table: str
    audit_table: str = "auditlog"
    fields: FieldFilter = field(default_factory=FieldFilter.all)
    extra_meta_fields: tuple[str, ...] = ()
    log_condition: str | None = None
    trigger_name: str | None = None

    def __post_init__(self) -> None:
        validate_identifier(self.table, "table")
        validate_identifier(self.audit_table, "audit_table")
        for f in self.extra_meta_fields:
            validate_identifier(f, "extra_meta_field")
        if self.trigger_name is not None:
            validate_identifier(self.trigger_name, "trigger_name")

    @property
    def effective_trigger_name(self) -> str:
        return self.trigger_name or f"audit_{self.table}_trigger"

    @property
    def function_name(self) -> str:
        # function and trigger share the same name by convention
        return self.effective_trigger_name

    def _meta_expr(self) -> str:
        if not self.extra_meta_fields:
            return "NULL"
        pairs = ", ".join(
            f"'{f}', to_jsonb(NEW.{f})" for f in self.extra_meta_fields
        )
        return f"jsonb_build_object({pairs})"

    def _log_conditions_block(self) -> str:
        if self.log_condition is None:
            return ""
        return (
            f"\n    IF NOT ({self.log_condition}) THEN\n"
            f"        RETURN NULL;\n"
            f"    END IF;\n"
        )

    def build(self) -> TriggerBundle:
        """Render install/uninstall SQL + compute a drift-detection checksum."""
        install_sql = render(
            "audit_trigger.sql",
            function_name=self.function_name,
            trigger_name=self.effective_trigger_name,
            table_name=self.table,
            audit_table=self.audit_table,
            ignored_keys_expr=self.fields.to_ignored_keys_expr(),
            log_conditions_block=self._log_conditions_block(),
            meta_expr=self._meta_expr(),
        ).strip()

        uninstall_sql = (
            f"DROP TRIGGER IF EXISTS {self.effective_trigger_name} ON {self.table};\n"
            f"DROP FUNCTION IF EXISTS {self.function_name}() CASCADE;"
        )

        checksum = hashlib.sha256(install_sql.encode("utf-8")).hexdigest()

        return TriggerBundle(
            spec=self,
            function_name=self.function_name,
            trigger_name=self.effective_trigger_name,
            install_sql=install_sql,
            uninstall_sql=uninstall_sql,
            checksum=checksum,
        )

    def to_fingerprint(self) -> dict:
        """Serialize the spec to a JSON-safe dict for tracking-table storage."""
        return {
            "table": self.table,
            "audit_table": self.audit_table,
            "fields_kind": self.fields.kind,
            "fields": list(self.fields.fields),
            "extra_meta_fields": list(self.extra_meta_fields),
            "log_condition": self.log_condition,
            "trigger_name": self.trigger_name,
        }


@dataclass(frozen=True)
class TriggerBundle:
    """Rendered SQL for a single :class:`TrackSpec`."""

    spec: TrackSpec
    function_name: str
    trigger_name: str
    install_sql: str
    uninstall_sql: str
    checksum: str
