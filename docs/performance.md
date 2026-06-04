# Performance

This page documents **how** we measure auditrum's performance and
**what the numbers mean** — so a reader can either trust the
published figures below or reproduce them on their own hardware.

> **The figures below are preliminary reference-hardware numbers**,
> not production data. They come from a single benchmark run on an
> Apple M3 Pro under Docker Desktop (PostgreSQL 16-alpine in a
> testcontainer, Python 3.13, psycopg 3.3). They establish
> order-of-magnitude overhead only — Docker Desktop's macOS VM
> inflates absolute timings, and some operations (DELETE, the
> no-chain INSERT baseline) showed high run-to-run variance. They
> will be replaced by catalog's production numbers once its workload
> has run on a real server for a full retention window. **Trust the
> deltas, not the absolute µs**, and reproduce on your own hardware
> with the commands below.

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

## Preliminary numbers (reference hardware)

The tables below are the first measured pass — single run, Apple M3
Pro, Docker Desktop, PostgreSQL 16-alpine, Python 3.13, psycopg 3.3.
Values are **medians** (more robust than the mean against the VM's
jitter). Cells marked **—** have no benchmark yet; rows sourced from
catalog load tests are called out as such. Catalog production numbers
will supersede these.

### Trigger overhead (PG 16-alpine, Python 3.13, psycopg 3.3 — Apple M3 Pro / Docker Desktop)

Overhead is the marginal cost over the untracked baseline (median µs;
percent is relative to that row's baseline).

| Operation | Untracked (baseline) | ``FieldFilter.all()`` | ``FieldFilter.only(*2)`` | ``log_condition`` short-circuit |
|-----------|----------------------|-----------------------|--------------------------|---------------------------------|
| INSERT    | 159 µs               | 232 µs (+46%)         | 226 µs (+42%)            | —                               |
| UPDATE    | 183 µs               | 205 µs (+12%)         | —                        | 193 µs (+6%)                    |
| DELETE    | 363 µs               | 474 µs (+31%)¹        | —                        | n/a                             |

¹ DELETE showed high variance on this run (stddev ≈ median); treat
the +31% as indicative only until a quieter / bare-metal rerun.

Two honest caveats from this dataset: (1) ``FieldFilter.only(*2)``
barely beats ``.all()`` on INSERT here because the benchmark table has
only three columns — the filter saves little until the tracked row is
wide (catalog's tables are). (2) The absolute baselines (e.g. DELETE
at 363 µs) are inflated by Docker Desktop's macOS VM; on bare-metal
Linux they drop substantially, but the *deltas* are what transfer.

What we expect to see, qualitatively: INSERTs and DELETEs pay a
constant cost per row; UPDATEs pay that plus a ``jsonb_diff`` step
that scales with the number of tracked fields. ``log_condition``
short-circuit should be near-free — it's a branch on the PL/pgSQL
entry, not a full trigger body execution.

### Hash chain ceiling (PG 16, single writer)

| Scenario                            | µs / insert | ops / sec    |
|-------------------------------------|-------------|--------------|
| Audited, chain disabled             | 294 µs²     | ~3,400       |
| Audited, chain enabled (uncontended)| 322 µs      | ~3,100       |
| Audited, chain enabled, 4 writers   | — (catalog load test) | —  |
| Audited, chain enabled, 16 writers  | — (catalog load test) | —  |

² The chain-disabled baseline was the noisiest run in the whole suite
(stddev > median); the ~28 µs delta the advisory lock adds is the
takeaway, not the absolute. ops/sec is derived from the median.

The concurrency rows come from catalog load tests, not the
microbenchmark suite — the single-writer number from
``benchmarks/`` is the floor, not the ceiling.

### Time-travel latency (100-event row history)

| Call                                      | µs / op |
|-------------------------------------------|---------|
| ``reconstruct_row(at=latest)``            | 225 µs  |
| ``reconstruct_row(at=halfway)``           | —       |
| ``reconstruct_row(at=earliest)``          | —       |
| ``reconstruct_field_history('field')``    | 702 µs  |

Measured on the 100-UPDATE history fixture
(``benchmarks/test_time_travel.py``). The ``at=halfway`` /
``at=earliest`` rows aren't benchmarked yet — only the latest-revision
read and the full field history are.

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
| Python       | 3.11, 3.12, 3.13, 3.14             |
| Django       | 4.2 LTS, 5.2 LTS, 6.0             |

The matrix is a 6-cell diagonal covering every Postgres version and
every supported Python/Django at least once with a valid pairing —
running the full 60-cell (5×4×3) cartesian product is expensive for a
library that mostly interacts with Postgres through the same
``psycopg`` interface on every cell. Support policy is the current
Django LTS lines plus the latest feature release.

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
