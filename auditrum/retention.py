"""Retention and purge helpers for the audit log.

Two strategies are offered:

* :func:`generate_purge_sql` — simple ``DELETE`` by ``changed_at``. Fine for
  smaller tables and ad-hoc cleanups.
* :func:`drop_old_partitions` — detaches/drops month partitions whose range is
  entirely older than the cutoff. Much faster than ``DELETE`` for partitioned
  audit logs and avoids WAL churn.

Calendar handling: intervals are parsed as **calendar units** via
:class:`dateutil.relativedelta.relativedelta`. ``"6 months"`` means
six calendar months ago (e.g. April 14 → October 14), not 180 days.
``"1 year"`` correctly handles leap years. Day and week units are
unambiguous and behave the same as before.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime

from dateutil.relativedelta import relativedelta
from psycopg import sql

from auditrum.tracking.spec import validate_identifier

__all__ = [
    "drop_old_partitions",
    "generate_purge_sql",
]

_INTERVAL_RE = re.compile(
    r"^\s*(\d+)\s*(day|days|week|weeks|month|months|year|years)\s*$", re.IGNORECASE
)


def _parse_interval(expr: str) -> relativedelta:
    """Parse a human-readable interval into a calendar-aware ``relativedelta``.

    Recognises ``N day(s)``, ``N week(s)``, ``N month(s)``, ``N year(s)``.
    Raises :class:`ValueError` on anything else. Months and years are
    calendar units, not 30/365-day approximations — this matters for
    GDPR retention deadlines that often map to "delete after 2 calendar
    years from collection".
    """
    m = _INTERVAL_RE.match(expr)
    if not m:
        raise ValueError(
            f"Invalid retention interval: {expr!r}. Use e.g. '30 days', '6 months', '2 years'"
        )
    n = int(m.group(1))
    unit = m.group(2).lower()
    if unit.startswith("day"):
        return relativedelta(days=n)
    if unit.startswith("week"):
        return relativedelta(weeks=n)
    if unit.startswith("month"):
        return relativedelta(months=n)
    if unit.startswith("year"):
        return relativedelta(years=n)
    raise ValueError(
        f"Unsupported retention interval unit: {unit!r}. Supported units are "
        f"'day'/'days', 'week'/'weeks', 'month'/'months', 'year'/'years'."
    )


def _cutoff_for(expr: str) -> datetime:
    """Compute the absolute UTC cutoff timestamp for the given interval."""
    return datetime.now(UTC) - _parse_interval(expr)


def generate_purge_sql(table_name: str, older_than: str) -> sql.Composed:
    """Return a parameterized ``DELETE`` for rows older than the given interval.

    ``older_than`` is a human-readable interval like ``"30 days"``, ``"6 months"``,
    ``"2 years"``. It is parsed on the Python side via
    :func:`dateutil.relativedelta.relativedelta` and converted to an
    absolute cutoff timestamp bound as a literal, so the final query is
    fully safe and calendar-correct (six calendar months, not 180 days).
    """
    validate_identifier(table_name, "table_name")
    cutoff = _cutoff_for(older_than)
    return sql.SQL("DELETE FROM {table} WHERE changed_at < {cutoff}").format(
        table=sql.Identifier(table_name),
        cutoff=sql.Literal(cutoff),
    )


def drop_old_partitions(conn, table_name: str, older_than: str) -> list[str]:
    """Drop month partitions whose upper bound is older than the cutoff.

    Returns the list of dropped partition names. Connects via the supplied
    connection (which must have sufficient privileges) and inspects
    ``pg_inherits`` to find child partitions of ``table_name``; each child's
    partition bound is parsed and compared to the cutoff.
    """
    validate_identifier(table_name, "table_name")
    cutoff = _cutoff_for(older_than)

    dropped: list[str] = []
    bound_pattern = re.compile(
        r"FOR VALUES FROM \('([^']+)'\) TO \('([^']+)'\)", re.IGNORECASE
    )

    with conn.cursor() as cur:
        cur.execute(
            sql.SQL(
                "SELECT c.relname, pg_get_expr(c.relpartbound, c.oid) "
                "FROM pg_inherits i "
                "JOIN pg_class c ON c.oid = i.inhrelid "
                "WHERE i.inhparent = {}::regclass"
            ).format(sql.Literal(table_name))
        )
        rows = cur.fetchall()

        for name, bound_expr in rows:
            if bound_expr is None or "DEFAULT" in bound_expr.upper():
                continue
            m = bound_pattern.search(bound_expr)
            if not m:
                continue
            upper = datetime.fromisoformat(m.group(2)).replace(tzinfo=UTC)
            if upper <= cutoff:
                cur.execute(sql.SQL("DROP TABLE IF EXISTS {}").format(sql.Identifier(name)))
                dropped.append(name)

    return dropped
