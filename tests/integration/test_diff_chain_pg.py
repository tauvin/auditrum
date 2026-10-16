"""Integration: verify_diff_chain against real Postgres, bypassing the trigger."""

import random

import pytest
from psycopg import sql

from auditrum.diff_chain import list_registered_tables, verify_diff_chain
from auditrum.executor import PsycopgExecutor
from auditrum.tracking import FieldFilter, TrackSpec, TriggerManager


def install(conn, spec):
    mgr = TriggerManager(PsycopgExecutor(conn))
    mgr.bootstrap()
    mgr.install(spec, force=True)


def now(conn):
    with conn.cursor() as cur:
        cur.execute("SELECT clock_timestamp()")
        return cur.fetchone()[0]


def bypass(conn, statement, params=()):
    """Run a write with row triggers disabled for this session."""
    with conn.cursor() as cur:
        cur.execute("SET session_replication_role = replica")
        try:
            cur.execute(statement, params)
        finally:
            cur.execute("SET session_replication_role = origin")


def kinds(findings):
    return {(f.object_id, f.kind, f.column) for f in findings}


@pytest.fixture
def items(fresh_auditlog):
    conn = fresh_auditlog
    with conn.cursor() as cur:
        cur.execute("DROP TABLE IF EXISTS vc_items CASCADE")
        cur.execute("CREATE TABLE vc_items (id int PRIMARY KEY, name text, secret text, n int)")
    yield conn
    with conn.cursor() as cur:
        cur.execute("DROP TABLE IF EXISTS vc_items CASCADE")


def tracked(conn, storage="diff"):
    install(
        conn, TrackSpec(table="vc_items", fields=FieldFilter.exclude("secret"), storage=storage)
    )


def seed(conn):
    with conn.cursor() as cur:
        for i in (1, 2, 3):
            cur.execute("INSERT INTO vc_items VALUES (%s, 'a', 's', 0)", (i,))
        cur.execute("UPDATE vc_items SET name = 'b' WHERE id IN (1, 2)")


@pytest.mark.parametrize("storage", ["full", "diff"])
def test_clean_history_has_no_findings(items, storage):
    tracked(items, storage)
    seed(items)
    assert verify_diff_chain(items, table="vc_items", full=True) == []


def test_null_transitions_are_clean(items):
    tracked(items)
    with items.cursor() as cur:
        cur.execute("INSERT INTO vc_items VALUES (1, NULL, 's', NULL)")
        cur.execute("UPDATE vc_items SET name = 'x', n = 1 WHERE id = 1")
        cur.execute("UPDATE vc_items SET name = NULL WHERE id = 1")
    assert verify_diff_chain(items, table="vc_items", full=True) == []


def test_delete_then_reinsert_is_clean(items):
    tracked(items)
    with items.cursor() as cur:
        cur.execute("INSERT INTO vc_items VALUES (1, 'a', 's', 0)")
        cur.execute("DELETE FROM vc_items WHERE id = 1")
        cur.execute("INSERT INTO vc_items VALUES (1, 'z', 's', 9)")
    assert verify_diff_chain(items, table="vc_items", full=True) == []


def test_bypassing_update_is_a_live_finding(items):
    tracked(items)
    seed(items)
    bypass(items, "UPDATE vc_items SET name = 'X' WHERE id = 1")
    [finding] = verify_diff_chain(items, table="vc_items", full=True)
    assert (finding.object_id, finding.kind, finding.column) == ("1", "live", "name")
    assert (finding.expected, finding.actual) == ("b", "X")


def test_bypass_then_audited_update_is_a_chain_finding(items):
    tracked(items)
    seed(items)
    bypass(items, "UPDATE vc_items SET name = 'X' WHERE id = 1")
    with items.cursor() as cur:
        cur.execute("UPDATE vc_items SET name = 'c' WHERE id = 1")
    [finding] = verify_diff_chain(items, table="vc_items", full=True)
    assert (finding.object_id, finding.kind, finding.column) == ("1", "chain", "name")
    assert (finding.expected, finding.actual) == ("b", "X")


def test_bypassing_delete_is_a_lifecycle_finding(items):
    tracked(items)
    seed(items)
    bypass(items, "DELETE FROM vc_items WHERE id = 2")
    assert kinds(verify_diff_chain(items, table="vc_items", full=True)) == {
        ("2", "lifecycle", None)
    }


