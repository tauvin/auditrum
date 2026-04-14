"""OpenTelemetry auto-enrichment for audit context metadata.

When an :class:`auditrum_context` block is entered, we read the current
OTel span (if any) and merge its ``trace_id`` / ``span_id`` into the
audit context metadata. Those fields then flow into the PostgreSQL
``audit_context.metadata`` jsonb column via the usual
``set_config`` → ``_audit_attach_context()`` pipeline, giving you a
direct join between distributed traces and database-side audit events.

Design rule: **never raise**. If ``opentelemetry`` is not installed or
there's no active span, the enrichment is a no-op. Users with no OTel
setup pay zero runtime cost beyond a Python-level ImportError catch.
"""

from __future__ import annotations

from typing import Any


def enrich_metadata(metadata: dict[str, Any]) -> dict[str, Any]:
    """Merge OTel trace identifiers into ``metadata`` if possible.

    Mutates and returns ``metadata`` so the call can be chained. Existing
    ``trace_id`` / ``span_id`` values are preserved — manual assignment
    takes precedence over the auto-enrichment.
    """
    try:
        from opentelemetry import trace
    except ImportError:
        return metadata

    try:
        span = trace.get_current_span()
    except Exception:
        return metadata

    if span is None:
        return metadata

    ctx = span.get_span_context()
    if not getattr(ctx, "is_valid", False):
        return metadata

    # OTel trace_id / span_id are ints; format as canonical hex strings
    # matching the OTLP wire format so they join cleanly against
    # Jaeger/Tempo/Honeycomb backends.
    try:
        metadata.setdefault("trace_id", format(ctx.trace_id, "032x"))
        metadata.setdefault("span_id", format(ctx.span_id, "016x"))
    except Exception:
        pass

    return metadata
