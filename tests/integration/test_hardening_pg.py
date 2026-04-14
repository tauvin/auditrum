import psycopg
import pytest

from auditrum.hardening import generate_grant_admin_sql, generate_revoke_sql
from auditrum.hash_chain import generate_hash_chain_sql, get_chain_tip, verify_chain
from auditrum.retention import drop_old_partitions, generate_purge_sql
from auditrum.schema import (
    generate_audit_attach_context_sql,
    generate_audit_context_table_sql,
    generate_audit_current_user_id_sql,
    generate_auditlog_partitions_sql,
    generate_auditlog_table_sql,
    generate_jsonb_diff_function_sql,
)
from auditrum.triggers import generate_trigger_sql


@pytest.fixture
def audit_setup(pg_conn):
    with pg_conn.cursor() as cur:
        cur.execute("DROP TABLE IF EXISTS auditlog CASCADE")
        cur.execute("DROP TABLE IF EXISTS audit_context CASCADE")
        cur.execute("DROP TABLE IF EXISTS widgets CASCADE")
        cur.execute("DROP FUNCTION IF EXISTS jsonb_diff(jsonb, jsonb) CASCADE")
        cur.execute("DROP FUNCTION IF EXISTS _audit_attach_context() CASCADE")
        cur.execute("DROP FUNCTION IF EXISTS _audit_current_user_id() CASCADE")
        cur.execute(generate_audit_context_table_sql("audit_context"))
        cur.execute(generate_auditlog_table_sql("auditlog"))
        cur.execute(generate_jsonb_diff_function_sql())
        cur.execute(generate_audit_attach_context_sql("audit_context"))
        cur.execute(generate_audit_current_user_id_sql())
        cur.execute(generate_auditlog_partitions_sql("auditlog", months_ahead=1))
        cur.execute("CREATE TABLE widgets (id serial PRIMARY KEY, name text)")
        cur.execute(generate_trigger_sql("widgets"))
    yield pg_conn
    with pg_conn.cursor() as cur:
        cur.execute("DROP FUNCTION IF EXISTS _audit_attach_context() CASCADE")
        cur.execute("DROP FUNCTION IF EXISTS _audit_current_user_id() CASCADE")
        cur.execute("DROP TABLE IF EXISTS auditlog CASCADE")
        cur.execute("DROP TABLE IF EXISTS audit_context CASCADE")
        cur.execute("DROP TABLE IF EXISTS widgets CASCADE")


@pytest.fixture
def limited_role(audit_setup):
    """Set up a non-superuser role with minimal privileges on tracked tables
    but NO direct write access to the audit tables. Drops the role on teardown.

    The role needs:
    - SELECT/INSERT/UPDATE/DELETE on widgets (the tracked table)
    - SELECT/USAGE on the sequence (so it can INSERT)
    - SELECT on auditlog (so the in-app history view still works)
    - NO direct write access on auditlog / audit_context (those come via
      the SECURITY DEFINER trigger functions only)
    """
    conn = audit_setup
    with conn.cursor() as cur:
        cur.execute("DROP ROLE IF EXISTS app_test_user")
        cur.execute("CREATE ROLE app_test_user LOGIN PASSWORD 'x'")
        cur.execute(
            "GRANT SELECT, INSERT, UPDATE, DELETE ON widgets TO app_test_user"
        )
        cur.execute(
            "GRANT USAGE, SELECT ON SEQUENCE widgets_id_seq TO app_test_user"
        )
        # Grant SELECT explicitly so we can verify hardening preserves it
        cur.execute("GRANT SELECT ON auditlog TO app_test_user")
        cur.execute("GRANT SELECT ON audit_context TO app_test_user")
        # Apply the hardening — REVOKE writes on auditlog + audit_context
        cur.execute(generate_revoke_sql("auditlog", app_role="app_test_user"))

    yield "app_test_user"

    with conn.cursor() as cur:
        cur.execute("DROP OWNED BY app_test_user CASCADE")
        cur.execute("DROP ROLE IF EXISTS app_test_user")


def _limited_dsn(pg_dsn: str, role: str = "app_test_user", password: str = "x") -> str:
    # Replace the user:pass portion of the DSN with the limited role credentials
    scheme, rest = pg_dsn.split("://", 1)
    _creds, host = rest.split("@", 1)
    return f"{scheme}://{role}:{password}@{host}"


