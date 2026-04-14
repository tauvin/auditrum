"""Integration tests for concurrent ``TriggerManager.sync()`` calls.

The concern from the security review: ``sync()`` reads
``installed_rows`` once at the start, then loops calling ``install()``
without a top-level lock. Two parallel sync calls could both decide a
trigger needs installing. The per-trigger advisory lock acquired
inside ``install()`` serialises the actual DDL, but we want to verify
the convergence guarantees hold under real concurrency:

1. Two ``sync([spec])`` calls running in parallel must converge to the
   same final tracking-table state.
2. The DDL must actually fire — the trigger function must exist in
   ``pg_proc`` after both calls finish.
3. Parallel ``sync(prune=False)`` against different spec sets must
   leave both sets installed (additive merge).

These tests open multiple psycopg connections from the same
testcontainer to exercise true parallelism, not just `ContextVar`
isolation.
"""

from concurrent.futures import ThreadPoolExecutor, as_completed

import psycopg
import pytest

from auditrum.executor import PsycopgExecutor
from auditrum.schema import (
    generate_audit_attach_context_sql,
    generate_audit_context_table_sql,
    generate_audit_current_user_id_sql,
    generate_auditlog_partitions_sql,
    generate_auditlog_table_sql,
    generate_jsonb_diff_function_sql,
)
from auditrum.tracking import FieldFilter, TrackSpec, TriggerManager


@pytest.fixture
def parallel_audit_setup(pg_dsn, pg_conn):
    """Set up the audit schema once, then yield the DSN so each test can
    open its own connection pool. ``pg_conn`` is reused for setup/teardown."""
    with pg_conn.cursor() as cur:
        cur.execute("DROP TABLE IF EXISTS auditlog CASCADE")
        cur.execute("DROP TABLE IF EXISTS audit_context CASCADE")
        cur.execute("DROP TABLE IF EXISTS conc_widgets CASCADE")
        cur.execute("DROP TABLE IF EXISTS conc_orders CASCADE")
        cur.execute("DROP TABLE IF EXISTS auditrum_applied_triggers CASCADE")
        cur.execute("DROP FUNCTION IF EXISTS jsonb_diff(jsonb, jsonb) CASCADE")
        cur.execute("DROP FUNCTION IF EXISTS _audit_attach_context() CASCADE")
        cur.execute("DROP FUNCTION IF EXISTS _audit_current_user_id() CASCADE")
        cur.execute(generate_audit_context_table_sql("audit_context"))
        cur.execute(generate_auditlog_table_sql("auditlog"))
        cur.execute(generate_jsonb_diff_function_sql())
        cur.execute(generate_audit_attach_context_sql("audit_context"))
        cur.execute(generate_audit_current_user_id_sql())
        cur.execute(generate_auditlog_partitions_sql("auditlog", months_ahead=1))
        cur.execute(
            "CREATE TABLE conc_widgets (id serial PRIMARY KEY, name text)"
        )
        cur.execute(
            "CREATE TABLE conc_orders (id serial PRIMARY KEY, status text)"
        )
    yield pg_dsn
    with pg_conn.cursor() as cur:
        cur.execute("DROP TABLE IF EXISTS auditlog CASCADE")
        cur.execute("DROP TABLE IF EXISTS audit_context CASCADE")
        cur.execute("DROP TABLE IF EXISTS conc_widgets CASCADE")
        cur.execute("DROP TABLE IF EXISTS conc_orders CASCADE")
        cur.execute("DROP TABLE IF EXISTS auditrum_applied_triggers CASCADE")
        cur.execute("DROP FUNCTION IF EXISTS jsonb_diff(jsonb, jsonb) CASCADE")
        cur.execute("DROP FUNCTION IF EXISTS _audit_attach_context() CASCADE")
        cur.execute("DROP FUNCTION IF EXISTS _audit_current_user_id() CASCADE")


