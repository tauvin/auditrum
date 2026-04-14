from unittest.mock import MagicMock

import pytest

from auditrum.hash_chain import generate_hash_chain_sql, get_chain_tip, verify_chain


class TestGenerateHashChainSql:
    def test_adds_columns_and_trigger(self):
        sql = generate_hash_chain_sql("auditlog")
        assert "ADD COLUMN IF NOT EXISTS row_hash" in sql
        assert "ADD COLUMN IF NOT EXISTS prev_hash" in sql
        assert "CREATE EXTENSION IF NOT EXISTS pgcrypto" in sql
        assert "pg_advisory_xact_lock" in sql
        assert "BEFORE INSERT ON auditlog" in sql

    def test_function_uses_sha256(self):
        sql = generate_hash_chain_sql("auditlog")
        assert "'sha256'" in sql

    def test_custom_table(self):
        sql = generate_hash_chain_sql("events_log")
        assert "events_log_hash_chain" in sql
        assert "ON events_log" in sql

    def test_rejects_injection(self):
        with pytest.raises(ValueError, match="Invalid table_name"):
            generate_hash_chain_sql("auditlog; DROP")

    def test_canonical_payload_via_jsonb(self):
        """The payload must be a structured jsonb object, not a separator-
        joined concatenation. Concatenation allowed collision attacks where
        a forged row's field could contain the separator."""
        sql = generate_hash_chain_sql("auditlog")
        assert "jsonb_build_object" in sql
        # Old vulnerable form: "|| '|' ||" — must not be there
        assert "|| '|' ||" not in sql
        # All seven fields must appear in the canonical object
        for field in (
            "'id', NEW.id",
            "'changed_at', NEW.changed_at",
            "'operation', NEW.operation",
            "'table_name', NEW.table_name",
            "'old_data', NEW.old_data",
            "'new_data', NEW.new_data",
            "'prev_hash', last_hash",
        ):
            assert field in sql, f"missing field binding: {field}"

    def test_function_is_security_definer(self):
        sql = generate_hash_chain_sql("auditlog")
        assert "SECURITY DEFINER" in sql
        assert "SET search_path = pg_catalog, public" in sql

    def test_adds_chain_seq_column_and_sequence(self):
        """chain_seq is assigned inside the advisory lock to guarantee
        chain order matches lock-acquisition order, not racy id order."""
        sql = generate_hash_chain_sql("auditlog")
        assert "ADD COLUMN IF NOT EXISTS chain_seq bigint" in sql
        assert "CREATE SEQUENCE IF NOT EXISTS auditlog_chain_seq" in sql
        assert "NEW.chain_seq := nextval('auditlog_chain_seq')" in sql

    def test_lookup_uses_chain_seq_ordering(self):
        sql = generate_hash_chain_sql("auditlog")
        assert "ORDER BY chain_seq NULLS FIRST, id DESC" in sql

    def test_advisory_lock_uses_64_bit_hashtext(self):
        sql = generate_hash_chain_sql("auditlog")
        assert "hashtextextended('auditlog', 0)" in sql

    def test_chain_seq_assigned_after_lock(self):
        """Order matters: lock first, THEN nextval. Reversing that would
        let two transactions grab chain_seq in id-grab order rather than
        lock-acquisition order, defeating the whole point of the fix."""
        sql = generate_hash_chain_sql("auditlog")
        lock_pos = sql.find("pg_advisory_xact_lock")
        nextval_pos = sql.find("nextval('auditlog_chain_seq')")
        assert lock_pos > 0
        assert nextval_pos > 0
        assert lock_pos < nextval_pos


class TestGetChainTip:
    def test_returns_dict_with_expected_fields(self):
        cur = MagicMock()
        cur.fetchone.return_value = (42, 7, "abc123", "2024-01-01")
        cur.__enter__ = MagicMock(return_value=cur)
        cur.__exit__ = MagicMock(return_value=False)
        conn = MagicMock()
        conn.cursor.return_value = cur

        tip = get_chain_tip(conn, "auditlog")
        assert tip == {
            "id": 42,
            "chain_seq": 7,
            "row_hash": "abc123",
            "changed_at": "2024-01-01",
        }

    def test_empty_chain_returns_none_fields(self):
        cur = MagicMock()
        cur.fetchone.return_value = None
        cur.__enter__ = MagicMock(return_value=cur)
        cur.__exit__ = MagicMock(return_value=False)
        conn = MagicMock()
        conn.cursor.return_value = cur

        tip = get_chain_tip(conn, "auditlog")
        assert tip == {
            "id": None,
            "chain_seq": None,
            "row_hash": None,
            "changed_at": None,
        }

    def test_rejects_injection_in_table(self):
        with pytest.raises(ValueError, match="Invalid table_name"):
            get_chain_tip(MagicMock(), "auditlog; DROP")


class TestVerifyChainAcceptsTipParam:
    def test_signature_accepts_expected_tip(self):
        """Smoke-check the kwarg exists. Real verification logic is in
        the integration tests since it requires a live PG."""
        cur = MagicMock()
        cur.fetchall.return_value = []
        cur.fetchone.return_value = (0,)
        cur.__enter__ = MagicMock(return_value=cur)
        cur.__exit__ = MagicMock(return_value=False)
        cur.__iter__ = MagicMock(return_value=iter([]))
        conn = MagicMock()
        conn.cursor.return_value = cur

        # Just verify the function accepts the kwarg without crashing
        result = verify_chain(
            conn, "auditlog", expected_tip={"id": 1, "row_hash": "x"}
        )
        assert "checked" in result
        assert "ok" in result
        assert "broken" in result

    def test_rejects_injection_in_table(self):
        with pytest.raises(ValueError, match="Invalid table_name"):
            verify_chain(MagicMock(), "auditlog; DROP")
