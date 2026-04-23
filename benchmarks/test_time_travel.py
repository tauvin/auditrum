"""Time-travel reconstruct latency.

Three scales matter:

* **Row-level** — ``reconstruct_row(table, id, at)`` on a row with
  N historical events. Scales linearly in history depth; the
  composite ``(table_name, object_id, changed_at DESC)`` index is
  what keeps it from blowing up.
* **Table-level streaming** — ``reconstruct_table(table, at,
  stream=True)`` memory footprint on a large audit log. The
  ``stream=True`` path uses a server-side named cursor; the
  non-streaming path does ``fetchall()`` on the whole result set.
* **Field history** — ``reconstruct_field_history(table, id, 'col')``
  — touches the diff jsonb column.

The numbers these produce fill out the "Time travel" row in
``docs/performance.md``.
"""

from __future__ import annotations

import pytest

from auditrum.timetravel import reconstruct_field_history, reconstruct_row
from auditrum.triggers import generate_trigger_sql


@pytest.fixture
def populated_history(fresh_auditlog):
    """Insert a row then UPDATE it ``N`` times to build a deep history.

    ``N = 100`` is enough to see the index working without making the
    benchmark take forever. Real catalog rows see far more churn than
    this; we'll bump this in docs once we see real numbers.
    """
    conn = fresh_auditlog
    with conn.cursor() as cur:
        cur.execute("DROP TABLE IF EXISTS tt_bench CASCADE")
        cur.execute(
            "CREATE TABLE tt_bench (id serial PRIMARY KEY, status text, count integer)"
        )
        cur.execute(generate_trigger_sql("tt_bench"))
        cur.execute(
            "INSERT INTO tt_bench (status, count) VALUES ('new', 0) RETURNING id"
        )
        (row_id,) = cur.fetchone()
        for i in range(1, 101):
            cur.execute(
                "UPDATE tt_bench SET count = %s WHERE id = %s", (i, row_id)
            )
    return conn, row_id


@pytest.mark.benchmark(group="time-travel")
def test_reconstruct_row_latest(benchmark, populated_history):
    from datetime import UTC, datetime

    conn, row_id = populated_history
    now = datetime.now(UTC)

    def run():
        reconstruct_row(conn, table="tt_bench", object_id=str(row_id), at=now)

    benchmark(run)


@pytest.mark.benchmark(group="time-travel")
def test_reconstruct_field_history(benchmark, populated_history):
    conn, row_id = populated_history

    def run():
        reconstruct_field_history(
            conn, table="tt_bench", object_id=str(row_id), field="count"
        )

    benchmark(run)
