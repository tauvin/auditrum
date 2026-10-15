"""Integration: diff-based reconstruction in _audit_reconstruct_row."""

import uuid

import psycopg
import pytest

from auditrum.executor import PsycopgExecutor
from auditrum.timetravel import reconstruct_row
from auditrum.tracking import FieldFilter, TrackSpec, TriggerManager


def install(conn, spec):
    mgr = TriggerManager(PsycopgExecutor(conn))
    mgr.bootstrap()
    mgr.install(spec, force=True)


def now(conn):
    with conn.cursor() as cur:
        cur.execute("SELECT clock_timestamp()")
        return cur.fetchone()[0]


def row_at(conn, at, object_id="1", table="items"):
    return reconstruct_row(conn, table=table, object_id=object_id, at=at)


@pytest.fixture
def items(fresh_auditlog):
    conn = fresh_auditlog
    with conn.cursor() as cur:
        cur.execute("DROP TABLE IF EXISTS items CASCADE")
        cur.execute("CREATE TABLE items (id int PRIMARY KEY, name text, secret text, n int)")
    yield conn
    with conn.cursor() as cur:
        cur.execute("DROP TABLE IF EXISTS items CASCADE")


@pytest.mark.parametrize("storage", ["full", "diff"])
class TestReconstructRowBothModes:
    def test_point_in_time_between_events(self, items, storage):
        conn = items
        install(conn, TrackSpec(table="items", storage=storage))
        with conn.cursor() as cur:
            before_insert = now(conn)
            cur.execute("INSERT INTO items VALUES (1, 'a', 's', 1)")
            after_insert = now(conn)
            cur.execute("UPDATE items SET name = 'b' WHERE id = 1")
            after_first = now(conn)
            cur.execute("UPDATE items SET n = 2 WHERE id = 1")
        assert row_at(conn, before_insert) is None
        assert row_at(conn, after_insert) == {"id": 1, "name": "a", "secret": "s", "n": 1}
        assert row_at(conn, after_first) == {"id": 1, "name": "b", "secret": "s", "n": 1}
        assert row_at(conn, now(conn)) == {"id": 1, "name": "b", "secret": "s", "n": 2}

    def test_deleted_row(self, items, storage):
        conn = items
        install(conn, TrackSpec(table="items", storage=storage))
        with conn.cursor() as cur:
            cur.execute("INSERT INTO items VALUES (1, 'a', 's', 1)")
            cur.execute("UPDATE items SET name = 'b' WHERE id = 1")
            before_delete = now(conn)
            cur.execute("DELETE FROM items WHERE id = 1")
        assert row_at(conn, before_delete) == {"id": 1, "name": "b", "secret": "s", "n": 1}
        assert row_at(conn, now(conn)) is None

    def test_delete_then_reinsert(self, items, storage):
        conn = items
        install(conn, TrackSpec(table="items", storage=storage))
        with conn.cursor() as cur:
            cur.execute("INSERT INTO items VALUES (1, 'a', 's', 1)")
            first_life = now(conn)
            cur.execute("DELETE FROM items WHERE id = 1")
            gap = now(conn)
            cur.execute("INSERT INTO items VALUES (1, 'z', 't', 9)")
        assert row_at(conn, first_life) == {"id": 1, "name": "a", "secret": "s", "n": 1}
        assert row_at(conn, gap) is None
        assert row_at(conn, now(conn)) == {"id": 1, "name": "z", "secret": "t", "n": 9}

    def test_insert_lost_to_retention(self, items, storage):
        conn = items
        install(conn, TrackSpec(table="items", storage=storage))
        with conn.cursor() as cur:
            cur.execute("INSERT INTO items VALUES (1, 'a', 's', 1)")
            cur.execute("UPDATE items SET name = 'b' WHERE id = 1")
            mid = now(conn)
            cur.execute("UPDATE items SET name = 'c' WHERE id = 1")
            cur.execute("DELETE FROM auditlog WHERE operation = 'INSERT'")
        assert row_at(conn, mid) == {"id": 1, "name": "b", "secret": "s", "n": 1}

    def test_excluded_columns_absent(self, items, storage):
        conn = items
        install(
            conn, TrackSpec(table="items", fields=FieldFilter.exclude("secret"), storage=storage)
        )
        with conn.cursor() as cur:
            cur.execute("INSERT INTO items VALUES (1, 'a', 's', 1)")
            mid = now(conn)
            cur.execute("UPDATE items SET name = 'b', secret = 't' WHERE id = 1")
        assert row_at(conn, mid) == {"id": 1, "name": "a", "n": 1}

    def test_concurrent_writer_cut(self, items, storage, pg_dsn):
        """Transaction A starts before `at` but writes after B: both are
        after the cut, so the state at `at` is the original row."""
        conn = items
        install(conn, TrackSpec(table="items", storage=storage))
        with conn.cursor() as cur:
            cur.execute("INSERT INTO items VALUES (1, 'orig', 's', 0)")
        with psycopg.connect(pg_dsn) as a:
            a.execute("SELECT now()")  # A's transaction starts here
            at = now(conn)
            with conn.cursor() as cur:
                cur.execute("UPDATE items SET name = 'B' WHERE id = 1")
            a.execute("UPDATE items SET n = 42 WHERE id = 1")
            a.commit()
        assert row_at(conn, at) == {"id": 1, "name": "orig", "secret": "s", "n": 0}

    def test_null_values_survive_fold(self, items, storage):
        conn = items
        install(conn, TrackSpec(table="items", storage=storage))
        with conn.cursor() as cur:
            cur.execute("INSERT INTO items VALUES (1, 'a', NULL, 1)")
            with_null = now(conn)
            cur.execute("UPDATE items SET secret = 'x' WHERE id = 1")
            cur.execute("UPDATE items SET secret = NULL WHERE id = 1")
        assert row_at(conn, with_null) == {"id": 1, "name": "a", "secret": None, "n": 1}