class TestHardeningAppendOnly:
    """Append-only model: limited role can produce audit rows via triggers
    but cannot forge, modify, or delete them directly.
    """

    def test_trigger_path_still_works_for_limited_role(
        self, audit_setup, limited_role, pg_dsn
    ):
        """SECURITY DEFINER: a limited role with NO direct write on auditlog
        can still produce audit rows by writing to tracked tables.
        """
        limited_dsn = _limited_dsn(pg_dsn)
        with (
            psycopg.connect(limited_dsn, autocommit=True) as app_conn,
            app_conn.cursor() as cur,
        ):
            cur.execute("INSERT INTO widgets (name) VALUES ('from_limited')")

        # Verify the audit row actually landed — use the superuser conn to read
        conn = audit_setup
        with conn.cursor() as cur:
            cur.execute(
                "SELECT operation, table_name FROM auditlog WHERE table_name = 'widgets'"
            )
            rows = cur.fetchall()
        assert len(rows) == 1
        assert rows[0] == ("INSERT", "widgets")

    def test_direct_insert_into_auditlog_blocked(
        self, audit_setup, limited_role, pg_dsn
    ):
        """A compromised app role must not be able to forge audit rows
        by bypassing the trigger path entirely.
        """
        limited_dsn = _limited_dsn(pg_dsn)
        with (
            psycopg.connect(limited_dsn, autocommit=True) as app_conn,
            app_conn.cursor() as cur,
            pytest.raises(psycopg.errors.InsufficientPrivilege),
        ):
            cur.execute(
                "INSERT INTO auditlog (operation, table_name, object_id) "
                "VALUES ('HACKED', 'users', '1')"
            )

    def test_direct_insert_into_audit_context_blocked(
        self, audit_setup, limited_role, pg_dsn
    ):
        """Same for the context table — forging a context_id would let an
        attacker attribute their rows to a different user.
        """
        limited_dsn = _limited_dsn(pg_dsn)
        with (
            psycopg.connect(limited_dsn, autocommit=True) as app_conn,
            app_conn.cursor() as cur,
            pytest.raises(psycopg.errors.InsufficientPrivilege),
        ):
            cur.execute(
                "INSERT INTO audit_context (id, metadata) "
                "VALUES ('00000000-0000-0000-0000-000000000001', '{}'::jsonb)"
            )

    def test_direct_update_into_auditlog_blocked(
        self, audit_setup, limited_role, pg_dsn
    ):
        """Rewriting history must fail even for a normal audit row that the
        limited role legitimately produced via the trigger path.
        """
        conn = audit_setup
        with conn.cursor() as cur:
            # Produce one legitimate row first (as superuser, doesn't matter)
            cur.execute("INSERT INTO widgets (name) VALUES ('target')")

        limited_dsn = _limited_dsn(pg_dsn)
        with (
            psycopg.connect(limited_dsn, autocommit=True) as app_conn,
            app_conn.cursor() as cur,
            pytest.raises(psycopg.errors.InsufficientPrivilege),
        ):
            cur.execute("UPDATE auditlog SET operation = 'HACKED'")

    def test_direct_delete_from_auditlog_blocked(
        self, audit_setup, limited_role, pg_dsn
    ):
        limited_dsn = _limited_dsn(pg_dsn)
        with (
            psycopg.connect(limited_dsn, autocommit=True) as app_conn,
            app_conn.cursor() as cur,
            pytest.raises(psycopg.errors.InsufficientPrivilege),
        ):
            cur.execute("DELETE FROM auditlog")

    def test_select_is_still_allowed(self, audit_setup, limited_role, pg_dsn):
        """Hardening does not revoke SELECT — in-app audit history views
        should still work from the runtime app role.
        """
        limited_dsn = _limited_dsn(pg_dsn)
        with (
            psycopg.connect(limited_dsn, autocommit=True) as app_conn,
            app_conn.cursor() as cur,
        ):
            cur.execute("SELECT COUNT(*) FROM auditlog")
            assert cur.fetchone() is not None

    def test_admin_grant_restores_full_access(self, audit_setup, pg_dsn):
        """After harden-then-grant, a dedicated admin role has full
        control back (for partition drops, retention purges, etc.)."""
        conn = audit_setup
        with conn.cursor() as cur:
            cur.execute("DROP ROLE IF EXISTS audit_admin_test")
            cur.execute("CREATE ROLE audit_admin_test LOGIN PASSWORD 'x'")
            cur.execute(generate_revoke_sql("auditlog"))
            cur.execute(generate_grant_admin_sql("auditlog", "audit_admin_test"))

        admin_dsn = _limited_dsn(pg_dsn, role="audit_admin_test")
        try:
            with (
                psycopg.connect(admin_dsn, autocommit=True) as admin_conn,
                admin_conn.cursor() as cur,
            ):
                # Admin can INSERT, UPDATE, DELETE on auditlog directly
                cur.execute(
                    "INSERT INTO auditlog (operation, table_name, object_id) "
                    "VALUES ('ADMIN_INSERT', 'maintenance', '1')"
                )
                cur.execute(
                    "UPDATE auditlog SET operation = 'ADMIN_UPDATED' "
                    "WHERE operation = 'ADMIN_INSERT'"
                )
                cur.execute(
                    "DELETE FROM auditlog WHERE operation = 'ADMIN_UPDATED'"
                )
        finally:
            with conn.cursor() as cur:
                cur.execute("DROP OWNED BY audit_admin_test CASCADE")
                cur.execute("DROP ROLE IF EXISTS audit_admin_test")