def _sync_in_own_connection(dsn: str, spec: TrackSpec) -> dict:
    """Helper that opens a fresh connection, runs sync, returns the report."""
    with psycopg.connect(dsn, autocommit=False) as conn:
        mgr = TriggerManager(PsycopgExecutor(conn))
        mgr.bootstrap()
        report = mgr.sync([spec])
        conn.commit()
        return {
            "installed": list(report.installed),
            "updated": list(report.updated),
            "skipped": list(report.skipped),
            "uninstalled": list(report.uninstalled),
        }


class TestParallelSync:
    def test_two_parallel_syncs_same_spec_converge(self, parallel_audit_setup):
        """Two parallel sync calls on the same spec must converge: only
        one tracking row exists at the end and the trigger function is
        actually installed in pg_proc."""
        dsn = parallel_audit_setup
        spec = TrackSpec(
            table="conc_widgets",
            fields=FieldFilter.only("name"),
        )

        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [
                pool.submit(_sync_in_own_connection, dsn, spec),
                pool.submit(_sync_in_own_connection, dsn, spec),
            ]
            reports = [f.result(timeout=30) for f in as_completed(futures)]

        # Combined: at least one of the two reports installed it; the
        # other either also tried (race) or skipped (drift detection).
        all_installed = [r["installed"] for r in reports]
        assert any("audit_conc_widgets_trigger" in inst for inst in all_installed)

        # Verify: tracking table has exactly one row for this trigger
        with psycopg.connect(dsn) as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT COUNT(*) FROM auditrum_applied_triggers "
                "WHERE trigger_name = %s",
                ("audit_conc_widgets_trigger",),
            )
            assert cur.fetchone()[0] == 1

            # Verify: function actually exists in pg_proc
            cur.execute(
                "SELECT COUNT(*) FROM pg_proc WHERE proname = %s",
                ("audit_conc_widgets_trigger",),
            )
            assert cur.fetchone()[0] == 1

            # Verify: trigger actually fires when we touch the tracked table
            cur.execute("INSERT INTO conc_widgets (name) VALUES ('parallel-test')")
            cur.execute(
                "SELECT COUNT(*) FROM auditlog WHERE table_name = 'conc_widgets'"
            )
            assert cur.fetchone()[0] == 1

    def test_parallel_syncs_different_specs_both_install(
        self, parallel_audit_setup
    ):
        """Two parallel syncs with disjoint spec lists must both succeed
        and produce two tracking rows. The advisory locks are per-trigger
        so different triggers don't block each other."""
        dsn = parallel_audit_setup
        spec_widgets = TrackSpec(table="conc_widgets")
        spec_orders = TrackSpec(table="conc_orders")

        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [
                pool.submit(_sync_in_own_connection, dsn, spec_widgets),
                pool.submit(_sync_in_own_connection, dsn, spec_orders),
            ]
            reports = [f.result(timeout=30) for f in as_completed(futures)]

        all_installed = sorted(
            name for r in reports for name in r["installed"]
        )
        assert all_installed == [
            "audit_conc_orders_trigger",
            "audit_conc_widgets_trigger",
        ]

        with psycopg.connect(dsn) as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT trigger_name FROM auditrum_applied_triggers ORDER BY trigger_name"
            )
            tracked = [row[0] for row in cur.fetchall()]
        assert tracked == [
            "audit_conc_orders_trigger",
            "audit_conc_widgets_trigger",
        ]

    def test_repeated_sync_is_idempotent_under_load(self, parallel_audit_setup):
        """N parallel sync calls of the same spec converge to one tracking
        row regardless of N. Stronger version of the first test."""
        dsn = parallel_audit_setup
        spec = TrackSpec(table="conc_widgets")

        with ThreadPoolExecutor(max_workers=8) as pool:
            futures = [
                pool.submit(_sync_in_own_connection, dsn, spec) for _ in range(8)
            ]
            for f in as_completed(futures):
                f.result(timeout=30)

        with psycopg.connect(dsn) as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT COUNT(*) FROM auditrum_applied_triggers "
                "WHERE trigger_name = %s",
                ("audit_conc_widgets_trigger",),
            )
            assert cur.fetchone()[0] == 1
