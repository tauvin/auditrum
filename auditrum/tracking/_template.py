"""Strict template loader for PL/pgSQL bodies.

Templates live as ``.sql`` files next to this module and are rendered via
``str.format_map`` with :class:`_StrictMap` so any missing placeholder
raises a loud :class:`KeyError` at render time instead of silently leaving
an unresolved ``{name}`` in the generated SQL.

The rendering step is a pure string substitution — no conditionals, no
loops. Any conditional logic happens in Python before render: the caller
pre-computes fragments (e.g. ``log_conditions_block`` is either ``""`` or a
validated PL/pgSQL snippet) and passes them in.
"""

from __future__ import annotations

import string
from functools import cache
from importlib.resources import files
from typing import Any


class _StrictMap(dict):
    """``dict`` subclass whose ``__missing__`` raises with a helpful message.

    Used with :meth:`str.format_map` so templates never render with
    unresolved ``{placeholder}`` fragments — any typo or missed binding is
    caught at the call site, not in production SQL.
    """

    def __missing__(self, key: str) -> Any:
        raise KeyError(
            f"auditrum template placeholder {{{key}}} has no binding. "
            f"Provided keys: {sorted(self.keys())}"
        )


@cache
def load_template(name: str) -> str:
    """Load a template file from ``auditrum.tracking.templates``.

    Cached on first load; templates are read-only after install so the
    cache never becomes stale within a process.
    """
    pkg = files("auditrum.tracking.templates")
    return pkg.joinpath(name).read_text(encoding="utf-8")


def render(template_name: str, **bindings: Any) -> str:
    """Load and render a template with strict placeholder checking.

    Verifies that **every** placeholder in the template has a value bound
    in ``bindings`` — extra keys in ``bindings`` are tolerated (for
    forward-compat), but missing keys raise :class:`KeyError` immediately.
    """
    template = load_template(template_name)

    template_keys = {
        field_name
        for _, field_name, _, _ in string.Formatter().parse(template)
        if field_name is not None and field_name != ""
    }
    missing = template_keys - set(bindings)
    if missing:
        raise KeyError(
            f"Template {template_name!r} requires placeholders "
            f"{sorted(missing)!r} that were not provided. "
            f"Provided: {sorted(bindings.keys())!r}"
        )

    return template.format_map(_StrictMap(bindings))
