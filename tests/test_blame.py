"""Unit tests for auditrum.blame — format_blame against synthesised BlameEntry
lists, and fetch_blame SQL shape with a fake cursor.
"""

import json
from datetime import UTC, datetime
from unittest.mock import MagicMock

import pytest

from auditrum.blame import BlameEntry, fetch_blame, format_blame


def _entry(**overrides):
    defaults = {
        "changed_at": datetime(2024, 6, 12, 14, 23, 0, tzinfo=UTC),
        "operation": "UPDATE",
        "user_id": 7,
        "context_id": "abc12345-0000-0000-0000-000000000000",
        "context_metadata": {"username": "alice", "source": "web"},
        "old_value": {"email": "a@x.com"},
        "new_value": {"email": "a2@x.com"},
        "change_reason": None,
        "diff": {"email": "a2@x.com"},
    }
    defaults.update(overrides)
    return BlameEntry(**defaults)


class TestFormatBlameText:
    def test_empty(self):
        out = format_blame([], fmt="text", table="users", object_id="42")
        assert "no events" in out

    def test_header_rendered(self):
        out = format_blame([], fmt="text", table="users", object_id="42")
        assert "Audit history for users:42" in out

    def test_header_with_field(self):
        out = format_blame([], fmt="text", field="email", table="users", object_id="42")
        assert "field: email" in out

    def test_single_update(self):
        e = _entry(
            operation="UPDATE",
            diff={"email": "a2@x.com"},
            old_value={"email": "a@x.com"},
            new_value={"email": "a2@x.com"},
        )
        out = format_blame([e], fmt="text", table="users", object_id="42")
        assert "UPDATE" in out
        assert "changed: email" in out
        assert "alice" in out or "user=7" in out

    def test_insert(self):
        e = _entry(
            operation="INSERT",
            old_value=None,
            new_value={"name": "alice", "email": "a@x.com"},
            diff=None,
        )
        out = format_blame([e], fmt="text", table="users", object_id="42")
        assert "INSERT" in out
        assert "inserted" in out
        assert "2 fields" in out

    def test_delete(self):
        e = _entry(
            operation="DELETE",
            old_value={"name": "alice", "email": "a@x.com"},
            new_value=None,
            diff=None,
        )
        out = format_blame([e], fmt="text", table="users", object_id="42")
        assert "DELETE" in out
        assert "deleted" in out

    def test_change_reason_printed(self):
        e = _entry(change_reason="compliance cleanup")
        out = format_blame([e], fmt="text", table="users", object_id="42")
        assert "compliance cleanup" in out

    def test_context_id_truncated(self):
        e = _entry(context_id="abcdefgh-1234-5678-90ab-cdef12345678")
        out = format_blame([e], fmt="text", table="users", object_id="42")
        assert "abcdefgh" in out

    def test_anonymous_actor_when_no_user(self):
        e = _entry(user_id=None, context_metadata=None)
        out = format_blame([e], fmt="text", table="users", object_id="42")
        assert "system" in out


class TestFormatBlameFieldFilter:
    def test_field_insert_shows_arrow_from_nothing(self):
        e = _entry(
            operation="INSERT",
            old_value=None,
            new_value="a@x.com",
            diff=None,
        )
        out = format_blame(
            [e], fmt="text", field="email", table="users", object_id="42"
        )
        assert "a@x.com" in out
        assert "→" in out

    def test_field_update_shows_old_to_new(self):
        e = _entry(
            operation="UPDATE",
            old_value="a@x.com",
            new_value="a2@x.com",
            diff={"email": "a2@x.com"},
        )
        out = format_blame(
            [e], fmt="text", field="email", table="users", object_id="42"
        )
        assert "a@x.com" in out
        assert "a2@x.com" in out
        assert "→" in out

    def test_field_delete_shows_to_void(self):
        e = _entry(
            operation="DELETE",
            old_value="a@x.com",
            new_value=None,
        )
        out = format_blame(
            [e], fmt="text", field="email", table="users", object_id="42"
        )
        assert "a@x.com" in out
        assert "∅" in out

    def test_truncates_long_values(self):
        long_val = "x" * 100
        e = _entry(operation="INSERT", old_value=None, new_value=long_val, diff=None)
        out = format_blame(
            [e], fmt="text", field="email", table="users", object_id="42"
        )
        assert "..." in out


class TestFormatBlameJson:
    def test_json_round_trip(self):
        e = _entry()
        out = format_blame([e], fmt="json")
        parsed = json.loads(out)
        assert len(parsed) == 1
        assert parsed[0]["operation"] == "UPDATE"
        assert parsed[0]["user_id"] == 7
        assert parsed[0]["diff"] == {"email": "a2@x.com"}