class TestHashChainRoundtrip:
    def test_valid_chain_verifies(self, audit_setup):
        conn = audit_setup
        with conn.cursor() as cur:
            cur.execute(generate_hash_chain_sql("auditlog"))
            cur.execute("INSERT INTO widgets (name) VALUES ('a')")
            cur.execute("INSERT INTO widgets (name) VALUES ('b')")
            cur.execute("INSERT INTO widgets (name) VALUES ('c')")
        result = verify_chain(conn, "auditlog")
        assert result["ok"], result["broken"]
        assert result["checked"] == 3

    def test_tampered_row_is_detected(self, audit_setup):
        conn = audit_setup
        with conn.cursor() as cur:
            cur.execute(generate_hash_chain_sql("auditlog"))
            cur.execute("INSERT INTO widgets (name) VALUES ('a')")
            cur.execute("INSERT INTO widgets (name) VALUES ('b')")
            # Tamper: change new_data of an existing row directly
            cur.execute(
                "UPDATE auditlog SET new_data = '{\"name\": \"HACKED\"}'::jsonb "
                "WHERE id = (SELECT MIN(id) FROM auditlog)"
            )
        result = verify_chain(conn, "auditlog")
        assert not result["ok"]
        assert any("row_hash mismatch" in reason for _, reason in result["broken"])

    def test_tip_anchor_detects_tail_deletion(self, audit_setup):
        """The classic LAG-based verification cannot detect deletion of the
        most recent rows — there's no neighbour after the gap. Capturing
        a tip anchor and passing it to verify_chain closes the gap."""
        conn = audit_setup
        with conn.cursor() as cur:
            cur.execute(generate_hash_chain_sql("auditlog"))
            cur.execute("INSERT INTO widgets (name) VALUES ('a')")
            cur.execute("INSERT INTO widgets (name) VALUES ('b')")
            cur.execute("INSERT INTO widgets (name) VALUES ('c')")

        # Capture the tip anchor (this is what cron would store externally)
        tip = get_chain_tip(conn, "auditlog")
        assert tip["id"] is not None

        # Sanity check: chain verifies cleanly with current tip
        result = verify_chain(conn, "auditlog", expected_tip=tip)
        assert result["ok"]

        # Now delete the tail row directly (an attacker bypasses the chain)
        with conn.cursor() as cur:
            cur.execute(
                "DELETE FROM auditlog WHERE id = (SELECT MAX(id) FROM auditlog)"
            )

        # Without the tip anchor, the LAG check sees no problem
        result_no_anchor = verify_chain(conn, "auditlog")
        assert result_no_anchor["ok"]

        # With the tip anchor, the missing row is detected
        result_with_anchor = verify_chain(conn, "auditlog", expected_tip=tip)
        assert not result_with_anchor["ok"]
        assert any("tip" in reason for _, reason in result_with_anchor["broken"])

    def test_tip_anchor_detects_anchor_row_rewrite(self, audit_setup):
        """Tampering with the anchor row itself must also be detected."""
        conn = audit_setup
        with conn.cursor() as cur:
            cur.execute(generate_hash_chain_sql("auditlog"))
            cur.execute("INSERT INTO widgets (name) VALUES ('a')")
            cur.execute("INSERT INTO widgets (name) VALUES ('b')")

        tip = get_chain_tip(conn, "auditlog")

        # Rewrite the anchor row's hash directly
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE auditlog SET row_hash = 'fake_hash_value' WHERE id = %s",
                (tip["id"],),
            )

        result = verify_chain(conn, "auditlog", expected_tip=tip)
        assert not result["ok"]
        assert any(
            "tip row_hash mismatch" in reason for _, reason in result["broken"]
        )

    def test_get_chain_tip_empty_log(self, audit_setup):
        """Sane behaviour when the chain is brand new: no rows, all-None tip."""
        conn = audit_setup
        with conn.cursor() as cur:
            cur.execute(generate_hash_chain_sql("auditlog"))
        tip = get_chain_tip(conn, "auditlog")
        assert tip == {
            "id": None,
            "chain_seq": None,
            "row_hash": None,
            "changed_at": None,
        }

    def test_chain_seq_strictly_monotonic(self, audit_setup):
        """chain_seq is assigned inside the advisory lock, so successive
        inserts get strictly increasing values regardless of concurrent
        id allocation order."""
        conn = audit_setup
        with conn.cursor() as cur:
            cur.execute(generate_hash_chain_sql("auditlog"))
            for name in ("a", "b", "c", "d", "e"):
                cur.execute("INSERT INTO widgets (name) VALUES (%s)", (name,))
            cur.execute("SELECT chain_seq FROM auditlog ORDER BY id")
            seqs = [row[0] for row in cur.fetchall()]
        assert all(s is not None for s in seqs)
        assert all(b > a for a, b in zip(seqs, seqs[1:], strict=False))

    def test_chain_verifies_after_chain_seq_migration(self, audit_setup):
        """If a chain is enabled on a table that already has rows
        (which would lack chain_seq), new rows should chain to the
        legacy tail and verify_chain should still pass. Tests the
        `chain_seq IS NULL` fallback in the trigger lookup."""
        conn = audit_setup
        with conn.cursor() as cur:
            # Insert legacy rows BEFORE the chain is enabled. They get
            # NULL row_hash + NULL chain_seq.
            cur.execute("INSERT INTO widgets (name) VALUES ('legacy')")
            # Now enable the chain
            cur.execute(generate_hash_chain_sql("auditlog"))
            # Insert new rows — these get chain_seq + row_hash
            cur.execute("INSERT INTO widgets (name) VALUES ('post-chain-1')")
            cur.execute("INSERT INTO widgets (name) VALUES ('post-chain-2')")
        result = verify_chain(conn, "auditlog")
        assert result["ok"], result["broken"]

    def test_canonical_payload_resists_separator_collision(self, audit_setup):
        """Two semantically distinct rows whose concatenated text would
        collide under the old `||'|'||` payload must produce different
        hashes under the canonical jsonb encoding.

        Construction: row A has operation='UPDATE' and table_name='users'.
        Row B has operation='UPDATE|users' and table_name=''. Under
        ``operation || '|' || table_name`` both produce ``'UPDATE|users'``.
        Under ``jsonb_build_object('operation', op, 'table_name', tbl)``
        they produce different canonical JSON and therefore different hashes.
        """
        conn = audit_setup
        with conn.cursor() as cur:
            cur.execute(generate_hash_chain_sql("auditlog"))
            # Insert two rows with the colliding shape via direct INSERT
            # (we're a superuser in this test, so REVOKE doesn't apply).
            cur.execute(
                "INSERT INTO auditlog "
                "(operation, table_name, object_id) "
                "VALUES ('UPDATE', 'users', '1') RETURNING row_hash"
            )
            hash_a = cur.fetchone()[0]
            cur.execute(
                "INSERT INTO auditlog "
                "(operation, table_name, object_id) "
                "VALUES ('UPDATE|users', '', '2') RETURNING row_hash"
            )
            hash_b = cur.fetchone()[0]
        # Different rows must produce different hashes — even though their
        # naive concat would collide.
        assert hash_a != hash_b, (
            "Hash collision: canonical encoding failed to distinguish "
            "these rows. The hash chain is vulnerable to forgery."
        )


class TestRetentionRoundtrip:
    def test_purge_deletes_old_rows(self, audit_setup):
        conn = audit_setup
        with conn.cursor() as cur:
            # Insert a row and backdate it
            cur.execute("INSERT INTO widgets (name) VALUES ('old')")
            cur.execute(
                "UPDATE auditlog SET changed_at = now() - interval '100 days'"
            )
            cur.execute("INSERT INTO widgets (name) VALUES ('fresh')")

            query = generate_purge_sql("auditlog", "30 days")
            cur.execute(query)
            cur.execute("SELECT COUNT(*) FROM auditlog")
            assert cur.fetchone()[0] == 1

    def test_drop_old_partitions(self, audit_setup):
        conn = audit_setup
        with conn.cursor() as cur:
            # Create a partition explicitly in the past
            cur.execute(
                "CREATE TABLE IF NOT EXISTS auditlog_p2020_01 "
                "PARTITION OF auditlog "
                "FOR VALUES FROM ('2020-01-01') TO ('2020-02-01')"
            )
        dropped = drop_old_partitions(conn, "auditlog", "365 days")
        assert "auditlog_p2020_01" in dropped
