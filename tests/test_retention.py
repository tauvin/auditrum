from datetime import datetime

import pytest
from dateutil.relativedelta import relativedelta

from auditrum.retention import _parse_interval, generate_purge_sql


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
