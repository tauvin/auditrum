# Performance

This page documents **how** we measure auditrum's performance and
**what the numbers mean** — so a reader can either trust the
published figures below or reproduce them on their own hardware.

> **Numbers below are placeholders.** They land once catalog's
> production workload has run on 0.4 for a full retention window.
> See the _"What you can do right now"_ section for the methodology.

## How we measure

Three hot paths, three benchmark files:

| Path                 | Benchmark file                                | Metric unit | What it tells you                                                                  |
|----------------------|-----------------------------------------------|-------------|------------------------------------------------------------------------------------|
| Trigger overhead     | ``benchmarks/test_trigger_overhead.py``       | µs / op     | Marginal cost of auditrum on your write path. Report as (tracked − untracked).    |
| Hash chain write     | ``benchmarks/test_hash_chain.py``             | µs / op     | The ceiling the advisory lock imposes on insert throughput.                       |
| Time-travel latency  | ``benchmarks/test_time_travel.py``            | µs / op     | Reconstruction speed at N events of history.                                      |

All three use ``pytest-benchmark`` with a fresh testcontainer
Postgres per session. Measurements include the Python ↔ Postgres
round-trip, which is why every "tracked" benchmark has an
"untracked" baseline; the **delta** is the reported number, not the
absolute timing.

```bash
# Reproduce on your hardware
uv sync --extra benchmark
uv run pytest benchmarks/ --benchmark-only --benchmark-columns=mean,stddev,ops
```

Save a baseline and gate regressions:

```bash
uv run pytest benchmarks/ --benchmark-only --benchmark-save=baseline
# ... make a change ...
uv run pytest benchmarks/ --benchmark-only \
    --benchmark-compare=baseline \
    --benchmark-compare-fail=mean:20%
```

## What the numbers will say (skeleton)

Once catalog numbers land, this section fills in with a table per
workload. The skeleton below is what the shape looks like.

### Trigger overhead (PG 16, Python 3.13, Django 5.1)

| Operation | Untracked (baseline) | ``FieldFilter.all()`` | ``FieldFilter.only(*2)`` | ``log_condition`` short-circuit |
|-----------|----------------------|-----------------------|--------------------------|---------------------------------|
| INSERT    | _tbd_ µs             | _tbd_ µs (+_X_%)      | _tbd_ µs (+_Y_%)         | _tbd_ µs (+_Z_%)                 |
| UPDATE    | _tbd_ µs             | _tbd_ µs (+_X_%)      | _tbd_ µs (+_Y_%)         | _tbd_ µs (+_Z_%)                 |
| DELETE    | _tbd_ µs             | _tbd_ µs (+_X_%)      | _tbd_ µs (+_Y_%)         | n/a                             |

What we expect to see, qualitatively: INSERTs and DELETEs pay a
constant cost per row; UPDATEs pay that plus a ``jsonb_diff`` step
that scales with the number of tracked fields. ``log_condition``
short-circuit should be near-free — it's a branch on the PL/pgSQL
entry, not a full trigger body execution.

### Hash chain ceiling (PG 16, single writer)

| Scenario                           | µs / insert | ops / sec |
|------------------------------------|-------------|-----------|
| Audited, chain disabled             | _tbd_       | _tbd_     |
| Audited, chain enabled (uncontended) | _tbd_      | _tbd_     |
| Audited, chain enabled, 4 writers   | _tbd_       | _tbd_     |
| Audited, chain enabled, 16 writers  | _tbd_       | _tbd_     |

The concurrency rows come from catalog load tests, not the
microbenchmark suite — the single-writer number from
``benchmarks/`` is the floor, not the ceiling.

### Time-travel latency (100-event row history)

| Call                                      | µs / op |
|-------------------------------------------|---------|
| ``reconstruct_row(at=latest)``            | _tbd_   |
| ``reconstruct_row(at=halfway)``           | _tbd_   |
| ``reconstruct_row(at=earliest)``          | _tbd_   |
| ``reconstruct_field_history('field')``    | _tbd_   |

Latency scales linearly in history depth — the composite
``(table_name, object_id, changed_at DESC)`` index is what keeps
it from becoming quadratic. The ``EXPLAIN`` in
``tests/integration/test_timetravel_pg.py`` asserts the index is
used; if it regressed we'd catch it there.

### Streaming memory footprint

``reconstruct_table(engine, table='…', at=…, stream=True)`` uses a
server-side named cursor. Expected memory footprint: bounded by
Python's cursor buffer, independent of result set size.

Planned measurement: 10M-row audit log, iterate all surviving rows
at a target timestamp, track RSS growth with ``tracemalloc`` +
``resource.getrusage()`` — land the numbers here once catalog has
enough history to make the test meaningful.

## CI matrix

See ``.github/workflows/ci.yml``:

| Axis         | Cells                              |
|--------------|------------------------------------|
| PostgreSQL   | 13, 14, 15, 16, 17                 |
| Python       | 3.11, 3.12, 3.13                    |
| Django       | 4.2 LTS, 5.1                       |

The matrix is slimmed to ~6 cells along a diagonal plus
full-PG-coverage for the newest Python/Django pair — running the
full 30-cell cartesian product is expensive for a library that
mostly interacts with Postgres through the same ``psycopg``
interface on every cell.

## Monitoring in production

Once auditrum is live:

* Prometheus collector in
  ``auditrum.observability.prometheus.AuditrumCollector`` emits per-
  ``(table, operation)`` rates and per-table trigger duration
  histograms.
* Grafana dashboard JSON in
  [``examples/grafana/``](../examples/grafana/) renders those as the
  three most common operational questions: "are events flowing?",
  "is trigger latency OK?", "is the chain intact?".
* Hash chain verification runs as a scheduled job
  (``verify_chain(conn, expected_tip=…)`` with an anchor from the
  previous run); its ``last_verify_ok`` gauge is what the dashboard's
  "hash chain status" panel reads.

## How to read the numbers responsibly

* **Compare deltas, not absolutes.** The untracked baseline
  benchmark exists so you can subtract your hardware's PG-INSERT
  cost from the reported figure. Absolute µs are meaningless across
  machines.
* **A single benchmark run is noise.** ``pytest-benchmark`` captures
  stddev alongside mean; if stddev is ≥ 10% of mean, rerun.
  ``--benchmark-warmup=on`` is enabled by default and helps, but
  containerised Postgres on an unrelated-workload machine can still
  produce noisy runs.
* **Catalog's numbers are one workload.** Catalog is
  write-heavy and bursty; a read-heavy workload will show different
  tracings. Don't assume catalog's numbers transfer linearly.