class TestFormatBlameRich:
    def test_rich_contains_color_markup(self):
        e = _entry()
        out = format_blame([e], fmt="rich", table="users", object_id="42")
        assert "[yellow]" in out  # UPDATE op color
        assert "[/yellow]" in out

    def test_rich_escapes_malicious_username(self):
        """An attacker-controlled username with rich markup must be
        escaped so it can't spoof colored terminal output."""
        e = _entry(
            context_metadata={
                "username": "[red]VICTIM_FAKE[/red]",
                "source": "web",
            }
        )
        out = format_blame([e], fmt="rich", table="users", object_id="42")
        # Backslash-escape neutralises the bracket so rich renders it literal
        assert "\\[red]VICTIM_FAKE" in out
        # Sanity: the escape is only on user-controlled content, not our own
        assert "[yellow]" in out

    def test_rich_escapes_malicious_change_reason(self):
        e = _entry(change_reason="[bold red]URGENT FAKE[/bold red]")
        out = format_blame([e], fmt="rich", table="users", object_id="42")
        assert "\\[bold red]URGENT FAKE" in out

    def test_text_mode_does_not_escape(self):
        """Plain text mode passes user content through verbatim."""
        e = _entry(context_metadata={"username": "[red]name[/red]", "source": "x"})
        out = format_blame([e], fmt="text", table="users", object_id="42")
        # Text mode preserves brackets as-is (no markup parser to attack)
        assert "[red]name[/red]" in out
        assert "\\[" not in out


class TestFetchBlameSql:
    def test_issues_parametrised_query(self):
        """fetch_blame must use psycopg.sql.Identifier for table names and
        bind table+object_id+limit as parameters."""
        cursor = MagicMock()
        cursor.fetchall.return_value = []
        cursor.__enter__ = MagicMock(return_value=cursor)
        cursor.__exit__ = MagicMock(return_value=False)
        conn = MagicMock()
        conn.cursor.return_value = cursor

        fetch_blame(conn, table="users", object_id="42", limit=10)

        cursor.execute.assert_called_once()
        query_arg = cursor.execute.call_args[0][0]
        params = cursor.execute.call_args[0][1]
        # psycopg.sql.Composed stringifies to a SQL fragment with quoted identifiers
        rendered = str(query_arg)
        assert "users" not in rendered  # table is a parameter, not identifier
        assert "auditlog" in rendered  # audit_table is an identifier
        assert "audit_context" in rendered
        assert params == ("users", "42", 10)

    def test_rejects_injection_in_audit_table(self):
        conn = MagicMock()
        with pytest.raises(ValueError, match="Invalid audit_table"):
            fetch_blame(conn, table="users", object_id="42", audit_table="bad; DROP")

    def test_builds_entries_from_rows(self):
        cursor = MagicMock()
        cursor.fetchall.return_value = [
            (
                datetime(2024, 6, 12, 14, 23, 0, tzinfo=UTC),
                "UPDATE",
                7,
                "abc12345-0000-0000-0000-000000000000",
                {"email": "a@x.com"},
                {"email": "a2@x.com"},
                {"email": "a2@x.com"},
                {"username": "alice", "change_reason": "fix"},
            ),
        ]
        cursor.__enter__ = MagicMock(return_value=cursor)
        cursor.__exit__ = MagicMock(return_value=False)
        conn = MagicMock()
        conn.cursor.return_value = cursor

        entries = fetch_blame(conn, table="users", object_id="42")
        assert len(entries) == 1
        assert entries[0].operation == "UPDATE"
        assert entries[0].user_id == 7
        assert entries[0].change_reason == "fix"
        assert entries[0].diff == {"email": "a2@x.com"}

    def test_field_filter_narrows_results(self):
        cursor = MagicMock()
        cursor.fetchall.return_value = [
            (
                datetime(2024, 1, 1, tzinfo=UTC),
                "UPDATE",
                1,
                None,
                {"email": "a@x.com", "name": "alice"},
                {"email": "a@x.com", "name": "alice2"},
                {"name": "alice2"},
                None,
            ),
        ]
        cursor.__enter__ = MagicMock(return_value=cursor)
        cursor.__exit__ = MagicMock(return_value=False)
        conn = MagicMock()
        conn.cursor.return_value = cursor

        # email was NOT in diff, so field='email' should produce zero entries
        entries = fetch_blame(conn, table="users", object_id="42", field="email")
        assert len(entries) == 0

        # resetting mock and asking for 'name'
        cursor.fetchall.return_value = [
            (
                datetime(2024, 1, 1, tzinfo=UTC),
                "UPDATE",
                1,
                None,
                {"email": "a@x.com", "name": "alice"},
                {"email": "a@x.com", "name": "alice2"},
                {"name": "alice2"},
                None,
            ),
        ]
        entries = fetch_blame(conn, table="users", object_id="42", field="name")
        assert len(entries) == 1
        assert entries[0].old_value == "alice"
        assert entries[0].new_value == "alice2"

    def test_insert_with_field_filter(self):
        cursor = MagicMock()
        cursor.fetchall.return_value = [
            (
                datetime(2024, 1, 1, tzinfo=UTC),
                "INSERT",
                None,
                None,
                None,
                {"email": "a@x.com", "name": "alice"},
                None,
                None,
            ),
        ]
        cursor.__enter__ = MagicMock(return_value=cursor)
        cursor.__exit__ = MagicMock(return_value=False)
        conn = MagicMock()
        conn.cursor.return_value = cursor

        entries = fetch_blame(conn, table="users", object_id="42", field="email")
        assert len(entries) == 1
        assert entries[0].old_value is None
        assert entries[0].new_value == "a@x.com"
