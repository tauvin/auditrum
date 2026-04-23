"""Hash chain throughput ceiling.

The chain takes a per-table advisory lock in its BEFORE INSERT
trigger to guarantee monotonic ``chain_seq``. That lock serialises
chain writes for the table — a real ceiling on insert throughput
under heavy concurrency.

The benchmark measures the uncontended case (single writer) to set
the baseline, then the concurrency section would land once we run
these against catalog's real load to get the ceiling number.
"""

from __future__ import annotations

import pytest

from auditrum.hash_chain import generate_hash_chain_sql
from auditrum.triggers import generate_trigger_sql


@pytest.fixture
def chain_table(fresh_auditlog):
    conn = fresh_auditlog
    with conn.cursor() as cur:
        cur.execute("DROP TABLE IF EXISTS chain_bench CASCADE")
        cur.execute(
            "CREATE TABLE chain_bench ("
            "id serial PRIMARY KEY, "
            "payload text NOT NULL"
            ")"
        )
        cur.execute(generate_trigger_sql("chain_bench"))
        # Hash chain installs a BEFORE INSERT trigger on auditlog
        # itself — one chain per audit_table, not per tracked table.
        cur.execute(generate_hash_chain_sql())
    yield conn


@pytest.mark.benchmark(group="hash-chain")
def test_insert_without_chain(benchmark, fresh_auditlog):
    """Baseline: insert into an audited table *without* the chain
    trigger installed. Comparing against ``test_insert_with_chain``
    isolates the chain's cost."""
    conn = fresh_auditlog
    with conn.cursor() as cur:
        cur.execute("DROP TABLE IF EXISTS chain_baseline CASCADE")
        cur.execute(
            "CREATE TABLE chain_baseline (id serial PRIMARY KEY, payload text)"
        )
        cur.execute(generate_trigger_sql("chain_baseline"))

    def run():
        with conn.cursor() as cur:
            cur.execute("INSERT INTO chain_baseline (payload) VALUES ('x')")

    benchmark(run)


@pytest.mark.benchmark(group="hash-chain")
def test_insert_with_chain(benchmark, chain_table):
    conn = chain_table

    def run():
        with conn.cursor() as cur:
            cur.execute("INSERT INTO chain_bench (payload) VALUES ('x')")

    benchmark(run)
