import pytest

from auditrum.tracking import FieldFilter, TrackSpec, TriggerBundle
from auditrum.tracking._template import _StrictMap, render


class TestStrictMap:
    def test_missing_key_raises_with_helpful_message(self):
        m = _StrictMap({"a": "x"})
        with pytest.raises(KeyError, match="placeholder .*b.* has no binding"):
            m["b"]

    def test_present_key_returns(self):
        m = _StrictMap({"a": "x"})
        assert m["a"] == "x"


class TestRender:
    def test_rejects_missing_placeholder_before_format(self):
        with pytest.raises(KeyError, match="requires placeholders"):
            render("audit_trigger.sql", function_name="x")

    def test_renders_valid_template(self):
        sql = render(
            "audit_trigger.sql",
            function_name="audit_x_trigger",
            trigger_name="audit_x_trigger",
            table_name="x",
            audit_table="auditlog",
            ignored_keys_expr="ARRAY[]::text[]",
            log_conditions_block="",
            meta_expr="NULL",
        )
        assert "CREATE TRIGGER audit_x_trigger" in sql
        assert "ON x" in sql

    def test_extra_bindings_are_tolerated(self):
        sql = render(
            "audit_trigger.sql",
            function_name="f",
            trigger_name="f",
            table_name="t",
            audit_table="a",
            ignored_keys_expr="ARRAY[]::text[]",
            log_conditions_block="",
            meta_expr="NULL",
            unused_extra="ok",
        )
        assert sql


class TestFieldFilter:
    def test_all(self):
        f = FieldFilter.all()
        assert f.kind == "all"
        assert f.fields == ()
        assert f.to_ignored_keys_expr() == "ARRAY[]::text[]"

    def test_only_whitelist(self):
        f = FieldFilter.only("name", "email")
        assert f.kind == "only"
        assert f.fields == ("name", "email")
        expr = f.to_ignored_keys_expr()
        assert "NOT IN ('name', 'email')" in expr

    def test_exclude_blacklist(self):
        f = FieldFilter.exclude("password", "token")
        expr = f.to_ignored_keys_expr()
        assert "ARRAY['password', 'token']::text[]" in expr

    def test_all_with_fields_rejected(self):
        with pytest.raises(ValueError, match="must not carry field names"):
            FieldFilter(kind="all", fields=("x",))

    def test_only_empty_rejected(self):
        with pytest.raises(ValueError, match="requires at least one field"):
            FieldFilter(kind="only", fields=())

    def test_exclude_empty_rejected(self):
        with pytest.raises(ValueError, match="requires at least one field"):
            FieldFilter(kind="exclude", fields=())

    @pytest.mark.parametrize("bad", ["users; DROP", "1bad", "bad-name", ""])
    def test_invalid_identifiers_rejected(self, bad):
        with pytest.raises(ValueError, match="Invalid"):
            FieldFilter.only(bad)

    def test_hashable(self):
        a = FieldFilter.only("x")
        b = FieldFilter.only("x")
        assert hash(a) == hash(b)
        assert {a, b} == {a}


class TestTrackSpec:
    def test_minimal(self):
        spec = TrackSpec(table="users")
        bundle = spec.build()
        assert isinstance(bundle, TriggerBundle)
        assert bundle.trigger_name == "audit_users_trigger"
        assert bundle.function_name == "audit_users_trigger"
        assert "CREATE TRIGGER audit_users_trigger" in bundle.install_sql
        assert "DROP TRIGGER IF EXISTS audit_users_trigger" in bundle.uninstall_sql

    def test_custom_trigger_name(self):
        spec = TrackSpec(table="users", trigger_name="my_trigger")
        bundle = spec.build()
        assert bundle.trigger_name == "my_trigger"
        assert "CREATE TRIGGER my_trigger" in bundle.install_sql

    def test_checksum_is_deterministic(self):
        a = TrackSpec(table="users", fields=FieldFilter.only("name"))
        b = TrackSpec(table="users", fields=FieldFilter.only("name"))
        assert a.build().checksum == b.build().checksum

    def test_checksum_changes_with_fields(self):
        a = TrackSpec(table="users", fields=FieldFilter.only("name"))
        b = TrackSpec(table="users", fields=FieldFilter.only("name", "email"))
        assert a.build().checksum != b.build().checksum

    def test_checksum_changes_with_meta(self):
        a = TrackSpec(table="users")
        b = TrackSpec(table="users", extra_meta_fields=("tenant_id",))
        assert a.build().checksum != b.build().checksum

    def test_checksum_changes_with_log_condition(self):
        a = TrackSpec(table="users")
        b = TrackSpec(table="users", log_condition="NEW.is_active")
        assert a.build().checksum != b.build().checksum

    def test_log_condition_embedded_in_body(self):
        spec = TrackSpec(table="users", log_condition="NEW.is_active = TRUE")
        install = spec.build().install_sql
        assert "IF NOT (NEW.is_active = TRUE) THEN" in install

    def test_extra_meta_fields_embedded(self):
        spec = TrackSpec(table="users", extra_meta_fields=("tenant_id",))
        install = spec.build().install_sql
        assert "'tenant_id'" in install
        assert "to_jsonb(NEW.tenant_id)" in install

    def test_calls_audit_attach_context(self):
        spec = TrackSpec(table="users")
        assert "_audit_attach_context()" in spec.build().install_sql

    def test_calls_audit_current_user_id(self):
        spec = TrackSpec(table="users")
        assert "_audit_current_user_id()" in spec.build().install_sql

    @pytest.mark.parametrize("bad", ["users; DROP", "1bad", "bad-name"])
    def test_invalid_table_rejected(self, bad):
        with pytest.raises(ValueError, match="Invalid table"):
            TrackSpec(table=bad)

    def test_fingerprint_round_trip(self):
        spec = TrackSpec(
            table="users",
            fields=FieldFilter.only("name", "email"),
            extra_meta_fields=("tenant_id",),
        )
        fp = spec.to_fingerprint()
        assert fp["table"] == "users"
        assert fp["fields_kind"] == "only"
        assert fp["fields"] == ["name", "email"]
        assert fp["extra_meta_fields"] == ["tenant_id"]

    def test_hashable_and_immutable(self):
        a = TrackSpec(table="users", fields=FieldFilter.only("name"))
        b = TrackSpec(table="users", fields=FieldFilter.only("name"))
        assert hash(a) == hash(b)
        from dataclasses import FrozenInstanceError

        with pytest.raises(FrozenInstanceError):
            a.table = "other"  # type: ignore[misc]
