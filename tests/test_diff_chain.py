"""Unit tests for auditrum.diff_chain — argument checks, parsing and row decoding."""

from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock

import pytest

from auditrum.diff_chain import DiffChainMismatch, parse_since, verify_diff_chain


def _conn(fetchone=None, fetchall=()):
    cur = MagicMock()
    cur.fetchone.return_value = fetchone
    cur.fetchall.return_value = list(fetchall)
    cur.__enter__ = MagicMock(return_value=cur)
    cur.__exit__ = MagicMock(return_value=False)
    conn = MagicMock()
    conn.cursor.return_value = cur
    return conn, cur


class TestArguments:
    def test_requires_a_scope(self):
        conn, _ = _conn()
        with pytest.raises(ValueError, match="since, object_id or full"):
            verify_diff_chain(conn, table="items")

    def test_rejects_bad_identifiers(self):
        conn, _ = _conn()
        with pytest.raises(ValueError, match="Invalid table"):
            verify_diff_chain(conn, table="items; DROP", full=True)
        with pytest.raises(ValueError, match="Invalid audit_table"):
            verify_diff_chain(conn, table="items", full=True, audit_table="bad log")

    def test_table_missing_from_registry_raises(self):
        conn, _ = _conn(fetchone=(None,))
        with pytest.raises(ValueError, match="items.*auditrum_applied_triggers"):
            verify_diff_chain(conn, table="items", full=True)

    def test_log_condition_table_is_refused(self):
        conn, cur = _conn(fetchone=({"fields_kind": "all", "log_condition": "NEW.n > 0"},))
        with pytest.raises(ValueError, match="items.*log_condition.*gaps by design"):
            verify_diff_chain(conn, table="items", full=True)
        assert cur.execute.call_count == 1

    def test_spec_as_json_text_is_decoded(self):
        # Django's connection hands jsonb over as str, not dict.
        conn, _ = _conn(
            fetchone=('{"fields_kind": "all", "log_condition": null}',),
            fetchall=[("1", 7, "live", "name", '"a"', '"X"', "live value of name differs")],
        )
        [finding] = verify_diff_chain(conn, table="items", full=True)
        assert finding.kind == "live" and finding.actual == "X"

    def test_log_condition_in_json_text_spec_is_refused(self):
        conn, _ = _conn(fetchone=('{"fields_kind": "all", "log_condition": "NEW.n > 0"}',))
        with pytest.raises(ValueError, match="items.*log_condition"):
            verify_diff_chain(conn, table="items", full=True)


class TestQueryShape:
    def _query(self, **kwargs):
        conn, cur = _conn(fetchone=({"fields_kind": "all"},))
        verify_diff_chain(conn, table="items", full=True, **kwargs)
        query, params = cur.execute.call_args.args
        return query.as_string(None), params

    def test_default_reads_the_diff_only(self):
        query, params = self._query()
        assert "old_data" not in query and "_audit_filter_row" not in query
        assert "object_ids" not in params

    def test_snapshots_read_old_and_new_data(self):
        query, _ = self._query(snapshots=True)
        assert "a.old_data" in query and "_audit_filter_row" in query

    def test_object_ids_narrow_every_read_of_the_audit_table(self):
        query, params = self._query(snapshots=True, _object_ids=["1", "2"])
        assert params["object_ids"] == ["1", "2"]
        assert query.count("a.object_id = ANY(%(object_ids)s::text[])") == 2


class TestDecoding:
    def test_string_jsonb_values_are_decoded(self):
        conn, cur = _conn(
            fetchone=({"fields_kind": "all"},),
            fetchall=[
                ("1", 7, "chain", "name", '"a"', '"X"', "old value of name differs from event 5")
            ],
        )
        [finding] = verify_diff_chain(conn, table="items", full=True)
        assert finding == DiffChainMismatch(
            table="items",
            object_id="1",
            audit_id=7,
            kind="chain",
            column="name",
            expected="a",
            actual="X",
            detail="old value of name differs from event 5",
        )


class TestParseSince:
    def test_interval(self):
        before = datetime.now(UTC)
        value = parse_since("2 days")
        assert (
            before - timedelta(days=2, seconds=5)
            < value
            < before - timedelta(days=2) + timedelta(seconds=5)
        )

    def test_iso_with_offset(self):
        assert parse_since("2026-10-01T12:00:00+02:00") == datetime(2026, 10, 1, 10, 0, tzinfo=UTC)

    def test_naive_iso_is_utc(self):
        assert parse_since("2026-10-01T12:00:00") == datetime(2026, 10, 1, 12, 0, tzinfo=UTC)

    def test_garbage(self):
        with pytest.raises(ValueError, match="invalid --since value 'yesterday-ish'.*ISO-8601"):
            parse_since("yesterday-ish")

    def test_option_names_the_option_in_the_error(self):
        with pytest.raises(ValueError, match="invalid --older-than value 'soon'"):
            parse_since("soon", option="--older-than")
