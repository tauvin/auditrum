import pytest

from auditrum.revert import generate_revert_sql, generate_revert_sql_from_log
from auditrum.tracking import TrackSpec
from auditrum.triggers import generate_trigger_sql


@pytest.fixture
def users_with_trigger(fresh_auditlog):
    conn = fresh_auditlog
    with conn.cursor() as cur:
        cur.execute("DROP TABLE IF EXISTS users CASCADE")
        cur.execute("CREATE TABLE users (id serial PRIMARY KEY, name text, email text)")
        cur.execute(generate_trigger_sql("users"))
    yield conn
    with conn.cursor() as cur:
        cur.execute("DROP TABLE IF EXISTS users CASCADE")


class TestRevertRoundtrip:
    def test_revert_restores_old_values(self, users_with_trigger):
        conn = users_with_trigger
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO users (name, email) VALUES ('alice', 'a@old.com') RETURNING id"
            )
            user_id = cur.fetchone()[0]
            cur.execute("UPDATE users SET email = 'a@new.com' WHERE id = %s", (user_id,))
            cur.execute(
                "SELECT id FROM auditlog WHERE operation = 'UPDATE' ORDER BY id DESC LIMIT 1"
            )
            log_id = cur.fetchone()[0]

            sql = generate_revert_sql_from_log(conn, "auditlog", "users", str(user_id), log_id)
            cur.execute(sql)
            cur.execute("SELECT email FROM users WHERE id = %s", (user_id,))
            assert cur.fetchone()[0] == "a@old.com"

    def test_generate_revert_sql_injection_blocked(self):
        with pytest.raises(ValueError):
            generate_revert_sql("auditlog", "users; DROP", "1", 1, ["name"])

    def test_revert_keeps_later_changes_to_other_columns(self, users_with_trigger):
        conn = users_with_trigger
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO users (name, email) VALUES ('alice', 'a@old.com') RETURNING id"
            )
            user_id = cur.fetchone()[0]
            cur.execute("UPDATE users SET email = 'a@new.com' WHERE id = %s", (user_id,))
            cur.execute("SELECT max(id) FROM auditlog WHERE operation = 'UPDATE'")
            log_id = cur.fetchone()[0]
            cur.execute("UPDATE users SET name = 'alice2' WHERE id = %s", (user_id,))
            cur.execute(
                generate_revert_sql_from_log(conn, "auditlog", "users", str(user_id), log_id)
            )
            cur.execute("SELECT name, email FROM users WHERE id = %s", (user_id,))
            assert cur.fetchone() == ("alice2", "a@old.com")

    def test_revert_on_diff_table(self, fresh_auditlog):
        conn = fresh_auditlog
        with conn.cursor() as cur:
            cur.execute("DROP TABLE IF EXISTS users CASCADE")
            cur.execute("CREATE TABLE users (id serial PRIMARY KEY, name text, email text)")
            cur.execute(TrackSpec(table="users", storage="diff").build().install_sql)
            cur.execute("INSERT INTO users (name, email) VALUES ('bob', 'b@old.com') RETURNING id")
            user_id = cur.fetchone()[0]
            cur.execute("UPDATE users SET email = 'b@new.com' WHERE id = %s", (user_id,))
            cur.execute("SELECT max(id) FROM auditlog WHERE operation = 'UPDATE'")
            log_id = cur.fetchone()[0]
            cur.execute(
                generate_revert_sql_from_log(conn, "auditlog", "users", str(user_id), log_id)
            )
            cur.execute("SELECT email FROM users WHERE id = %s", (user_id,))
            assert cur.fetchone()[0] == "b@old.com"
            cur.execute("DROP TABLE IF EXISTS users CASCADE")
