"""Row size and reconstruct latency: full vs diff storage.

A synthetic "hot" table with a dozen columns, where each UPDATE touches
one or two of them — the workload diff storage targets. Prints the
average audit row size per operation for docs/performance-tuning.md.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from auditrum.executor import PsycopgExecutor
from auditrum.timetravel import reconstruct_row
from auditrum.tracking import TrackSpec, TriggerManager

COLUMNS = ", ".join(f"c{i} text" for i in range(10))


def _populate(conn, storage: str, rows: int = 200, updates: int = 20) -> None:
    with conn.cursor() as cur:
        cur.execute("DROP TABLE IF EXISTS hot CASCADE")
        cur.execute(f"CREATE TABLE hot (id serial PRIMARY KEY, status text, n int, {COLUMNS})")
        mgr = TriggerManager(PsycopgExecutor(conn))
        mgr.bootstrap()
        mgr.install(TrackSpec(table="hot", storage=storage), force=True)
        values = ", ".join(["%s"] * 10)
        for r in range(rows):
            cur.execute(
                f"INSERT INTO hot (status, n, {', '.join(f'c{i}' for i in range(10))}) "
                f"VALUES ('new', 0, {values})",
                [f"value-{r}-{i}-" + "x" * 40 for i in range(10)],
            )
        for u in range(updates):
            cur.execute("UPDATE hot SET n = %s, status = %s", (u, f"s{u % 3}"))


def _avg_sizes(conn) -> dict[str, int]:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT operation, avg(pg_column_size(a.*))::int FROM auditlog a GROUP BY operation"
        )
        return dict(cur.fetchall())


@pytest.mark.parametrize("storage", ["full", "diff"])
def test_row_size(fresh_auditlog, storage):
    _populate(fresh_auditlog, storage)
    sizes = _avg_sizes(fresh_auditlog)
    print(f"\n{storage}: avg audit row bytes by operation: {sizes}")
    assert sizes["UPDATE"] > 0


@pytest.mark.benchmark(group="storage-reconstruct")
@pytest.mark.parametrize("storage", ["full", "diff"])
def test_reconstruct_row_latency(benchmark, fresh_auditlog, storage):
    _populate(fresh_auditlog, storage, rows=10, updates=100)
    at = datetime.now(UTC)

    def run():
        reconstruct_row(fresh_auditlog, table="hot", object_id="1", at=at)

    benchmark(run)
