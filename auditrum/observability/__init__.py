"""Optional observability integrations: OpenTelemetry, Prometheus, Sentry.

All three are **soft dependencies** — the helpers gracefully no-op when
the corresponding library is not installed. Add them via extras::

    pip install auditrum[observability]

Usage::

    # OpenTelemetry: automatic via auditrum_context if opentelemetry is installed
    from auditrum.observability.otel import enrich_metadata

    # Prometheus: register a collector with prometheus_client
    from auditrum.observability.prometheus import AuditrumCollector
    from prometheus_client import REGISTRY
    REGISTRY.register(AuditrumCollector(conn_factory=lambda: psycopg.connect(dsn)))

    # Sentry: add breadcrumbs for audit context entries
    from auditrum.observability.sentry import add_breadcrumb_for_context
"""

from auditrum.observability.otel import enrich_metadata
from auditrum.observability.prometheus import AuditrumCollector
from auditrum.observability.sentry import add_breadcrumb_for_context

__all__ = [
    "enrich_metadata",
    "AuditrumCollector",
    "add_breadcrumb_for_context",
]
