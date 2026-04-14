# Observability

auditrum ships three optional, composable observability hooks:

- **OpenTelemetry** — automatically enriches every audit context with
  the current trace id and span id
- **Prometheus** — windowed event counters as a regular
  `prometheus_client` collector
- **Sentry** — breadcrumb on each context entry, so exceptions include
  the audit metadata trail

All three are **soft dependencies**. They're no-ops when their
underlying library is not installed, so you can call into them
unconditionally from your code without paying any runtime cost in
environments that don't have them.

## Install

```bash
pip install "auditrum[observability]"
```

This adds:

- `opentelemetry-api>=1.20`
- `prometheus-client>=0.17`
- `sentry-sdk>=1.30`

You can also install any subset via the standard pip `pip install
opentelemetry-api prometheus-client` — the extras group is just a
convenience.

## OpenTelemetry

The killer feature. When `opentelemetry-api` is importable, every
`auditrum_context(...)` block automatically inspects the current span
and merges `trace_id` / `span_id` into the audit context metadata.
That metadata flows into the PL/pgSQL `_audit_attach_context()` function
via the session GUC pipeline and ends up in
`audit_context.metadata` jsonb.

Result: every audit event in the database carries the OTLP-format
hex trace id, and you can **join distributed traces directly against
database-side audit events**.

```python
# your existing OTel setup
from opentelemetry import trace

tracer = trace.get_tracer("myapp")

with tracer.start_as_current_span("checkout") as span:
    order.status = "paid"
    order.save()
# Audit event for orders:42 now has metadata.trace_id = <the OTLP trace id>
```

### Querying trace-linked audit events

```sql
SELECT a.changed_at, a.operation, a.diff, c.metadata->>'url' AS url
FROM auditlog a
LEFT JOIN audit_context c ON c.id = a.context_id
WHERE c.metadata->>'trace_id' = 'a7bb5c7d9f6e4a22...';
```

Or via the Django manager:

```python
AuditLog.objects.filter(context__metadata__trace_id="a7bb5c7d9f6e4a22...")
```

Which pairs with a link to Jaeger / Tempo / Honeycomb / whatever
collector is storing the full trace. You get the distributed flow
(service A → service B → DB) and the row-level data change in the
same story.

### Manual enrichment

If you're not on Django and want the enrichment anyway, call
`enrich_metadata` yourself when building the context dict:

```python
from auditrum.observability.otel import enrich_metadata

metadata = {"user_id": 42, "source": "worker"}
enrich_metadata(metadata)
# metadata now also has trace_id and span_id if an active OTel span exists

with audit_context_block(**metadata):
    # your work
    ...
```

The function mutates the dict in place and also returns it for
chaining. Existing `trace_id` / `span_id` keys are **preserved** — the
enrichment uses `setdefault` semantics so manual assignments take
precedence.

### Gotchas

- Span must be **recording** and its context must be `is_valid` for
  enrichment to kick in. Non-recording spans and invalid contexts are
  silently ignored.
- If OTel throws (e.g. a broken Context object), enrichment catches
  the exception and logs nothing. Observability never crashes the app.
- The `trace_id` is formatted as 32-char lowercase hex, `span_id` as
  16-char lowercase hex — matches the OTLP wire format, which is what
  all the major backends index on.

## Prometheus metrics

Registers a pull-based collector with `prometheus_client` that exposes
one gauge:

- `auditrum_events{table="...", operation="INSERT|UPDATE|DELETE"}` —
  count of events in the last N seconds, grouped by table and operation

```python
import psycopg
from prometheus_client import REGISTRY, start_http_server
from auditrum.observability.prometheus import AuditrumCollector

def _conn_factory():
    return psycopg.connect("postgresql://metrics_user:...@host/db")

collector = AuditrumCollector(
    _conn_factory,
    window_seconds=60,
    audit_table="auditlog",
)
REGISTRY.register(collector)

start_http_server(9090)
```

Each scrape does a lightweight `GROUP BY table_name, operation` over
events from the last 60 seconds. The resulting timeseries are ideal for
"rate of DELETE operations on `users` over time" alerts, or "no audit
events happened in the last 5 minutes — is the app broken?" sentinels.

### Connection handling

`conn_factory` is a zero-arg callable returning a connection. The
collector **closes** the connection after each scrape unless the
callable reuses one. If you want a persistent connection, your factory
can maintain it:

```python
_shared = [None]

def _conn_factory():
    if _shared[0] is None or _shared[0].closed:
        _shared[0] = psycopg.connect(dsn)
    return _shared[0]
```

### Safety

If the factory throws or the query fails, `collect()` returns the
gauge without samples rather than raising. A broken metrics endpoint
never takes down the `/metrics` scrape.

### Suggested alerts

```yaml
# Prometheus rules
- alert: AuditrumAuditLogStalled
  expr: sum(auditrum_events) == 0
  for: 5m
  labels: { severity: critical }
  annotations:
    summary: "No audit events in the last 5 minutes"
    description: "Either the app is fully idle, or the audit pipeline is broken. Investigate."

- alert: AuditrumDeleteBurst
  expr: sum by (table) (rate(auditrum_events{operation="DELETE"}[1m])) > 100
  for: 2m
  annotations:
    summary: "Unusually high DELETE rate on {{ $labels.table }}"
```

## Sentry breadcrumbs

Attaches an audit-context breadcrumb to the current Sentry scope when
a context block is entered. If an exception is captured later in the
same request, the Sentry event includes the audit metadata (user,
source, request id, optional change reason) as context trail.

Automatic via the Django `auditrum_context` hook. If you want manual
control or you're on a non-Django framework:

```python
from auditrum.observability.sentry import add_breadcrumb_for_context

metadata = {
    "source": "cli",
    "user_id": 42,
    "command": "rebalance_accounts",
}
add_breadcrumb_for_context(metadata)
```

Breadcrumb shape:

```
category: auditrum
level:    info
message:  audit context cli
data:     {source: cli, user_id: 42, command: rebalance_accounts}
```

No-op if `sentry_sdk` is not installed. Swallows any exceptions raised
by the SDK so observability never cascades into application failure.

## Combining everything

In a typical Django + OTel + Prometheus + Sentry deployment, the whole
observability story is **three lines of setup code** plus the
middleware:

```python
# settings.py — INSTALLED_APPS and MIDDLEWARE wire up audit context
INSTALLED_APPS = [..., "auditrum.integrations.django"]
MIDDLEWARE = [..., "auditrum.integrations.django.middleware.AuditrumMiddleware", ...]

# app startup
from prometheus_client import REGISTRY, start_http_server
from auditrum.observability.prometheus import AuditrumCollector
from django.db import connection as _conn

REGISTRY.register(AuditrumCollector(lambda: _conn.connection, window_seconds=60))
start_http_server(9090)

# OpenTelemetry just works if your Django app has OTel tracing set up.
# Sentry breadcrumbs just work if sentry_sdk is initialised.
```

After that, every request produces:

- An OTel span (from your existing Django instrumentation)
- An audit context row linked to that span's trace id
- Audit events linked to that context row
- A Prometheus gauge ticking up
- A Sentry breadcrumb that shows up in any error captured in the request

## What's next

- [Architecture](architecture.md) — how the context pipeline enables
  all of this without per-request hot paths
- [Hardening](hardening.md) — alert on `verify-chain` failures in the
  same Prometheus/Sentry setup