class TestReconstructRowDiffOnly:
    def test_mixed_full_then_diff_history(self, items):
        conn = items
        install(conn, TrackSpec(table="items"))
        with conn.cursor() as cur:
            cur.execute("INSERT INTO items VALUES (1, 'a', 's', 1)")
            full_era = now(conn)
            cur.execute("UPDATE items SET name = 'b' WHERE id = 1")
        install(conn, TrackSpec(table="items", storage="diff"))
        with conn.cursor() as cur:
            switch = now(conn)
            cur.execute("UPDATE items SET n = 2 WHERE id = 1")
            diff_era = now(conn)
            cur.execute("UPDATE items SET name = 'c' WHERE id = 1")
        assert row_at(conn, full_era) == {"id": 1, "name": "a", "secret": "s", "n": 1}
        assert row_at(conn, switch) == {"id": 1, "name": "b", "secret": "s", "n": 1}
        assert row_at(conn, diff_era) == {"id": 1, "name": "b", "secret": "s", "n": 2}

    def test_missing_registry_entry_raises(self, items):
        conn = items
        install(conn, TrackSpec(table="items", storage="diff"))
        with conn.cursor() as cur:
            cur.execute("INSERT INTO items VALUES (1, 'a', 's', 1)")
            mid = now(conn)
            cur.execute("UPDATE items SET name = 'b' WHERE id = 1")
            cur.execute("DELETE FROM auditrum_applied_triggers")
        with pytest.raises(
            psycopg.errors.RaiseException, match="no entry in auditrum_applied_triggers"
        ):
            row_at(conn, mid)

    def test_registry_without_storage_key_is_full(self, items):
        """Registry rows from before 0.7 have no "storage" key: such a table
        is full, so an untracked row is not read from the live table."""
        conn = items
        install(conn, TrackSpec(table="items"))
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE auditrum_applied_triggers "
                "SET spec_fingerprint = spec_fingerprint - 'storage'"
            )
            cur.execute("ALTER TABLE items DISABLE TRIGGER USER")
            cur.execute("INSERT INTO items VALUES (1, 'a', 's', 1)")
            cur.execute("ALTER TABLE items ENABLE TRIGGER USER")
        assert row_at(conn, now(conn)) is None

    def test_untracked_row_read_from_live_table(self, items):
        conn = items
        with conn.cursor() as cur:
            cur.execute("INSERT INTO items VALUES (1, 'a', 's', 1)")
        install(conn, TrackSpec(table="items", storage="diff"))
        assert row_at(conn, now(conn)) == {"id": 1, "name": "a", "secret": "s", "n": 1}

    def test_uuid_primary_key(self, fresh_auditlog):
        conn = fresh_auditlog
        pk = uuid.uuid4()
        with conn.cursor() as cur:
            cur.execute("DROP TABLE IF EXISTS docs CASCADE")
            cur.execute("CREATE TABLE docs (id uuid PRIMARY KEY, title text)")
        install(conn, TrackSpec(table="docs", storage="diff"))
        with conn.cursor() as cur:
            cur.execute("INSERT INTO docs VALUES (%s, 'v1')", (pk,))
            mid = now(conn)
            cur.execute("UPDATE docs SET title = 'v2' WHERE id = %s", (pk,))
        assert row_at(conn, mid, object_id=str(pk), table="docs") == {"id": str(pk), "title": "v1"}
        with conn.cursor() as cur:
            cur.execute("DROP TABLE IF EXISTS docs CASCADE")

    def test_nested_jsonb_value_is_replaced_whole(self, fresh_auditlog):
        conn = fresh_auditlog
        with conn.cursor() as cur:
            cur.execute("DROP TABLE IF EXISTS prefs CASCADE")
            cur.execute("CREATE TABLE prefs (id int PRIMARY KEY, meta jsonb)")
        install(conn, TrackSpec(table="prefs", storage="diff"))
        with conn.cursor() as cur:
            cur.execute("""INSERT INTO prefs VALUES (1, '{"a": 1}')""")
            mid = now(conn)
            cur.execute("""UPDATE prefs SET meta = '{"b": 2}' WHERE id = 1""")
        assert row_at(conn, mid, table="prefs") == {"id": 1, "meta": {"a": 1}}
        with conn.cursor() as cur:
            cur.execute("DROP TABLE IF EXISTS prefs CASCADE")

    def test_security_invoker_requires_select_on_table(self, items):
        conn = items
        install(conn, TrackSpec(table="items", storage="diff"))
        with conn.cursor() as cur:
            cur.execute("INSERT INTO items VALUES (1, 'a', 's', 1)")
            mid = now(conn)
            cur.execute("UPDATE items SET name = 'b' WHERE id = 1")
            cur.execute("DROP ROLE IF EXISTS tt_reader")
            cur.execute("CREATE ROLE tt_reader")
            cur.execute("GRANT USAGE ON SCHEMA public TO tt_reader")
            cur.execute("GRANT SELECT ON auditlog, auditrum_applied_triggers TO tt_reader")
            cur.execute("SET ROLE tt_reader")
            try:
                with pytest.raises(psycopg.errors.InsufficientPrivilege):
                    cur.execute("SELECT _audit_reconstruct_row('items', '1', %s)", (mid,))
            finally:
                cur.execute("RESET ROLE")
                cur.execute("DROP OWNED BY tt_reader")
                cur.execute("DROP ROLE tt_reader")
