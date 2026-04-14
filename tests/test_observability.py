"""Unit tests for auditrum.observability — OTel, Prometheus, Sentry.

All three libraries are soft deps: the helpers must gracefully no-op
when they're not installed, and must correctly pick up state when they
are. We mock the modules via ``sys.modules`` injection where needed.
"""

import sys
from types import ModuleType
from unittest.mock import MagicMock

import pytest

from auditrum.observability.otel import enrich_metadata
from auditrum.observability.sentry import add_breadcrumb_for_context


class TestOtelEnrichment:
    def test_noop_without_opentelemetry(self, monkeypatch):
        monkeypatch.setitem(sys.modules, "opentelemetry", None)
        meta: dict = {"source": "http"}
        result = enrich_metadata(meta)
        assert result is meta
        assert "trace_id" not in meta

    def test_enriches_from_active_span(self, monkeypatch):
        # Build a fake opentelemetry.trace module
        fake_trace = ModuleType("opentelemetry.trace")

        class FakeCtx:
            is_valid = True
            trace_id = 0xAABBCCDDEEFF00112233445566778899
            span_id = 0x1122334455667788

        fake_span = MagicMock()
        fake_span.get_span_context.return_value = FakeCtx()
        fake_trace.get_current_span = lambda: fake_span

        fake_otel = ModuleType("opentelemetry")
        fake_otel.trace = fake_trace
        monkeypatch.setitem(sys.modules, "opentelemetry", fake_otel)
        monkeypatch.setitem(sys.modules, "opentelemetry.trace", fake_trace)

        meta: dict = {}
        enrich_metadata(meta)
        assert meta["trace_id"] == "aabbccddeeff00112233445566778899"
        assert meta["span_id"] == "1122334455667788"

    def test_ignores_invalid_span_context(self, monkeypatch):
        fake_trace = ModuleType("opentelemetry.trace")

        class InvalidCtx:
            is_valid = False
            trace_id = 0
            span_id = 0

        span = MagicMock()
        span.get_span_context.return_value = InvalidCtx()
        fake_trace.get_current_span = lambda: span

        fake_otel = ModuleType("opentelemetry")
        fake_otel.trace = fake_trace
        monkeypatch.setitem(sys.modules, "opentelemetry", fake_otel)
        monkeypatch.setitem(sys.modules, "opentelemetry.trace", fake_trace)

        meta: dict = {}
        enrich_metadata(meta)
        assert "trace_id" not in meta

    def test_existing_trace_id_is_preserved(self, monkeypatch):
        fake_trace = ModuleType("opentelemetry.trace")

        class FakeCtx:
            is_valid = True
            trace_id = 0xDEADBEEFCAFEBABE00000000000000AA
            span_id = 0x1234567890ABCDEF

        span = MagicMock()
        span.get_span_context.return_value = FakeCtx()
        fake_trace.get_current_span = lambda: span

        fake_otel = ModuleType("opentelemetry")
        fake_otel.trace = fake_trace
        monkeypatch.setitem(sys.modules, "opentelemetry", fake_otel)
        monkeypatch.setitem(sys.modules, "opentelemetry.trace", fake_trace)

        meta = {"trace_id": "manual-override"}
        enrich_metadata(meta)
        assert meta["trace_id"] == "manual-override"  # setdefault preserves existing

    def test_survives_unexpected_errors(self, monkeypatch):
        fake_trace = ModuleType("opentelemetry.trace")

        def broken_get_current_span():
            raise RuntimeError("boom")

        fake_trace.get_current_span = broken_get_current_span
        fake_otel = ModuleType("opentelemetry")
        fake_otel.trace = fake_trace
        monkeypatch.setitem(sys.modules, "opentelemetry", fake_otel)
        monkeypatch.setitem(sys.modules, "opentelemetry.trace", fake_trace)

        # Must not raise — observability never crashes the app
        meta: dict = {}
        enrich_metadata(meta)
        assert meta == {}


class TestSentryBreadcrumb:
    def test_noop_without_sentry_sdk(self, monkeypatch):
        monkeypatch.setitem(sys.modules, "sentry_sdk", None)
        add_breadcrumb_for_context({"source": "cli"})  # must not raise

    def test_calls_add_breadcrumb_when_sdk_present(self, monkeypatch):
        fake_sdk = ModuleType("sentry_sdk")
        fake_sdk.add_breadcrumb = MagicMock()
        monkeypatch.setitem(sys.modules, "sentry_sdk", fake_sdk)

        add_breadcrumb_for_context({"source": "http", "user_id": 42})
        fake_sdk.add_breadcrumb.assert_called_once()
        kwargs = fake_sdk.add_breadcrumb.call_args[1]
        assert kwargs["category"] == "auditrum"
        assert kwargs["data"] == {"source": "http", "user_id": 42}
        assert "http" in kwargs["message"]

    def test_survives_sdk_errors(self, monkeypatch):
        fake_sdk = ModuleType("sentry_sdk")
        fake_sdk.add_breadcrumb = MagicMock(side_effect=RuntimeError("boom"))
        monkeypatch.setitem(sys.modules, "sentry_sdk", fake_sdk)
        add_breadcrumb_for_context({"source": "cron"})  # must not raise


class TestPrometheusCollector:
    def test_raises_clear_error_without_prometheus_client(self, monkeypatch):
        monkeypatch.setitem(sys.modules, "prometheus_client", None)
        with pytest.raises(ImportError, match="prometheus_client is required"):
            from auditrum.observability.prometheus import AuditrumCollector

            AuditrumCollector(lambda: None)

    def test_collect_yields_gauge(self):
        pytest.importorskip("prometheus_client")
        from auditrum.observability.prometheus import AuditrumCollector

        conn = MagicMock()
        cursor = MagicMock()
        cursor.fetchall.return_value = [
            ("users", "INSERT", 5),
            ("users", "UPDATE", 12),
            ("orders", "DELETE", 1),
        ]
        cursor.__enter__ = MagicMock(return_value=cursor)
        cursor.__exit__ = MagicMock(return_value=False)
        conn.cursor.return_value = cursor
        conn.close = MagicMock()

        collector = AuditrumCollector(lambda: conn, window_seconds=30)
        families = list(collector.collect())
        assert len(families) == 1
        gauge = families[0]
        assert gauge.name == "auditrum_events"
        samples = {(s.labels["table"], s.labels["operation"]): s.value for s in gauge.samples}
        assert samples[("users", "INSERT")] == 5.0
        assert samples[("users", "UPDATE")] == 12.0
        assert samples[("orders", "DELETE")] == 1.0
        conn.close.assert_called_once()

    def test_collect_swallows_db_errors(self):
        pytest.importorskip("prometheus_client")
        from auditrum.observability.prometheus import AuditrumCollector

        def _broken():
            raise RuntimeError("connection refused")

        collector = AuditrumCollector(_broken)
        families = list(collector.collect())
        # Still yields the gauge family — scraping never crashes the endpoint
        assert len(families) == 1
        assert families[0].name == "auditrum_events"

    def test_rejects_injection_in_audit_table(self):
        pytest.importorskip("prometheus_client")
        from auditrum.observability.prometheus import AuditrumCollector

        with pytest.raises(ValueError, match="Invalid audit_table"):
            AuditrumCollector(lambda: None, audit_table="bad; DROP")
