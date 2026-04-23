# Grafana dashboards

Production-ready dashboards for operators running auditrum. Drop the
JSON into **Dashboards → Import**, point it at your Prometheus data
source, and the panels populate from metrics exposed by
`auditrum.observability.prometheus.AuditrumCollector`.

## Included dashboards

* **`auditrum-overview.json`** — the single page most users want on
  their wall: audit event rate by table and operation, trigger
  latency p50/p95/p99, hash chain status, partition disk usage,
  retention lag. One row per concern so an operator can scan
  "everything's fine" at a glance.

## What you need on the cluster

1. `AuditrumCollector` registered with `prometheus_client.REGISTRY`
   in your Django process:

   ```python
   from auditrum.observability.prometheus import AuditrumCollector
   AuditrumCollector()   # registers itself on import
   ```

2. Prometheus scraping the `/metrics` endpoint that exposes
   `prometheus_client`'s registry.

3. The following Postgres-side metrics, exported by `postgres_exporter`
   or any equivalent:

   * `pg_database_size_bytes{datname="<your db>"}`
   * `pg_stat_user_tables_n_live_tup{relname=~"auditlog.*"}`
   * `pg_total_relation_size_bytes{relname=~"auditlog_.*"}`

   The partition-usage panel needs the last two. If your exporter
   uses different metric names, edit the JSON's `expr:` fields
   accordingly — the templating language is Prometheus PromQL, not
   auditrum-specific.

## Notes on the PromQL

* Latency panels use a `histogram_quantile(…, rate(… [5m]))` pattern
  against the `auditrum_trigger_duration_seconds_bucket` histogram.
  If you pre-aggregate with recording rules, point the panels at the
  recording rule name instead of the raw histogram.
* Retention lag is computed as `(now - oldest partition start) - <your
  retention window>`; if you run non-monthly partitions, replace
  the `month` unit in the panel expression.

## What the numbers look like in practice

Real-world numbers from catalog's production deployment land in
[`docs/performance.md`](../../docs/performance.md) under the
"Monitoring" section. The dashboards themselves are meant to be
usable **before** you've collected those numbers — they show the
right panels with the right y-axis units so you're not re-discovering
what to look at when you're already on fire.
