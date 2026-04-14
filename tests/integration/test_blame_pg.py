"""Integration test: blame against a real Postgres row history."""

import pytest

from auditrum.blame import fetch_blame, format_blame
from auditrum.triggers import generate_trigger_sql


@pytest.fixture
def users_with_history(fresh_auditlog):
    conn = fresh_auditlog
    with conn.cursor() as cur:
        cur.execute("DROP TABLE IF EXISTS app_users CASCADE")
        cur.execute(
            "CREATE TABLE app_users ("
            "id serial PRIMARY KEY, "
            "name text, "
            "email text"
            ")"
        )
        cur.execute(generate_trigger_sql("app_users"))

        # Seed a row and mutate it a few times
        cur.execute(
            "INSERT INTO app_users (name, email) VALUES ('alice', 'a@x.com') RETURNING id"
        )
        user_id = cur.fetchone()[0]
        cur.execute("UPDATE app_users SET email = 'a2@x.com' WHERE id = %s", (user_id,))
        cur.execute(
            "UPDATE app_users SET name = 'alice2', email = 'a3@x.com' WHERE id = %s",
            (user_id,),
        )
    yield conn, str(user_id)
    with conn.cursor() as cur:
        cur.execute("DROP TABLE IF EXISTS app_users CASCADE")


class TestBlameRoundtrip:
    def test_fetches_full_history_in_order(self, users_with_history):
        conn, uid = users_with_history
        entries = fetch_blame(conn, table="app_users", object_id=uid)
        ops = [e.operation for e in entries]
        assert ops == ["INSERT", "UPDATE", "UPDATE"]

    def test_field_filter_only_picks_email_changes(self, users_with_history):
        conn, uid = users_with_history
        entries = fetch_blame(conn, table="app_users", object_id=uid, field="email")
        # INSERT + 2 UPDATEs touched email = 3 entries
        assert len(entries) == 3
        assert entries[0].operation == "INSERT"
        assert entries[0].new_value == "a@x.com"
        assert entries[1].old_value == "a@x.com"
        assert entries[1].new_value == "a2@x.com"

    def test_field_filter_name_skips_email_only_update(self, users_with_history):
        conn, uid = users_with_history
        entries = fetch_blame(conn, table="app_users", object_id=uid, field="name")
        # INSERT + final UPDATE changed name; middle UPDATE only touched email
        assert len(entries) == 2

    def test_format_text_plain_smoke(self, users_with_history):
        conn, uid = users_with_history
        entries = fetch_blame(conn, table="app_users", object_id=uid)
        out = format_blame(entries, fmt="text", table="app_users", object_id=uid)
        assert "INSERT" in out
        assert "UPDATE" in out
        # No rich markup in text mode
        assert "[bold]" not in out
        assert "[yellow]" not in out

    def test_nonexistent_row(self, users_with_history):
        conn, _ = users_with_history
        entries = fetch_blame(conn, table="app_users", object_id="9999999")
        assert entries == []
