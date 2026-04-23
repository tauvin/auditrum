"""Trigger overhead — the headline number for the README.

Compares the cost of a bare ``INSERT`` / ``UPDATE`` / ``DELETE`` on
a tracked table against the same statement on an untracked table.
The delta is what users pay for auditrum on the write path.

Three axes we care about:

1. **Operation** — INSERT / UPDATE / DELETE overhead.
2. **Field filter** — ``FieldFilter.all()`` vs ``FieldFilter.only(…)``.
   ``only`` is what most bidwise / catalog tables actually use; it
   shrinks ``old_filtered`` / ``new_filtered`` so the diff step is
   cheaper.
3. **log_condition** — short-circuit gate for "don't log drafts"
   patterns. Supposed to be near-zero cost when it short-circuits.

Real numbers land in ``docs/performance.md`` once we run these
against catalog's workload. What ships in the repo is the methodology
— users can re-run on their own hardware and compare.
"""

from __future__ import annotations

import pytest

from auditrum.triggers import generate_trigger_sql


@pytest.fixture
def bench_tables(fresh_auditlog):
    """Two tables with identical schemas — one with a trigger, one without.

    The untracked table is the baseline; timings are reported relative
    to it so the reader can see the marginal cost of the trigger and
    not the cost of PG INSERT itself.
    """
    conn = fresh_auditlog
    with conn.cursor() as cur:
        cur.execute("DROP TABLE IF EXISTS bench_tracked CASCADE")
        cur.execute("DROP TABLE IF EXISTS bench_untracked CASCADE")
        cur.execute(
            "CREATE TABLE bench_tracked ("
            "id serial PRIMARY KEY, "
            "status text NOT NULL, "
            "total numeric(10, 2), "
            "tenant_id integer NOT NULL, "
            "notes text"
            ")"
        )
        cur.execute(
            "CREATE TABLE bench_untracked ("
            "id serial PRIMARY KEY, "
            "status text NOT NULL, "
            "total numeric(10, 2), "
            "tenant_id integer NOT NULL, "
            "notes text"
            ")"
        )
        cur.execute(generate_trigger_sql("bench_tracked"))
    yield conn


@pytest.mark.benchmark(group="insert")
def test_insert_untracked(benchmark, bench_tables):
    conn = bench_tables

    def run():
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO bench_untracked (status, total, tenant_id) "
                "VALUES ('new', 100.00, 1)"
            )

    benchmark(run)


@pytest.mark.benchmark(group="insert")
def test_insert_tracked_all_fields(benchmark, bench_tables):
    conn = bench_tables

    def run():
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO bench_tracked (status, total, tenant_id) "
                "VALUES ('new', 100.00, 1)"
            )

    benchmark(run)


@pytest.mark.benchmark(group="insert")
def test_insert_tracked_only_two_fields(benchmark, fresh_auditlog):
    """``FieldFilter.only('status', 'total')`` — the common case.

    Compared to ``FieldFilter.all()`` this skips the ignored columns
    in ``ignored_keys`` and produces a smaller ``diff`` jsonb. The
    benchmark measures whether the smaller filter actually shows up
    as reduced trigger time.
    """
    conn = fresh_auditlog
    with conn.cursor() as cur:
        cur.execute("DROP TABLE IF EXISTS bench_narrow CASCADE")
        cur.execute(
            "CREATE TABLE bench_narrow ("
            "id serial PRIMARY KEY, "
            "status text NOT NULL, "
            "total numeric(10, 2), "
            "tenant_id integer NOT NULL, "
            "notes text"
            ")"
        )
        cur.execute(
            generate_trigger_sql("bench_narrow", track_only=["status", "total"])
        )

    def run():
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO bench_narrow (status, total, tenant_id) "
                "VALUES ('new', 100.00, 1)"
            )

    benchmark(run)


@pytest.mark.benchmark(group="update")
def test_update_untracked(benchmark, bench_tables):
    conn = bench_tables
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO bench_untracked (status, total, tenant_id) "
            "VALUES ('new', 100, 1) RETURNING id"
        )
        row_id = cur.fetchone()[0]

    def run():
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE bench_untracked SET status = 'paid' WHERE id = %s",
                (row_id,),
            )

    benchmark(run)


@pytest.mark.benchmark(group="update")
def test_update_tracked_all_fields(benchmark, bench_tables):
    conn = bench_tables
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO bench_tracked (status, total, tenant_id) "
            "VALUES ('new', 100, 1) RETURNING id"
        )
        row_id = cur.fetchone()[0]

    def run():
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE bench_tracked SET status = 'paid' WHERE id = %s",
                (row_id,),
            )

    benchmark(run)


@pytest.mark.benchmark(group="update")
def test_update_tracked_with_log_condition_short_circuit(benchmark, fresh_auditlog):
    """``log_condition`` that always evaluates false → trigger fires,
    short-circuits immediately, does not write to ``auditlog``.

    Worst-case check for the "don't log draft rows" pattern: we want
    the short-circuit to be cheaper than a full diff computation.
    """
    conn = fresh_auditlog
    with conn.cursor() as cur:
        cur.execute("DROP TABLE IF EXISTS bench_gated CASCADE")
        cur.execute(
            "CREATE TABLE bench_gated ("
            "id serial PRIMARY KEY, "
            "status text NOT NULL, "
            "is_logged boolean NOT NULL DEFAULT false"
            ")"
        )
        cur.execute(
            generate_trigger_sql(
                "bench_gated",
                log_conditions="NEW.is_logged = TRUE",
            )
        )
        cur.execute(
            "INSERT INTO bench_gated (status, is_logged) VALUES ('new', false) "
            "RETURNING id"
        )
        row_id = cur.fetchone()[0]

    def run():
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE bench_gated SET status = 'paid' WHERE id = %s", (row_id,)
            )

    benchmark(run)


@pytest.mark.benchmark(group="delete")
def test_delete_tracked(benchmark, bench_tables):
    conn = bench_tables

    def setup_and_delete():
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO bench_tracked (status, total, tenant_id) "
                "VALUES ('new', 1, 1) RETURNING id"
            )
            row_id = cur.fetchone()[0]
            cur.execute("DELETE FROM bench_tracked WHERE id = %s", (row_id,))

    # pedantic=False is the default — the INSERT is part of the
    # measurement here because isolating DELETE requires re-seeding
    # each iteration, and the extra round-trip dominates the measured
    # delete cost. Comparing against ``test_delete_untracked`` below
    # cancels out the insert delta.
    benchmark(setup_and_delete)


@pytest.mark.benchmark(group="delete")
def test_delete_untracked(benchmark, bench_tables):
    conn = bench_tables

    def setup_and_delete():
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO bench_untracked (status, total, tenant_id) "
                "VALUES ('new', 1, 1) RETURNING id"
            )
            row_id = cur.fetchone()[0]
            cur.execute("DELETE FROM bench_untracked WHERE id = %s", (row_id,))

    benchmark(setup_and_delete)
