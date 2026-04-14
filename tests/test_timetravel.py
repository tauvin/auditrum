"""Unit tests for auditrum.timetravel — SQL shape + result parsing with a
fake cursor. Real-DB behaviour is covered in integration tests.
"""

from datetime import UTC, datetime
from unittest.mock import MagicMock

import pytest

from auditrum.timetravel import (
    reconstruct_field_history,
    reconstruct_row,
    reconstruct_table,
)


def _cursor(rows):
    cur = MagicMock()
    cur.fetchall.return_value = list(rows)
    cur.fetchone.return_value = rows[0] if rows else None
    cur.__enter__ = MagicMock(return_value=cur)
    cur.__exit__ = MagicMock(return_value=False)
    return cur


def _conn(cursor):
    conn = MagicMock()
    conn.cursor.return_value = cursor
    return conn


class TestReconstructRow:
    def test_returns_none_when_sql_returns_none(self):
        conn = _conn(_cursor([(None,)]))
        result = reconstruct_row(
            conn, table="users", object_id="42", at=datetime(2024, 1, 1, tzinfo=UTC)
        )
        assert result is None

    def test_returns_dict_when_row_found(self):
        payload = {"id": 42, "name": "alice", "email": "a@x.com"}
        conn = _conn(_cursor([(payload,)]))
        result = reconstruct_row(
            conn, table="users", object_id="42", at=datetime(2024, 1, 1, tzinfo=UTC)
        )
        assert result == payload

    def test_json_string_is_decoded(self):
        import json

        payload = {"id": 42, "name": "alice"}
        conn = _conn(_cursor([(json.dumps(payload),)]))
        result = reconstruct_row(
            conn, table="users", object_id="42", at=datetime(2024, 1, 1, tzinfo=UTC)
        )
        assert result == payload

    def test_calls_sql_helper(self):
        cur = _cursor([(None,)])
        conn = _conn(cur)
        ts = datetime(2024, 6, 12, 14, 23, 0, tzinfo=UTC)
        reconstruct_row(conn, table="users", object_id="42", at=ts)
        sql, params = cur.execute.call_args[0]
        assert "_audit_reconstruct_row" in sql
        assert params == ("users", "42", ts)

    def test_rejects_injection_in_audit_table(self):
        conn = _conn(_cursor([]))
        with pytest.raises(ValueError, match="Invalid audit_table"):
            reconstruct_row(
                conn,
                table="users",
                object_id="42",
                at=datetime(2024, 1, 1, tzinfo=UTC),
                audit_table="bad; DROP",
            )


class TestReconstructTable:
    def test_yields_tuples(self):
        rows = [
            ("1", {"id": 1, "name": "alice"}),
            ("2", {"id": 2, "name": "bob"}),
        ]
        conn = _conn(_cursor(rows))
        result = list(
            reconstruct_table(
                conn, table="users", at=datetime(2024, 1, 1, tzinfo=UTC)
            )
        )
        assert result == rows

    def test_empty_table(self):
        conn = _conn(_cursor([]))
        result = list(
            reconstruct_table(
                conn, table="users", at=datetime(2024, 1, 1, tzinfo=UTC)
            )
        )
        assert result == []

    def test_calls_set_returning_function(self):
        cur = _cursor([])
        conn = _conn(cur)
        ts = datetime(2024, 6, 12, tzinfo=UTC)
        list(reconstruct_table(conn, table="users", at=ts))
        sql, params = cur.execute.call_args[0]
        assert "_audit_reconstruct_table" in sql
        assert params == ("users", ts)


class TestReconstructFieldHistory:
    def test_builds_timeline_from_insert_and_updates(self):
        insert_ts = datetime(2024, 1, 1, tzinfo=UTC)
        update_ts = datetime(2024, 2, 1, tzinfo=UTC)
        rows = [
            (insert_ts, "INSERT", None, {"name": "alice", "email": "a@x.com"}, None),
            (
                update_ts,
                "UPDATE",
                {"email": "a@x.com"},
                {"email": "a2@x.com"},
                {"email": "a2@x.com"},
            ),
        ]
        conn = _conn(_cursor(rows))
        history = reconstruct_field_history(
            conn, table="users", object_id="42", field="email"
        )
        assert history == [
            (insert_ts, "a@x.com"),
            (update_ts, "a2@x.com"),
        ]

    def test_skips_updates_that_did_not_touch_field(self):
        insert_ts = datetime(2024, 1, 1, tzinfo=UTC)
        unrelated_update_ts = datetime(2024, 2, 1, tzinfo=UTC)
        related_update_ts = datetime(2024, 3, 1, tzinfo=UTC)
        rows = [
            (insert_ts, "INSERT", None, {"email": "a@x.com", "name": "alice"}, None),
            (
                unrelated_update_ts,
                "UPDATE",
                {"name": "alice"},
                {"name": "alice2"},
                {"name": "alice2"},
            ),
            (
                related_update_ts,
                "UPDATE",
                {"email": "a@x.com"},
                {"email": "a2@x.com"},
                {"email": "a2@x.com"},
            ),
        ]
        conn = _conn(_cursor(rows))
        history = reconstruct_field_history(
            conn, table="users", object_id="42", field="email"
        )
        assert len(history) == 2
        assert history[0][1] == "a@x.com"
        assert history[1][1] == "a2@x.com"

    def test_delete_appends_none(self):
        insert_ts = datetime(2024, 1, 1, tzinfo=UTC)
        delete_ts = datetime(2024, 6, 1, tzinfo=UTC)
        rows = [
            (insert_ts, "INSERT", None, {"email": "a@x.com"}, None),
            (delete_ts, "DELETE", {"email": "a@x.com"}, None, None),
        ]
        conn = _conn(_cursor(rows))
        history = reconstruct_field_history(
            conn, table="users", object_id="42", field="email"
        )
        assert history[-1] == (delete_ts, None)

    def test_rejects_injection(self):
        conn = _conn(_cursor([]))
        with pytest.raises(ValueError, match="Invalid"):
            reconstruct_field_history(
                conn, table="users", object_id="42", field="email; DROP"
            )