def test_bypassing_insert_after_delete_is_a_lifecycle_finding(items):
    tracked(items)
    seed(items)
    with items.cursor() as cur:
        cur.execute("DELETE FROM vc_items WHERE id = 3")
    bypass(items, "INSERT INTO vc_items VALUES (3, 'r', 's', 0)")
    [finding] = verify_diff_chain(items, table="vc_items", full=True)
    assert (finding.object_id, finding.kind) == ("3", "lifecycle")
    assert "DELETE" in finding.detail


def test_bypassing_delete_then_audited_insert_reads_as_lifecycle(items):
    tracked(items)
    seed(items)
    bypass(items, "DELETE FROM vc_items WHERE id = 1")
    with items.cursor() as cur:
        cur.execute("INSERT INTO vc_items VALUES (1, 'r', 's', 0)")
    [finding] = verify_diff_chain(items, table="vc_items", full=True)
    assert (finding.object_id, finding.kind) == ("1", "lifecycle")
    assert finding.detail == "INSERT for a row that already existed"


def test_excluded_column_bypass_is_ignored(items):
    tracked(items)
    seed(items)
    bypass(items, "UPDATE vc_items SET secret = 'zz'")
    assert verify_diff_chain(items, table="vc_items", full=True) == []


def test_full_mode_pre_existing_row_bypass_is_a_live_finding(items):
    with items.cursor() as cur:
        cur.execute("INSERT INTO vc_items VALUES (1, 'a', 's', 0)")
    tracked(items, "full")
    with items.cursor() as cur:
        cur.execute("UPDATE vc_items SET n = 5 WHERE id = 1")
    bypass(items, "UPDATE vc_items SET name = 'zz' WHERE id = 1")
    [finding] = verify_diff_chain(items, table="vc_items", full=True)
    assert (finding.object_id, finding.kind, finding.column) == ("1", "live", "name")
    assert (finding.expected, finding.actual) == ("a", "zz")


def test_full_mode_bypass_between_updates_is_a_chain_finding(items):
    with items.cursor() as cur:
        cur.execute("INSERT INTO vc_items VALUES (1, 'a', 's', 0)")
    tracked(items, "full")
    with items.cursor() as cur:
        cur.execute("UPDATE vc_items SET n = 5 WHERE id = 1")
    bypass(items, "UPDATE vc_items SET name = 'zz' WHERE id = 1")
    with items.cursor() as cur:
        cur.execute("UPDATE vc_items SET n = 6 WHERE id = 1")
        cur.execute("SELECT max(id) FROM auditlog")
        second = cur.fetchone()[0]
    [finding] = verify_diff_chain(items, table="vc_items", full=True)
    assert (finding.object_id, finding.kind, finding.column) == ("1", "chain", "name")
    assert finding.audit_id == second
    assert (finding.expected, finding.actual) == ("a", "zz")


def test_full_mode_history_with_pre_existing_rows_is_clean(items):
    with items.cursor() as cur:
        cur.execute("INSERT INTO vc_items VALUES (1, 'a', 's', 0), (2, 'b', 's', 0)")
    tracked(items, "full")
    with items.cursor() as cur:
        cur.execute("UPDATE vc_items SET n = 5 WHERE id = 1")
        cur.execute("UPDATE vc_items SET name = 'c' WHERE id = 1")
        cur.execute("UPDATE vc_items SET n = 1 WHERE id = 2")
        cur.execute("DELETE FROM vc_items WHERE id = 2")
        cur.execute("INSERT INTO vc_items VALUES (2, 'z', 's', 3)")
    assert verify_diff_chain(items, table="vc_items", full=True) == []


def test_pre_062_snapshots_with_excluded_columns_are_clean(items):
    """Old snapshots hold the excluded column; a save that changed only it wrote no row."""
    tracked(items, "full")
    with items.cursor() as cur:
        cur.execute("INSERT INTO vc_items VALUES (1, 'a', 's2', 0)")
        cur.execute("DELETE FROM auditlog")
        cur.execute(
            "INSERT INTO auditlog (operation, changed_at, object_id, table_name, new_data, diff) "
            "VALUES ('INSERT', now(), '1', 'vc_items', "
            """'{"id": 1, "name": "a", "secret": "s", "n": 0}', """
            """'{"id": {"old": null, "new": 1}, "name": {"old": null, "new": "a"}, """
            """"n": {"old": null, "new": 0}}'::jsonb)"""
        )
        cur.execute("UPDATE vc_items SET name = 'b' WHERE id = 1")
        cur.execute(
            "UPDATE auditlog SET old_data = %s::jsonb, new_data = %s::jsonb "
            "WHERE operation = 'UPDATE' AND table_name = 'vc_items'",
            (
                '{"id": 1, "name": "a", "secret": "s2", "n": 0}',
                '{"id": 1, "name": "b", "secret": "s2", "n": 0}',
            ),
        )
    assert verify_diff_chain(items, table="vc_items", full=True) == []


