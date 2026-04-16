"""Unit tests for the backwards-compatible ``auditrum.integrations.django.audit``
module — ``register`` passthrough and the ``_LegacyRegistryView`` dict proxy.

The registry view reprojects the internal spec registry into the
historical dict-of-dicts shape old ``@pghistory`` migrators relied on.
These tests pin that projection so future refactors of the spec-side
registry don't accidentally break the legacy surface.
"""

from unittest.mock import patch

import pytest

django = pytest.importorskip("django")

from django.conf import settings as django_settings  # noqa: E402

if not django_settings.configured:
    django_settings.configure(
        INSTALLED_APPS=[
            "django.contrib.contenttypes",
            "django.contrib.auth",
            "django.contrib.admin",
            "django.contrib.sessions",
            "django.contrib.messages",
            "auditrum.integrations.django",
        ],
        DATABASES={
            "default": {"ENGINE": "django.db.backends.sqlite3", "NAME": ":memory:"}
        },
        ROOT_URLCONF="django.contrib.contenttypes.urls",
        TEMPLATES=[
            {
                "BACKEND": "django.template.backends.django.DjangoTemplates",
                "DIRS": [],
                "APP_DIRS": True,
                "OPTIONS": {"context_processors": []},
            }
        ],
    )
    django.setup()

from auditrum.integrations.django.audit import register, registry  # noqa: E402
from auditrum.tracking.spec import FieldFilter, TrackSpec  # noqa: E402


class TestRegisterPassthrough:
    def test_forwards_to_imperative_register(self):
        class DummyModel:
            class _meta:
                db_table = "dummies"

            __qualname__ = "DummyModel"

        with patch(
            "auditrum.integrations.django.audit._imperative_register"
        ) as mock:
            register(DummyModel, extra_meta_fields=["x"])

        args, kwargs = mock.call_args
        assert args[0] is DummyModel
        assert kwargs["extra_meta_fields"] == ["x"]


class TestLegacyRegistryView:
    """All six dict-like methods delegate to ``_snapshot``."""

    def _fake_model(self, db_table: str):
        class _Meta:
            pass

        _Meta.db_table = db_table

        class _Field:
            concrete = True

            def __init__(self, name: str):
                self.name = name

        _Meta.get_fields = staticmethod(
            lambda: [_Field("id"), _Field("name"), _Field("status")]
        )

        class Model:
            pass

        Model._meta = _Meta
        return Model

    def _stub_registry_with(self, specs: dict, models: list):
        return (
            patch(
                "auditrum.integrations.django.audit._spec_registry",
                new=specs,
            ),
            patch(
                "django.apps.apps.get_models",
                return_value=models,
            ),
        )

    def test_snapshot_maps_model_to_fields(self):
        model = self._fake_model("orders")
        spec = TrackSpec(
            table="orders",
            fields=FieldFilter.only("name", "status"),
        )
        reg, get_models = self._stub_registry_with(
            {spec.effective_trigger_name: spec}, [model]
        )
        with reg, get_models:
            snap = dict(registry.items())

        assert model in snap
        row = snap[model]
        assert row["table_name"] == "orders"
        assert row["fields"] == ["id", "name", "status"]
        assert row["track_only"] == ["name", "status"]
        assert row["exclude_fields"] is None

    def test_snapshot_handles_exclude_filter(self):
        model = self._fake_model("invoices")
        spec = TrackSpec(
            table="invoices",
            fields=FieldFilter.exclude("internal_notes"),
        )
        reg, get_models = self._stub_registry_with(
            {spec.effective_trigger_name: spec}, [model]
        )
        with reg, get_models:
            snap = dict(registry.items())

        row = snap[model]
        assert row["track_only"] is None
        assert row["exclude_fields"] == ["internal_notes"]

    def test_snapshot_skips_specs_without_matching_model(self):
        spec = TrackSpec(table="orphan_table")
        reg, get_models = self._stub_registry_with(
            {spec.effective_trigger_name: spec}, []
        )
        with reg, get_models:
            snap = dict(registry.items())

        assert snap == {}

    def test_iter_and_len_reflect_snapshot(self):
        model = self._fake_model("widgets")
        spec = TrackSpec(table="widgets")
        reg, get_models = self._stub_registry_with(
            {spec.effective_trigger_name: spec}, [model]
        )
        with reg, get_models:
            assert len(registry) == 1
            assert list(iter(registry)) == [model]
            assert model in registry

    def test_keys_and_values_mirror_items(self):
        model = self._fake_model("widgets")
        spec = TrackSpec(table="widgets")
        reg, get_models = self._stub_registry_with(
            {spec.effective_trigger_name: spec}, [model]
        )
        with reg, get_models:
            keys = list(registry.keys())
            values = list(registry.values())
            items = list(registry.items())
        assert keys == [model]
        assert len(values) == 1
        assert items == [(model, values[0])]

    def test_getitem_returns_row(self):
        model = self._fake_model("widgets")
        spec = TrackSpec(table="widgets", log_condition="status != 'archived'")
        reg, get_models = self._stub_registry_with(
            {spec.effective_trigger_name: spec}, [model]
        )
        with reg, get_models:
            row = registry[model]
        assert row["log_conditions"] == "status != 'archived'"
