"""Integration: time-travel against a real row lifecycle."""

from time import sleep

import pytest

from auditrum.timetravel import (
    reconstruct_field_history,
    reconstruct_row,
    reconstruct_table,
)
from auditrum.triggers import generate_trigger_sql


@pytest.fixture
def sample_users(fresh_auditlog):
    conn = fresh_auditlog
    with conn.cursor() as cur:
        cur.execute("DROP TABLE IF EXISTS tt_users CASCADE")
        cur.execute(
            "CREATE TABLE tt_users (id serial PRIMARY KEY, name text, email text)"
        )
        cur.execute(generate_trigger_sql("tt_users"))
    yield conn
    with conn.cursor() as cur:
        cur.execute("DROP TABLE IF EXISTS tt_users CASCADE")


def _now(conn):
    with conn.cursor() as cur:
        cur.execute("SELECT now()")
        return cur.fetchone()[0]


class TestReconstructRow:
    def test_returns_none_before_insert(self, sample_users):
        conn = sample_users
        before = _now(conn)
        sleep(0.05)
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO tt_users (name, email) VALUES ('alice', 'a@x.com')"
            )
        assert (
            reconstruct_row(
                conn, table="tt_users", object_id="1", at=before
            )
            is None
        )

    def test_returns_state_between_inserts_and_updates(self, sample_users):
        conn = sample_users
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO tt_users (name, email) VALUES ('alice', 'a@x.com') RETURNING id"
            )
            uid = cur.fetchone()[0]
        after_insert = _now(conn)
        sleep(0.05)
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE tt_users SET email = 'a2@x.com' WHERE id = %s", (uid,)
            )
        after_update = _now(conn)

        before_state = reconstruct_row(
            conn, table="tt_users", object_id=str(uid), at=after_insert
        )
        after_state = reconstruct_row(
            conn, table="tt_users", object_id=str(uid), at=after_update
        )
        assert before_state["email"] == "a@x.com"
        assert after_state["email"] == "a2@x.com"

    def test_returns_none_after_delete(self, sample_users):
        conn = sample_users
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO tt_users (name, email) VALUES ('bob', 'b@x.com') RETURNING id"
            )
            uid = cur.fetchone()[0]
            cur.execute("DELETE FROM tt_users WHERE id = %s", (uid,))
        after_delete = _now(conn)

        assert (
            reconstruct_row(
                conn, table="tt_users", object_id=str(uid), at=after_delete
            )
            is None
        )

    def test_recreation_returns_new_state(self, sample_users):
        conn = sample_users
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO tt_users (name, email) VALUES ('carol', 'c@x.com') RETURNING id"
            )
            uid = cur.fetchone()[0]
            cur.execute("DELETE FROM tt_users WHERE id = %s", (uid,))
            # Re-insert with same id via explicit assignment (emulates recreation)
            cur.execute(
                "INSERT INTO tt_users (id, name, email) VALUES (%s, 'carol2', 'c2@x.com')",
                (uid,),
            )
        after_recreate = _now(conn)
        state = reconstruct_row(
            conn, table="tt_users", object_id=str(uid), at=after_recreate
        )
        assert state["email"] == "c2@x.com"
        assert state["name"] == "carol2"


class TestReconstructTable:
    def test_surviving_rows_only(self, sample_users):
        conn = sample_users
        with conn.cursor() as cur:
            cur.execute("INSERT INTO tt_users (name) VALUES ('a'), ('b'), ('c')")
            cur.execute("DELETE FROM tt_users WHERE name = 'b'")
        at = _now(conn)
        result = dict(reconstruct_table(conn, table="tt_users", at=at))
        names = sorted(r["name"] for r in result.values())
        assert names == ["a", "c"]

    def test_time_in_the_past(self, sample_users):
        conn = sample_users
        with conn.cursor() as cur:
            cur.execute("INSERT INTO tt_users (name) VALUES ('old')")
        before_second = _now(conn)
        sleep(0.05)
        with conn.cursor() as cur:
            cur.execute("INSERT INTO tt_users (name) VALUES ('new')")

        # At 'before_second' only 'old' existed
        names = sorted(
            r["name"]
            for _, r in reconstruct_table(conn, table="tt_users", at=before_second)
        )
        assert names == ["old"]


class TestReconstructFieldHistory:
    def test_full_timeline(self, sample_users):
        conn = sample_users
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO tt_users (name, email) VALUES ('a', 'a1@x.com') RETURNING id"
            )
            uid = cur.fetchone()[0]
            cur.execute("UPDATE tt_users SET email = 'a2@x.com' WHERE id = %s", (uid,))
            cur.execute("UPDATE tt_users SET name = 'a_renamed' WHERE id = %s", (uid,))
            cur.execute("UPDATE tt_users SET email = 'a3@x.com' WHERE id = %s", (uid,))

        history = reconstruct_field_history(
            conn, table="tt_users", object_id=str(uid), field="email"
        )
        values = [v for _, v in history]
        assert values == ["a1@x.com", "a2@x.com", "a3@x.com"]

    def test_delete_closes_timeline(self, sample_users):
        conn = sample_users
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO tt_users (name, email) VALUES ('z', 'z@x.com') RETURNING id"
            )
            uid = cur.fetchone()[0]
            cur.execute("DELETE FROM tt_users WHERE id = %s", (uid,))

        history = reconstruct_field_history(
            conn, table="tt_users", object_id=str(uid), field="email"
        )
        assert len(history) == 2
        assert history[0][1] == "z@x.com"
        assert history[1][1] is None