def legacy_row(conn, object_id, diff):
    """Insert a pre-0.4 style UPDATE row whose diff holds only the new values."""
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO auditlog (operation, changed_at, object_id, table_name, diff) "
            "VALUES ('UPDATE', now(), %s, 'vc_items', %s::jsonb)",
            (str(object_id), diff),
        )


def test_values_only_rows_mid_history_feed_the_chain(items):
    tracked(items)
    seed(items)
    # Object 1: the legacy row sits between two paired events.
    bypass(items, "UPDATE vc_items SET name = 'L' WHERE id = 1")
    legacy_row(items, 1, '{"name": "L"}')
    with items.cursor() as cur:
        cur.execute("UPDATE vc_items SET name = 'c' WHERE id = 1")
    # Object 2: the legacy row is the last event touching name.
    bypass(items, "UPDATE vc_items SET name = 'M' WHERE id = 2")
    legacy_row(items, 2, '{"name": "M"}')
    assert verify_diff_chain(items, table="vc_items", full=True) == []


def test_jsonb_object_value_is_not_mistaken_for_a_paired_entry(items):
    with items.cursor() as cur:
        cur.execute("ALTER TABLE vc_items ADD COLUMN meta jsonb")
    tracked(items)
    with items.cursor() as cur:
        cur.execute("""INSERT INTO vc_items VALUES (1, 'a', 's', 0, '{"a": 1}')""")
        cur.execute("""UPDATE vc_items SET meta = '{"b": 2}' WHERE id = 1""")
    bypass(items, """UPDATE vc_items SET meta = '{"c": 3}' WHERE id = 1""")
    legacy_row(items, 1, '{"meta": {"c": 3}}')
    with items.cursor() as cur:
        cur.execute("""UPDATE vc_items SET meta = '{"d": 4}' WHERE id = 1""")
    assert verify_diff_chain(items, table="vc_items", full=True) == []


def test_object_id_scope(items):
    tracked(items)
    seed(items)
    bypass(items, "UPDATE vc_items SET name = 'X' WHERE id IN (1, 2)")
    assert kinds(verify_diff_chain(items, table="vc_items", object_id=2, full=True)) == {
        ("2", "live", "name")
    }


def test_window_skips_old_breaks_but_reports_live_state(items):
    tracked(items)
    seed(items)
    bypass(items, "UPDATE vc_items SET name = 'X' WHERE id = 1")
    with items.cursor() as cur:
        cur.execute("UPDATE vc_items SET name = 'c' WHERE id = 1")  # chain break on 1
    window = now(items)
    with items.cursor() as cur:
        cur.execute("UPDATE vc_items SET n = 5 WHERE id = 1")  # touches 1 inside the window
        cur.execute("UPDATE vc_items SET n = 5 WHERE id = 2")
    bypass(items, "UPDATE vc_items SET name = 'Y' WHERE id = 2")  # live break on 2
    assert kinds(verify_diff_chain(items, table="vc_items", since=window)) == {
        ("2", "live", "name")
    }
    assert ("1", "chain", "name") in kinds(verify_diff_chain(items, table="vc_items", full=True))


def test_limit_caps_findings(items):
    tracked(items)
    seed(items)
    bypass(items, "UPDATE vc_items SET name = 'X'")
    assert len(verify_diff_chain(items, table="vc_items", full=True)) == 3
    assert len(verify_diff_chain(items, table="vc_items", full=True, limit=2)) == 2


def test_table_missing_from_registry_raises(items):
    tracked(items)
    with items.cursor() as cur:
        cur.execute("DELETE FROM auditrum_applied_triggers")
    with pytest.raises(ValueError, match="vc_items.*auditrum_applied_triggers"):
        verify_diff_chain(items, table="vc_items", full=True)


def test_list_registered_tables(items):
    tracked(items)
    assert list_registered_tables(items) == ["vc_items"]


