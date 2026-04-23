import pytest

from auditrum.triggers import generate_trigger_sql


@pytest.fixture
def sample_table(fresh_auditlog):
    conn = fresh_auditlog
    with conn.cursor() as cur:
        cur.execute("DROP TABLE IF EXISTS users CASCADE")
        cur.execute(
            "CREATE TABLE users ("
            "id serial PRIMARY KEY, "
            "name text, "
            "email text, "
            "password text, "
            "is_active boolean DEFAULT true"
            ")"
        )
    yield conn
    with conn.cursor() as cur:
        cur.execute("DROP TABLE IF EXISTS users CASCADE")


class TestTriggerRoundtrip:
    def test_insert_creates_audit_row(self, sample_table):
        conn = sample_table
        with conn.cursor() as cur:
            cur.execute(generate_trigger_sql("users"))
            cur.execute("INSERT INTO users (name, email) VALUES ('alice', 'a@x.com')")
            cur.execute("SELECT operation, table_name FROM auditlog")
            rows = cur.fetchall()
        assert len(rows) == 1
        assert rows[0][0] == "INSERT"
        assert rows[0][1] == "users"

    def test_update_creates_paired_diff(self, sample_table):
        """UPDATE diff is in paired ``{field: {old, new}}`` form — the
        self-sufficient shape a UI can render without cross-referencing
        ``old_data``."""
        conn = sample_table
        with conn.cursor() as cur:
            cur.execute(generate_trigger_sql("users"))
            cur.execute("INSERT INTO users (name, email) VALUES ('alice', 'a@x.com')")
            cur.execute("UPDATE users SET email = 'new@x.com' WHERE name = 'alice'")
            cur.execute("SELECT operation, diff FROM auditlog WHERE operation = 'UPDATE'")
            rows = cur.fetchall()
        assert len(rows) == 1
        assert rows[0][1] == {"email": {"old": "a@x.com", "new": "new@x.com"}}

    def test_insert_diff_uses_paired_shape(self, sample_table):
        conn = sample_table
        with conn.cursor() as cur:
            cur.execute(generate_trigger_sql("users"))
            cur.execute("INSERT INTO users (name, email) VALUES ('zoe', 'z@x.com')")
            cur.execute("SELECT diff FROM auditlog WHERE operation = 'INSERT'")
            (diff,) = cur.fetchone()
        assert diff["name"] == {"old": None, "new": "zoe"}
        assert diff["email"] == {"old": None, "new": "z@x.com"}

    def test_delete_diff_uses_paired_shape(self, sample_table):
        conn = sample_table
        with conn.cursor() as cur:
            cur.execute(generate_trigger_sql("users"))
            cur.execute("INSERT INTO users (name, email) VALUES ('y', 'y@x.com')")
            cur.execute("DELETE FROM users WHERE name = 'y'")
            cur.execute("SELECT diff FROM auditlog WHERE operation = 'DELETE'")
            (diff,) = cur.fetchone()
        assert diff["name"] == {"old": "y", "new": None}
        assert diff["email"] == {"old": "y@x.com", "new": None}

    def test_update_to_null_value_is_not_dropped(self, sample_table):
        """Regression: the 0.3.x trigger ran ``jsonb_strip_nulls`` over
        the diff, which silently dropped UPDATEs that set a column to
        NULL. Paired format makes the wrapper obsolete — a null ``new``
        is now a real, queryable value."""
        conn = sample_table
        with conn.cursor() as cur:
            cur.execute(generate_trigger_sql("users"))
            cur.execute("INSERT INTO users (name, email) VALUES ('n', 'e@x.com')")
            cur.execute("UPDATE users SET email = NULL WHERE name = 'n'")
            cur.execute("SELECT diff FROM auditlog WHERE operation = 'UPDATE'")
            rows = cur.fetchall()
        assert len(rows) == 1
        assert rows[0][0] == {"email": {"old": "e@x.com", "new": None}}

    def test_delete_captures_old_data(self, sample_table):
        conn = sample_table
        with conn.cursor() as cur:
            cur.execute(generate_trigger_sql("users"))
            cur.execute("INSERT INTO users (name, email) VALUES ('bob', 'b@x.com')")
            cur.execute("DELETE FROM users WHERE name = 'bob'")
            cur.execute("SELECT operation, old_data FROM auditlog WHERE operation = 'DELETE'")
            rows = cur.fetchall()
        assert len(rows) == 1
        assert rows[0][1]["name"] == "bob"

    def test_exclude_fields_filters_diff(self, sample_table):
        conn = sample_table
        with conn.cursor() as cur:
            cur.execute(generate_trigger_sql("users", exclude_fields=["password"]))
            cur.execute(
                "INSERT INTO users (name, email, password) VALUES ('c', 'c@x.com', 'secret')"
            )
            cur.execute("UPDATE users SET password = 'new-secret', email = 'c2@x.com' "
                        "WHERE name = 'c'")
            cur.execute("SELECT diff FROM auditlog WHERE operation = 'UPDATE'")
            diff = cur.fetchone()[0]
        assert "password" not in diff
        assert diff["email"] == {"old": "c@x.com", "new": "c2@x.com"}

    def test_track_only_ignores_other_fields(self, sample_table):
        conn = sample_table
        with conn.cursor() as cur:
            cur.execute(generate_trigger_sql("users", track_only=["name"]))
            cur.execute("INSERT INTO users (name, email) VALUES ('d', 'd@x.com')")
            cur.execute("UPDATE users SET email = 'd2@x.com' WHERE name = 'd'")
            cur.execute("SELECT COUNT(*) FROM auditlog WHERE operation = 'UPDATE'")
            # email changed but not in track_only → no UPDATE row
            count = cur.fetchone()[0]
        assert count == 0

    def test_session_context_propagates_via_lazy_attach(self, sample_table):
        """Context GUCs should lazily populate audit_context table via _audit_attach_context()."""
        conn = sample_table
        ctx_uuid = "00000000-0000-0000-0000-0000000000aa"
        metadata_json = '{"username": "alice_admin", "source": "http"}'
        with conn.cursor() as cur:
            cur.execute(generate_trigger_sql("users"))
            # Emulate execute_wrapper: set context GUCs for this tx
            cur.execute(
                "SELECT set_config('auditrum.context_id', %s, false), "
                "set_config('auditrum.context_metadata', %s, false)",
                (ctx_uuid, metadata_json),
            )
            cur.execute("INSERT INTO users (name) VALUES ('e')")
            cur.execute(
                "SELECT context_id FROM auditlog ORDER BY id DESC LIMIT 1"
            )
            audit_ctx_id = cur.fetchone()[0]
        assert str(audit_ctx_id) == ctx_uuid
        with conn.cursor() as cur:
            cur.execute("SELECT metadata FROM audit_context WHERE id = %s", (ctx_uuid,))
            row = cur.fetchone()
        assert row is not None
        assert row[0]["username"] == "alice_admin"
        assert row[0]["source"] == "http"

    def test_no_context_row_without_guc(self, sample_table):
        """Lazy attach: read-only requests should not create context rows."""
        conn = sample_table
        with conn.cursor() as cur:
            cur.execute(generate_trigger_sql("users"))
            cur.execute("INSERT INTO users (name) VALUES ('no-ctx')")
            cur.execute("SELECT context_id FROM auditlog ORDER BY id DESC LIMIT 1")
            row = cur.fetchone()
            assert row[0] is None
            cur.execute("SELECT COUNT(*) FROM audit_context")
            assert cur.fetchone()[0] == 0
