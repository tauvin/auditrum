from datetime import UTC, datetime, timedelta, timezone

import pytest
from dateutil.relativedelta import relativedelta

import auditrum.retention as retention
from auditrum.retention import _parse_interval, drop_old_partitions, generate_purge_sql


class _FakeCursor:
    def __init__(self, rows):
        self._rows = rows
        self.dropped: list[str] = []

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, query, params=None):
        rendered = query.as_string(None) if hasattr(query, "as_string") else str(query)
        if "DROP TABLE" in rendered:
            self.dropped.append(rendered)

    def fetchall(self):
        return self._rows


class _FakeConn:
    def __init__(self, rows):
        self._cursor = _FakeCursor(rows)

    def cursor(self):
        return self._cursor


class TestParseInterval:
    @pytest.mark.parametrize(
        "expr,expected",
        [
            ("30 days", relativedelta(days=30)),
            ("1 day", relativedelta(days=1)),
            ("2 weeks", relativedelta(weeks=2)),
            ("6 months", relativedelta(months=6)),
            ("1 year", relativedelta(years=1)),
            ("2 years", relativedelta(years=2)),
        ],
    )
    def test_valid(self, expr, expected):
        assert _parse_interval(expr) == expected

    def test_calendar_aware_months(self):
        """Six months means six calendar months, not 180 days. This
        matters for GDPR retention deadlines."""
        delta = _parse_interval("6 months")
        # Apply to a fixed date to verify calendar semantics
        anchor = datetime(2025, 4, 14)
        result = anchor - delta
        # April 14 minus six months → October 14 of previous year
        assert result.month == 10
        assert result.day == 14
        assert result.year == 2024

    def test_calendar_aware_years_handles_leap(self):
        """One year before March 1, 2025 is March 1, 2024 (a leap year),
        not 365 days earlier."""
        delta = _parse_interval("1 year")
        anchor = datetime(2025, 3, 1)
        result = anchor - delta
        assert result == datetime(2024, 3, 1)

    @pytest.mark.parametrize("bad", ["", "forever", "30", "30 fortnights", "days"])
    def test_invalid(self, bad):
        with pytest.raises(ValueError):
            _parse_interval(bad)


class TestGeneratePurgeSql:
    def test_builds_parameterised_delete(self):
        query = generate_purge_sql("auditlog", "30 days")
        rendered = query.as_string(None)
        assert 'DELETE FROM "auditlog"' in rendered
        assert "changed_at" in rendered

    def test_cutoff_is_in_the_past(self):
        query = generate_purge_sql("auditlog", "30 days")
        rendered = query.as_string(None)
        assert "WHERE changed_at" in rendered

    def test_rejects_injection_in_table(self):
        with pytest.raises(ValueError, match="Invalid table_name"):
            generate_purge_sql("auditlog; DROP", "30 days")

    def test_rejects_invalid_interval(self):
        with pytest.raises(ValueError):
            generate_purge_sql("auditlog", "forever")


class TestDropOldPartitionsTimezone:
    """The partition upper-bound literal may carry a non-UTC offset when the
    server's ``timezone`` setting is not UTC. ``drop_old_partitions`` must
    honour that offset instead of stamping the bound with UTC, otherwise a
    partition can be dropped hours early."""

    def test_negative_offset_bound_not_dropped_early(self, monkeypatch):
        # Bound = 2020-01-01 00:00:00-05:00 == 2020-01-01 05:00:00 UTC.
        # cutoff sits between the (buggy) UTC-stamped instant and the real
        # offset-aware instant: with the offset preserved upper > cutoff so
        # the partition is KEPT; the old ``.replace(tzinfo=UTC)`` would have
        # made upper == cutoff and dropped it.
        cutoff = datetime(2020, 1, 1, 2, 0, 0, tzinfo=UTC)
        monkeypatch.setattr(retention, "_cutoff_for", lambda _expr: cutoff)
        conn = _FakeConn(
            [
                (
                    "auditlog_2019_12",
                    "FOR VALUES FROM ('2019-01-01-05:00') TO ('2020-01-01 00:00:00-05:00')",
                )
            ]
        )

        dropped = drop_old_partitions(conn, "auditlog", "ignored")

        assert dropped == []

    def test_offset_aware_bound_dropped_when_genuinely_old(self, monkeypatch):
        # Same bound, but cutoff is now after the real (offset-aware) instant
        # 2020-01-01 05:00:00 UTC, so the partition is genuinely expired.
        cutoff = datetime(2020, 1, 1, 6, 0, 0, tzinfo=UTC)
        monkeypatch.setattr(retention, "_cutoff_for", lambda _expr: cutoff)
        conn = _FakeConn(
            [
                (
                    "auditlog_2019_12",
                    "FOR VALUES FROM ('2019-01-01-05:00') TO ('2020-01-01 00:00:00-05:00')",
                )
            ]
        )

        dropped = drop_old_partitions(conn, "auditlog", "ignored")

        assert dropped == ["auditlog_2019_12"]

    def test_naive_bound_defaults_to_utc(self, monkeypatch):
        # No offset in the literal → treated as UTC (unchanged behaviour).
        cutoff = datetime(2020, 2, 1, 0, 0, 0, tzinfo=UTC)
        monkeypatch.setattr(retention, "_cutoff_for", lambda _expr: cutoff)
        conn = _FakeConn([("auditlog_2020_01", "FOR VALUES FROM ('2020-01-01') TO ('2020-02-01')")])

        dropped = drop_old_partitions(conn, "auditlog", "ignored")

        assert dropped == ["auditlog_2020_01"]

    def test_positive_offset_bound_preserved(self, monkeypatch):
        # +09:00 bound: 2020-01-01 00:00:00+09:00 == 2019-12-31 15:00:00 UTC.
        # Buggy replace() would have made it 2020-01-01 00:00:00 UTC (9h
        # later) and KEPT a partition that is actually expired.
        cutoff = datetime(2019, 12, 31, 18, 0, 0, tzinfo=timezone(timedelta(hours=0)))
        monkeypatch.setattr(retention, "_cutoff_for", lambda _expr: cutoff)
        conn = _FakeConn(
            [
                (
                    "auditlog_2019_12",
                    "FOR VALUES FROM ('2019-01-01+09:00') TO ('2020-01-01 00:00:00+09:00')",
                )
            ]
        )

        dropped = drop_old_partitions(conn, "auditlog", "ignored")

        assert dropped == ["auditlog_2019_12"]