def test_list_registered_tables_skips_log_condition_tables(items):
    tracked(items)
    with items.cursor() as cur:
        cur.execute("DROP TABLE IF EXISTS vc_cond CASCADE")
        cur.execute("CREATE TABLE vc_cond (id int PRIMARY KEY, n int)")
    try:
        install(items, TrackSpec(table="vc_cond", storage="full", log_condition="NEW.n > 0"))
        assert list_registered_tables(items) == ["vc_items"]
    finally:
        with items.cursor() as cur:
            cur.execute("DROP TABLE IF EXISTS vc_cond CASCADE")


@pytest.mark.parametrize("seed_value", range(5))
def test_random_history_is_clean_until_one_bypass(items, seed_value):
    rnd = random.Random(seed_value)
    tracked(items)
    with items.cursor() as cur:
        for step in range(40):
            object_id = rnd.choice([1, 2, 3, 4])
            cur.execute("SELECT 1 FROM vc_items WHERE id = %s", (object_id,))
            exists = cur.fetchone() is not None
            roll = rnd.random()
            if not exists:
                cur.execute(
                    "INSERT INTO vc_items VALUES (%s, %s, 's', %s)", (object_id, f"n{step}", step)
                )
            elif roll < 0.15:
                cur.execute("DELETE FROM vc_items WHERE id = %s", (object_id,))
            else:
                cur.execute(
                    "UPDATE vc_items SET name = %s, n = %s WHERE id = %s",
                    (f"v{step}", step, object_id),
                )
    assert verify_diff_chain(items, table="vc_items", full=True) == []

    with items.cursor() as cur:
        cur.execute("SELECT id FROM vc_items ORDER BY id")
        alive = [r[0] for r in cur.fetchall()]
    if not alive:
        pytest.skip("random history deleted every row")
    victim = rnd.choice(alive)
    bypass(items, "UPDATE vc_items SET n = -1 WHERE id = %s", (victim,))
    assert str(victim) in {
        f.object_id for f in verify_diff_chain(items, table="vc_items", full=True)
    }


def set_timezone(conn, zone):
    with conn.cursor() as cur:
        cur.execute(sql.SQL("SET TimeZone = {}").format(sql.Literal(zone)))


def test_timestamptz_written_and_checked_in_different_zones_is_clean(items):
    with items.cursor() as cur:
        cur.execute("ALTER TABLE vc_items ADD COLUMN at timestamptz")
    tracked(items)
    with items.cursor() as cur:
        cur.execute("SHOW TimeZone")
        original = cur.fetchone()[0]
    try:
        set_timezone(items, "UTC")
        with items.cursor() as cur:
            cur.execute("INSERT INTO vc_items VALUES (1, 'a', 's', 0, '2026-10-01 12:00+00')")
            cur.execute("UPDATE vc_items SET at = '2026-10-02 12:00+00' WHERE id = 1")
        set_timezone(items, "Asia/Tokyo")
        with items.cursor() as cur:
            cur.execute("UPDATE vc_items SET at = '2026-10-03 12:00+00' WHERE id = 1")
        set_timezone(items, "Europe/Berlin")
        assert verify_diff_chain(items, table="vc_items", full=True) == []
    finally:
        set_timezone(items, original)


def test_timestamptz_with_precision_and_domain_compare_by_value(items):
    with items.cursor() as cur:
        cur.execute("DROP DOMAIN IF EXISTS vc_instant CASCADE")
        cur.execute("CREATE DOMAIN vc_instant AS timestamptz")
        cur.execute("ALTER TABLE vc_items ADD COLUMN at3 timestamptz(3), ADD COLUMN atd vc_instant")
        cur.execute("SHOW TimeZone")
        original = cur.fetchone()[0]
    tracked(items)
    try:
        set_timezone(items, "UTC")
        with items.cursor() as cur:
            cur.execute(
                "INSERT INTO vc_items VALUES "
                "(1, 'a', 's', 0, '2026-10-01 12:00:00.123+00', '2026-10-01 12:00+00')"
            )
        set_timezone(items, "Asia/Tokyo")
        with items.cursor() as cur:
            cur.execute(
                "UPDATE vc_items SET at3 = '2026-10-02 12:00:00.456+00', "
                "atd = '2026-10-02 12:00+00' WHERE id = 1"
            )
        set_timezone(items, "America/New_York")
        assert verify_diff_chain(items, table="vc_items", full=True) == []
    finally:
        set_timezone(items, original)
        with items.cursor() as cur:
            cur.execute("DROP TABLE IF EXISTS vc_items CASCADE")
            cur.execute("DROP DOMAIN IF EXISTS vc_instant CASCADE")
