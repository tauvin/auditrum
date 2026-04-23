# Benchmarks

Reproducible microbenchmarks for the three hot paths users actually
care about:

* **Trigger overhead** — the delta between an INSERT / UPDATE / DELETE
  on a tracked table vs an untracked one. Varied across field filters
  and log conditions.
* **Hash chain throughput** — the ceiling imposed by the advisory
  lock taken in the chain trigger.
* **Time-travel latency** — `reconstruct_row` / `reconstruct_field_history`
  against a row with a realistic history depth.

## Running

```bash
uv sync --extra benchmark
uv run pytest benchmarks/ --benchmark-only --benchmark-autosave
```

Docker must be running — the suite stands up a Postgres testcontainer
(same image used by `tests/integration/`).

Results land in `./.benchmarks/<machine>/<timestamp>_*.json`. Compare
against a saved baseline:

```bash
# Save current results as the baseline
uv run pytest benchmarks/ --benchmark-only --benchmark-save=baseline

# Later, compare a change against it
uv run pytest benchmarks/ --benchmark-only --benchmark-compare=baseline \
    --benchmark-compare-fail=mean:20%
```

`--benchmark-compare-fail=mean:20%` makes CI fail if mean time
regresses by more than 20% against the saved baseline. Use this to
gate performance-sensitive PRs without committing to absolute numbers.

## What the numbers mean

Reported timings are wall-clock per operation as measured by
`pytest-benchmark`. They include the Python ↔ Postgres round-trip —
which is why the "baseline" (untracked) benchmarks exist alongside
every tracked benchmark. The right number to cite in user-facing
docs is the **delta** between tracked and untracked, not the absolute
timing, since absolute numbers depend on the host (container
startup, PG `shared_buffers`, kernel scheduling, you name it).

## What's not covered here

* **Concurrent write throughput** — the chain's advisory lock caps
  concurrent insert rate. The ceiling has to be measured under real
  multi-writer load; a single-process benchmark can't see it.
  Catalog's production traffic is the planned data source.
* **Streaming memory footprint** — `reconstruct_table(stream=True)`
  is supposed to bound memory regardless of result set size.
  Measuring that needs `memray` or `tracemalloc` across a 10M-row
  audit log, not a per-op microbenchmark. Scheduled separately in
  `docs/performance.md`.
* **Real-workload numbers** — the ones users quote at standups —
  come from catalog pre-prod once those are available. The numbers
  in this repo are a baseline on a clean containerised Postgres,
  not a capacity planning tool.

## CI

These benchmarks are **not** run on every PR — they're too sensitive
to runner noise to be a useful gate. They run on demand via
`workflow_dispatch` and nightly against `main`. See
`.github/workflows/` if we end up wiring that up; for now they're
manual.
