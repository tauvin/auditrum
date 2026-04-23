# Operator scripts

Ad-hoc SQL and tooling for running auditrum in production. These
are **not** part of the supported public API — they're utilities
that either poke at runtime state for troubleshooting, or collect
the numbers that eventually fill in `docs/performance.md` and the
case studies.

## `collect-perf-numbers.sql`

Pulls everything we need to fill in
[`docs/performance.md`](../docs/performance.md) and the
[catalog case study](../docs/case-studies/catalog.md) from a live
database. Non-invasive; read-only; skips gracefully if optional
extensions (pg_stat_statements) aren't installed.

### One-time PG configuration

Before the first collection, make sure the DB is recording the
right stats. In `postgresql.conf` (or via `ALTER SYSTEM` + reload):

```
track_functions = 'pl'
shared_preload_libraries = 'pg_stat_statements'
```

`track_functions = 'pl'` makes PostgreSQL count per-PL/pgSQL-function
time — without it, section 1 of the script is empty. Cost is
negligible (a single counter increment per call).

`pg_stat_statements` gives you per-statement mean execution time,
which is what sections 4 and the "INSERT roundtrip delta" in
performance.md rely on. It ships with Postgres but isn't always
loaded.

### Collection workflow

1. **Reset counters** (optional — skip if you want cumulative
   numbers since last restart):

   ```sql
   SELECT pg_stat_reset();
   SELECT pg_stat_reset_shared('statements');
   ```

2. **Wait for a representative traffic window.** A single business
   day is usually enough to catch the bulk of the distribution.
   A full week smooths out weekend / weekday variation.

3. **Run the collection script:**

   ```bash
   psql "$DATABASE_URL" -f scripts/collect-perf-numbers.sql \
       -v retention='1 year' \
       > perf-$(date +%Y%m%d).txt
   ```

   The `-v retention=...` binding parameterises section 3a's "lag
   beyond retention window" calculation. Pass whatever interval
   matches your retention policy.

4. **Review the output** against the placeholders in
   `docs/performance.md` and `docs/case-studies/catalog.md`. Fill
   in the tables. Commit.

### What each section feeds

| Script section                      | Docs table / prose                                          |
|-------------------------------------|-------------------------------------------------------------|
| 1. per-trigger timings              | `performance.md` → "Trigger overhead" table, `self_ms` col  |
| 2. event rate + volume              | `performance.md` → "Audit events / second", volume numbers  |
| 3. partition disk size              | `performance.md` → "Monitoring" prose, dashboard context    |
| 3a. retention lag                   | `performance.md` → operational runbook reference            |
| 3b. avg row size                    | `case-studies/catalog.md` → "Workload shape" prose          |
| 4. INSERT roundtrip via pg_stat_statements | `performance.md` → "Trigger overhead" table, application-side delta |
| 5. INSERT integrity cross-check     | operational sanity (goes in case-study "What broke" if applicable) |
| 6. hash chain status                | `case-studies/catalog.md` → "Workload shape" prose          |
| 7. context source distribution      | `case-studies/catalog.md` → integration-patterns section    |

### What the script does not measure

* **Concurrency ceiling.** The chain's advisory-lock contention only
  shows up under parallel writers. Use `pgbench` or a real load
  generator; see `benchmarks/README.md`.
* **Absolute trigger cost with no audit.** Requires an untracked
  comparison table. The benchmark suite in `benchmarks/` gets you
  this on a staging restore; production doesn't have an obvious
  untracked control table.
* **Latency distribution.** `pg_stat_user_functions` gives you
  cumulative mean, not p95/p99. For percentiles, enable the
  Prometheus `AuditrumCollector` + a trigger-duration histogram
  (currently emits event-rate gauges only; histogram support is
  a future addition — see `ROADMAP.md`).
