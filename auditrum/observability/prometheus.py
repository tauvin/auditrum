"""Prometheus collector for audit log event counts.

Users register the collector with ``prometheus_client.REGISTRY`` and
scrape metrics like any other exporter. On each scrape we issue a
lightweight ``GROUP BY table_name, operation`` query against the audit
log over a configurable window (default 60s) and expose the result as
labelled gauges.

Example::

    import psycopg
    from prometheus_client import REGISTRY, start_http_server
    from auditrum.observability.prometheus import AuditrumCollector

    def _conn_factory():
        return psycopg.connect("postgresql://…")

    REGISTRY.register(AuditrumCollector(_conn_factory, window_seconds=60))
    start_http_server(9090)

Safe defaults: if ``prometheus_client`` is not installed, instantiation
raises a clear :class:`ImportError`; at scrape time, a DB error yields
an empty scrape rather than a crash so one bad query can't take down
the metrics endpoint.
"""

from __future__ import annotations

import contextlib
from collections.abc import Callable, Iterator
from typing import Any

from psycopg import sql as pg_sql

from auditrum.tracking.spec import validate_identifier


class AuditrumCollector:
    """prometheus_client collector exposing audit event counts.

    Args:
        conn_factory: zero-arg callable returning a psycopg-compatible
            connection. The collector closes the connection after each
            scrape unless the callable manages lifetime itself.
        window_seconds: size of the lookback window for the event counts.
        audit_table: audit log table name (identifier-validated).
    """

    def __init__(
        self,
        conn_factory: Callable[[], Any],
        *,
        window_seconds: int = 60,
        audit_table: str = "auditlog",
    ) -> None:
        try:
            import prometheus_client  # noqa: F401
        except ImportError as exc:
            raise ImportError(
                "prometheus_client is required for AuditrumCollector. "
                "Install with: pip install auditrum[observability]"
            ) from exc

        validate_identifier(audit_table, "audit_table")
        self._conn_factory = conn_factory
        self._window_seconds = int(window_seconds)
        self._audit_table = audit_table

    def collect(self) -> Iterator[Any]:
        from prometheus_client.core import GaugeMetricFamily

        gauge = GaugeMetricFamily(
            "auditrum_events",
            f"Audit events in the last {self._window_seconds}s window",
            labels=["table", "operation"],
        )

        query = pg_sql.SQL(
            "SELECT table_name, operation, COUNT(*) "
            "FROM {audit_table} "
            "WHERE changed_at > now() - make_interval(secs => %s) "
            "GROUP BY table_name, operation"
        ).format(audit_table=pg_sql.Identifier(self._audit_table))

        try:
            conn = self._conn_factory()
        except Exception:
            yield gauge
            return

        try:
            with conn.cursor() as cur:
                cur.execute(query, (self._window_seconds,))
                for table, op, count in cur.fetchall():
                    gauge.add_metric([table or "", op or ""], float(count))
        except Exception:
            # Metrics endpoint never crashes on DB errors
            pass
        finally:
            with contextlib.suppress(Exception):
                conn.close()

        yield gauge
