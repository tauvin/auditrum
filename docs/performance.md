# Performance

This page documents **how** we measure auditrum's performance and
**what the numbers mean** — so a reader can either trust the
published figures below or reproduce them on their own hardware.

> **Where the numbers come from.** The _Reference microbenchmark_
> section is a reproducible synthetic suite you can run on your own
> hardware. It ran once on an Apple M3 Pro under Docker Desktop, whose
> macOS VM inflates absolute timings — **trust deltas over absolute µs**
> there. Qualitative conclusions from real production use are in
> _Field observations_.

## How we measure

Three hot paths, three benchmark files:

| Path                 | Benchmark file                                | Metric unit | What it tells you                                                                  |
|----------------------|-----------------------------------------------|-------------|------------------------------------------------------------------------------------|
| Trigger overhead     | ``benchmarks/test_trigger_overhead.py``       | µs / op     | Marginal cost of auditrum on your write path. Report as (tracked − untracked).    |
| Hash chain write     | ``benchmarks/test_hash_chain.py``             | µs / op     | The ceiling the advisory lock imposes on insert throughput.                       |
| Time-travel latency  | ``benchmarks/test_time_travel.py``            | µs / op     | Reconstruction speed at N events of history.                                      |
| Streaming memory     | ``benchmarks/test_stream_memory.py``          | MB (peak)   | Peak Python heap of ``reconstruct_table(stream=True)`` vs the materialised path.  |

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

## Field observations

Anonymised, qualitative conclusions from early production use. No
absolute figures are published here — they depend too much on the
workload to transfer.

- **Trigger overhead scales with row width.** It is not a fixed
  constant: wide rows produce larger ``jsonb`` diffs and cost more per
  write, and the cost is dominated by row width and by maintaining
  ``auditlog``'s indexes rather than by the trigger logic itself.
- **Time-travel lookups stay fast.** Latency grows sub-linearly with
  history depth thanks to the composite
  ``(table_name, object_id, changed_at DESC)`` index, which is used on
  every partition.
- **The GIN index on ``diff`` is pure write tax unless you query it.**
  It saw zero scans yet cost hundreds of MB per monthly partition, and
  it is re-maintained on every audited write. That is why it became
  opt-in (issue #6).
- **A legacy audit trigger next to auditrum doubles the per-write audit
  cost.** If you adopted auditrum on top of an in-house audit trigger,
  remove the legacy one — see
  [Recipe 5](migration-cookbook.md#recipe-5-removing-a-legacy-in-house-audit-trigger).

## Reference microbenchmark (reproduce anywhere)

The tables below are a synthetic suite you can run on any machine —
single run here on an Apple M3 Pro, Docker Desktop, PostgreSQL
16-alpine, Python 3.13, psycopg 3.3. Values are **medians** (robust
against VM jitter). Cells marked **—** have no benchmark yet; rows
sourced from load tests are called out as such.

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
wide. (2) The absolute baselines (e.g. DELETE
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
| Audited, chain enabled, 4 writers   | — (load test) | —  |
| Audited, chain enabled, 16 writers  | — (load test) | —  |

² The chain-disabled baseline was the noisiest run in the whole suite
(stddev > median); the ~28 µs delta the advisory lock adds is the
takeaway, not the absolute. ops/sec is derived from the median.

The concurrency rows would come from load tests, not the
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
Python's cursor buffer (``batch_size`` rows in flight), independent of
result set size. The default (``stream=False``) path does
``fetchall()`` and so its peak grows ~linearly with the number of
surviving rows.

``benchmarks/test_stream_memory.py`` measures this: it populates
``auditlog`` with one INSERT event per ``object_id`` (all alive at the
target timestamp), then compares the peak Python allocation
(``tracemalloc``) of iterating the streaming generator row-by-row —
never building a list — against materialising the default path into a
list. It also records a secondary ``resource.getrusage().ru_maxrss``
delta for reference.

**How to run**

```bash
# default ROWS=50_000, runs in CI/dev time
uv run pytest benchmarks/test_stream_memory.py -s

# crank it up on real hardware (e.g. 5M surviving rows)
AUDITRUM_STREAM_BENCH_ROWS=5000000 uv run pytest benchmarks/test_stream_memory.py -s
```

The test asserts the streaming peak is a small fraction of the
materialised peak (a tolerant guard, not an exact number — it is a
benchmark). The streaming peak stays flat as ``ROWS`` grows; the
materialised peak scales with it.

Reference hardware (Apple Silicon, Postgres 16-alpine in Docker,
50k surviving rows, ``batch_size=1000``): ``tracemalloc`` peak
**1.4 MB streaming vs 38.6 MB materialised** (ratio ≈ 0.04);
``ru_maxrss`` delta **+3.7 MB vs +111.6 MB**. A run at production scale is still the next real number to land here.

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
  [``examples/grafana/``](https://github.com/tauvin/auditrum/tree/main/examples/grafana) renders those as the
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
* **Numbers are one workload.** A write-heavy, bursty workload and a
  read-heavy one will show different tracings. Don't assume the
  published numbers transfer linearly.
