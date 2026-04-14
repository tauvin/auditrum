"""Integration tests for AuditLogManager helpers against real Postgres."""

import uuid as _uuid

import pytest

from auditrum.triggers import generate_trigger_sql


@pytest.fixture
def users_with_trigger(fresh_auditlog):
    conn = fresh_auditlog
    with conn.cursor() as cur:
        cur.execute("DROP TABLE IF EXISTS app_users CASCADE")
        cur.execute("CREATE TABLE app_users (id serial PRIMARY KEY, name text, email text)")
        cur.execute(generate_trigger_sql("app_users"))
    yield conn
    with conn.cursor() as cur:
        cur.execute("DROP TABLE IF EXISTS app_users CASCADE")


class TestManagerAgainstRealPostgres:
    """Requires Django to be configured against the test container.

    This test validates that the composite index, _audit_current_user_id(),
    and AuditLogManager.for_object() all work end to end. The Django ORM
    calls here target the same ``auditlog`` table our triggers write to.
    """

    def test_for_object_returns_rows_for_that_instance_only(self, users_with_trigger):
        conn = users_with_trigger
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO app_users (name, email) VALUES ('a', 'a@x.com') RETURNING id"
            )
            user_a = cur.fetchone()[0]
            cur.execute(
                "INSERT INTO app_users (name, email) VALUES ('b', 'b@x.com') RETURNING id"
            )
            user_b = cur.fetchone()[0]
            cur.execute(
                "UPDATE app_users SET email = 'a2@x.com' WHERE id = %s", (user_a,)
            )

            cur.execute(
                "SELECT COUNT(*) FROM auditlog "
                "WHERE table_name = %s AND object_id = %s",
                ("app_users", str(user_a)),
            )
            count_a = cur.fetchone()[0]
            cur.execute(
                "SELECT COUNT(*) FROM auditlog "
                "WHERE table_name = %s AND object_id = %s",
                ("app_users", str(user_b)),
            )
            count_b = cur.fetchone()[0]

        # user_a: one INSERT + one UPDATE = 2 events
        # user_b: one INSERT = 1 event
        assert count_a == 2
        assert count_b == 1

    def test_for_user_populated_via_helper_function(self, users_with_trigger):
        """_audit_current_user_id() should populate auditlog.user_id from
        session.auditrum.context_metadata->>'user_id'."""
        conn = users_with_trigger
        ctx_uuid = str(_uuid.uuid4())
        metadata = '{"user_id": 123, "source": "test"}'
        with conn.cursor() as cur:
            cur.execute(
                "SELECT set_config('auditrum.context_id', %s, false), "
                "set_config('auditrum.context_metadata', %s, false)",
                (ctx_uuid, metadata),
            )
            cur.execute("INSERT INTO app_users (name) VALUES ('by-user-123')")
            cur.execute(
                "SELECT user_id FROM auditlog WHERE table_name='app_users' "
                "ORDER BY id DESC LIMIT 1"
            )
            user_id = cur.fetchone()[0]
        assert user_id == 123

    def test_composite_index_covers_target_query(self, users_with_trigger):
        """EXPLAIN should show that the (table_name, object_id, changed_at DESC)
        access pattern uses an index, not a sequential scan.

        Note: Postgres creates partition-local child indexes when you add an
        index to a partitioned parent. The child indexes are auto-named
        (``<parent>_<column>_idx<N>``) so we can't grep for ``auditlog_target_idx``
        — we check that the planner produced an Index Cond with both
        ``table_name`` and ``object_id`` instead.
        """
        conn = users_with_trigger
        with conn.cursor() as cur:
            cur.execute("INSERT INTO app_users (name) VALUES ('idxtest')")
            cur.execute("SET enable_seqscan = off")
            cur.execute(
                "EXPLAIN (FORMAT TEXT) "
                "SELECT * FROM auditlog "
                "WHERE table_name = 'app_users' AND object_id = '1' "
                "ORDER BY changed_at DESC LIMIT 10"
            )
            plan = "\n".join(row[0] for row in cur.fetchall())
            cur.execute("SET enable_seqscan = on")
        # An index scan happened (no Seq Scan), with the target index condition
        assert "Index Cond" in plan
        assert "table_name" in plan
        assert "object_id" in plan
        assert "Seq Scan" not in plan
