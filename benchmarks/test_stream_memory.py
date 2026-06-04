"""Streaming memory footprint of ``reconstruct_table(stream=True)``.

The claim in ``docs/performance.md`` ("Streaming memory footprint") is
that the ``stream=True`` server-side named-cursor path keeps peak Python
memory bounded by the cursor's ``itersize`` batch, independent of the
result-set size — whereas the default path materialises the whole result
with ``fetchall()`` and so grows ~linearly with the row count.

This is a *memory* benchmark, not a latency one, so it does not use
``pytest-benchmark``. It populates ``auditlog`` with one INSERT event per
distinct ``object_id`` (so every object is alive at ``now`` and
``reconstruct_table`` returns ``ROWS`` rows), then compares peak Python
allocation of:

* iterating ``reconstruct_table(..., stream=True)`` row-by-row WITHOUT
  building a list, vs
* materialising the default (``fetchall()``-backed) path into a list.

``tracemalloc`` measures peak Python heap; ``resource.getrusage`` gives a
secondary RSS reading (noisier, reported for reference only).

Scale the row count with ``AUDITRUM_STREAM_BENCH_ROWS`` to push it into
the millions on real hardware; the default is modest so it runs in
CI/dev time.
"""

from __future__ import annotations

import json
import os
import resource
import tracemalloc
from datetime import UTC, datetime

import psycopg
import pytest

from auditrum.timetravel import reconstruct_table

# Distinct object_ids (= rows alive at ``now``) the benchmark inserts.
# Override with AUDITRUM_STREAM_BENCH_ROWS to crank it up on real hardware.
ROWS = int(os.environ.get("AUDITRUM_STREAM_BENCH_ROWS", "50000"))

# Server-side cursor batch. The streaming peak should track this, not ROWS.
STREAM_BATCH = 1000

# The streaming path keeps at most ~STREAM_BATCH rows in flight, so its
# peak should be a small fraction of the materialised path's peak. This is
# a tolerant guard (benchmark, not an exact assertion) — at 50k rows the
# real ratio is far below this; we only assert the stream path does not
# scale with ROWS the way fetchall() does.
MAX_STREAM_FRACTION = 0.5

_TABLE = "stream_bench"


def _ru_maxrss_bytes() -> int:
    """``ru_maxrss`` in bytes (Linux reports KiB, macOS reports bytes)."""
    raw = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return raw if os.uname().sysname == "Darwin" else raw * 1024


@pytest.fixture
def populated_stream_log(fresh_auditlog, pg_dsn):
    """Insert ``ROWS`` INSERT events, one per object_id, all alive at now.

    Writes straight into ``auditlog`` rather than driving a trigger: the
    benchmark cares about the read/streaming path, and a per-row trigger
    round-trip would dominate setup time at 50k+ rows. ``new_data`` is a
    small JSONB payload so reconstructed rows are non-trivial in size.

    Yields ``(read_conn, at, dsn)``. ``read_conn`` is a non-autocommit
    connection: the ``stream=True`` server-side named cursor issues
    ``DECLARE CURSOR``, which Postgres only allows inside a transaction
    block (the session ``pg_conn`` fixture is autocommit and cannot host
    one).
    """
    conn = fresh_auditlog
    now = datetime.now(UTC)

    rows = (
        (
            "INSERT",
            str(oid),
            _TABLE,
            json.dumps(
                {
                    "id": oid,
                    "status": "active",
                    "name": f"object-{oid}",
                    "payload": "x" * 64,
                }
            ),
        )
        for oid in range(ROWS)
    )

    with (
        conn.cursor() as cur,
        cur.copy("COPY auditlog (operation, object_id, table_name, new_data) FROM STDIN") as copy,
    ):
        for row in rows:
            copy.write_row(row)

    with psycopg.connect(pg_dsn) as read_conn:
        yield read_conn, now


@pytest.mark.benchmark(group="time-travel")
def test_stream_memory_bounded(populated_stream_log):
    conn, at = populated_stream_log

    # --- streaming path: consume row-by-row, never materialise ---
    tracemalloc.start()
    rss_before_stream = _ru_maxrss_bytes()
    count_stream = 0
    last = None
    for _object_id, row_data in reconstruct_table(
        conn, table=_TABLE, at=at, stream=True, batch_size=STREAM_BATCH
    ):
        count_stream += 1
        last = row_data  # touch the value so it can't be optimised away
    _stream_current, stream_peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    rss_after_stream = _ru_maxrss_bytes()
    assert last is not None

    # --- default path: materialise the full result into a list ---
    tracemalloc.start()
    rss_before_mat = _ru_maxrss_bytes()
    materialised = list(reconstruct_table(conn, table=_TABLE, at=at))
    _mat_current, mat_peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    rss_after_mat = _ru_maxrss_bytes()

    count_mat = len(materialised)
    del materialised

    # Both paths must agree on what they returned.
    assert count_stream == count_mat == ROWS

    print(
        f"\n[stream-memory] rows={ROWS} batch={STREAM_BATCH}\n"
        f"  tracemalloc peak  stream={stream_peak / 1e6:.1f} MB  "
        f"materialised={mat_peak / 1e6:.1f} MB  "
        f"ratio={stream_peak / mat_peak:.3f}\n"
        f"  ru_maxrss delta   stream=+{(rss_after_stream - rss_before_stream) / 1e6:.1f} MB  "
        f"materialised=+{(rss_after_mat - rss_before_mat) / 1e6:.1f} MB"
    )

    # Core claim: streaming peak is a small fraction of the materialised
    # peak, i.e. it is bounded by the cursor batch and does not scale with
    # ROWS the way fetchall() does.
    assert stream_peak < mat_peak * MAX_STREAM_FRACTION, (
        f"streaming peak ({stream_peak / 1e6:.1f} MB) not substantially below "
        f"materialised peak ({mat_peak / 1e6:.1f} MB)"
    )
