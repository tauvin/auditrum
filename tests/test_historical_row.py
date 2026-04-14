"""Unit tests for HistoricalRow — dict/attribute access + to_model fallback."""

from datetime import UTC, datetime

import pytest

from auditrum.timetravel import HistoricalRow


def _row(**data):
    return HistoricalRow(
        table="users",
        object_id="42",
        at=datetime(2024, 1, 1, tzinfo=UTC),
        data=data,
    )


class TestAttributeAccess:
    def test_getitem(self):
        r = _row(name="alice", email="a@x.com")
        assert r["name"] == "alice"
        assert r["email"] == "a@x.com"

    def test_attr(self):
        r = _row(name="alice", email="a@x.com")
        assert r.name == "alice"
        assert r.email == "a@x.com"

    def test_contains(self):
        r = _row(name="alice")
        assert "name" in r
        assert "missing" not in r

    def test_get_with_default(self):
        r = _row(name="alice")
        assert r.get("missing", "fallback") == "fallback"
        assert r.get("name") == "alice"

    def test_missing_attr_raises(self):
        r = _row(name="alice")
        with pytest.raises(AttributeError):
            _ = r.missing

    def test_dataclass_fields_still_accessible(self):
        r = _row(name="alice")
        assert r.table == "users"
        assert r.object_id == "42"
        assert r.at == datetime(2024, 1, 1, tzinfo=UTC)


class TestToModel:
    def test_plain_class_gets_full_data(self):
        class Plain:
            def __init__(self, **kwargs):
                self.kwargs = kwargs

        r = _row(name="alice", email="a@x.com")
        obj = r.to_model(Plain)
        assert obj.kwargs == {"name": "alice", "email": "a@x.com"}

    def test_django_like_model_filters_unknown_fields(self):
        class FakeField:
            def __init__(self, name):
                self.name = name
                self.concrete = True

        class FakeMeta:
            @staticmethod
            def get_fields():
                return [FakeField("name"), FakeField("email")]

        captured = {}

        class FakeModel:
            _meta = FakeMeta

            def __init__(self, **kwargs):
                captured.update(kwargs)

        r = _row(name="alice", email="a@x.com", stale_column="dropped")
        r.to_model(FakeModel)
        assert captured == {"name": "alice", "email": "a@x.com"}
        # stale_column is preserved in .data for inspection
        assert r.data["stale_column"] == "dropped"


class TestFrozen:
    def test_cannot_mutate(self):
        from dataclasses import FrozenInstanceError

        r = _row(name="alice")
        with pytest.raises(FrozenInstanceError):
            r.table = "other"  # type: ignore[misc]
