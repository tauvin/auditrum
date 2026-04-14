"""Unit tests for auditrum.integrations.django.runtime.

These tests don't touch a real database — they exercise the statement-prefix
filter, recursion guard, nested context behaviour, and GUC prefix format via
a stubbed ``execute`` callback.
"""

import uuid

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
        DATABASES={"default": {"ENGINE": "django.db.backends.sqlite3", "NAME": ":memory:"}},
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

from auditrum.integrations.django.runtime import (  # noqa: E402
    _inject_audit_context,
    _is_ignored_statement,
    auditrum_context,
    current_context,
)


class _FakeCursor:
    def __init__(self, name=None):
        self.name = name
        self.connection = self

    class _Info:
        transaction_status = None  # not INERROR

    info = _Info()


def _make_context(cursor=None):
    return {"cursor": cursor or _FakeCursor()}


class TestIsIgnoredStatement:
    @pytest.mark.parametrize(
        "sql",
        [
            "SELECT 1",
            "select * from users",
            "VACUUM FULL",
            "CREATE TABLE foo ()",
            "ALTER TABLE foo ADD COLUMN x int",
            "DROP TABLE foo",
            "BEGIN",
            "COMMIT",
            # CTE — must be ignored too. Prepending "SELECT set_config(...);"
            # to a "WITH ..." statement produces invalid multistatement SQL.
            "WITH x AS (SELECT 1) INSERT INTO y SELECT * FROM x",
        ],
    )
    def test_ignored(self, sql):
        assert _is_ignored_statement(sql)

    @pytest.mark.parametrize(
        "sql",
        [
            "INSERT INTO users VALUES (1)",
            "UPDATE users SET name='a'",
            "DELETE FROM users WHERE id=1",
        ],
    )
    def test_not_ignored(self, sql):
        assert not _is_ignored_statement(sql)


class TestAuditrumContextLifecycle:
    def test_enter_sets_tracker_with_uuid_and_metadata(self):
        assert current_context() is None
        with auditrum_context(source="cli", user_id=1) as ctx:
            assert ctx is not None
            assert dict(ctx.metadata) == {"source": "cli", "user_id": 1}
            assert current_context() is ctx
        assert current_context() is None

    def test_nested_pushes_new_context_with_merged_metadata(self):
        """Nested entry creates a *new* immutable context inheriting the
        outer id with merged metadata. Outer is restored on inner exit
        verbatim — inner-block keys must not leak out."""
        with auditrum_context(source="http") as outer:
            assert dict(outer.metadata) == {"source": "http"}
            outer_id = outer.id

            with auditrum_context(user_id=42) as inner:
                # Inner is a different object but inherits outer's id
                assert inner is not outer
                assert inner.id == outer_id
                assert dict(inner.metadata) == {"source": "http", "user_id": 42}
                assert current_context() is inner

            # After inner exits, outer is restored — and outer.metadata
            # was never mutated.
            assert current_context() is outer
            assert dict(outer.metadata) == {"source": "http"}

        assert current_context() is None

    def test_outer_metadata_is_immutable(self):
        """metadata is a MappingProxyType — mutation attempts raise."""
        with auditrum_context(source="http") as ctx, pytest.raises(TypeError):
            ctx.metadata["evil"] = "value"  # type: ignore[index]

    def test_context_is_frozen(self):
        from dataclasses import FrozenInstanceError

        with auditrum_context(source="http") as ctx, pytest.raises(FrozenInstanceError):
            ctx.id = uuid.uuid4()  # type: ignore[misc]

    def test_inner_exception_restores_outer(self):
        """If an exception escapes the inner block, the outer must still
        be restored (and unchanged), and the inner context must not leak."""
        with auditrum_context(source="http") as outer:
            with pytest.raises(RuntimeError), auditrum_context(user_id=99):
                raise RuntimeError("inner boom")
            assert current_context() is outer
            assert dict(outer.metadata) == {"source": "http"}
        assert current_context() is None

    def test_exception_inside_block_still_clears_tracker(self):
        with pytest.raises(RuntimeError), auditrum_context(source="cli"):
            raise RuntimeError("boom")
        assert current_context() is None

    def test_three_levels_of_nesting(self):
        """Verify three-level nesting with merges and restorations."""
        with auditrum_context(level="a") as a:
            with auditrum_context(level="b", extra="x") as b:
                assert dict(b.metadata) == {"level": "b", "extra": "x"}
                with auditrum_context(level="c") as c:
                    assert dict(c.metadata) == {"level": "c", "extra": "x"}
                # b restored
                assert current_context() is b
                assert dict(b.metadata) == {"level": "b", "extra": "x"}
            # a restored
            assert current_context() is a
            assert dict(a.metadata) == {"level": "a"}
        assert current_context() is None


class TestInjectAuditContext:
    def _captured_execute(self):
        captured = {}

        def execute(sql, params, many, context):
            captured["sql"] = sql
            captured["params"] = params
            captured["many"] = many
            return "ok"

        return execute, captured

    def test_passes_through_without_tracker(self):
        execute, captured = self._captured_execute()
        result = _inject_audit_context(
            execute, "INSERT INTO x VALUES (1)", (), False, _make_context()
        )
        assert result == "ok"
        assert captured["sql"] == "INSERT INTO x VALUES (1)"

    def test_prepends_set_config_for_tracked_insert(self):
        execute, captured = self._captured_execute()
        with auditrum_context(user_id=1):
            _inject_audit_context(
                execute, "INSERT INTO users VALUES (1)", (), False, _make_context()
            )
        # GUC names are now bound parameters too, not f-string-embedded
        assert "set_config(%s, %s, true)" in captured["sql"]
        assert captured["sql"].count("set_config(%s, %s, true)") == 2
        assert "INSERT INTO users VALUES (1)" in captured["sql"]
        params = captured["params"]
        # 4 prepended params: id_name, id_value, metadata_name, metadata_value
        assert len(params) == 4
        assert params[0] == "auditrum.context_id"
        assert isinstance(params[1], str)  # uuid
        assert params[2] == "auditrum.context_metadata"
        assert '"user_id": 1' in params[3]

    def test_guc_names_are_bound_parameters_not_f_string(self):
        """Defence in depth: even though guc_id is validated in settings,
        we pass it through as a bound parameter so injection via Django
        settings (PGAUDIT_GUC_ID) cannot escape the literal."""
        execute, captured = self._captured_execute()
        with auditrum_context(user_id=1):
            _inject_audit_context(
                execute, "INSERT INTO users VALUES (1)", (), False, _make_context()
            )
        # The GUC name must NOT appear literally inside the SQL string —
        # it's a placeholder, and the actual name is in params.
        assert "auditrum.context_id" not in captured["sql"]
        assert "auditrum.context_metadata" not in captured["sql"]
        # But it IS in the params
        assert "auditrum.context_id" in captured["params"]
        assert "auditrum.context_metadata" in captured["params"]

    def test_skips_injection_for_select(self):
        execute, captured = self._captured_execute()
        with auditrum_context(user_id=1):
            _inject_audit_context(execute, "SELECT 1", (), False, _make_context())
        assert captured["sql"] == "SELECT 1"
        assert "set_config" not in captured["sql"]

    def test_skips_injection_for_named_cursor(self):
        execute, captured = self._captured_execute()
        with auditrum_context(user_id=1):
            _inject_audit_context(
                execute,
                "INSERT INTO x VALUES (1)",
                (),
                False,
                _make_context(cursor=_FakeCursor(name="named")),
            )
        assert "set_config" not in captured["sql"]

    def test_sequential_queries_both_get_injected(self):
        """Every top-level cursor.execute in a context block must be prefixed,
        because ``is_local=true`` scopes the GUC to the current statement's
        transaction only."""
        execute, captured_list = self._accumulating_execute()
        with auditrum_context(user_id=1):
            _inject_audit_context(
                execute, "INSERT INTO x VALUES (1)", (), False, _make_context()
            )
            _inject_audit_context(
                execute, "UPDATE y SET z=1", (), False, _make_context()
            )
        assert len(captured_list) == 2
        assert all("set_config" in c["sql"] for c in captured_list)

    def _accumulating_execute(self):
        captured = []

        def execute(sql, params, many, context):
            captured.append({"sql": sql, "params": params})
            return "ok"

        return execute, captured

    def test_preserves_dict_params(self):
        execute, captured = self._captured_execute()
        with auditrum_context(user_id=1):
            _inject_audit_context(
                execute,
                "INSERT INTO users (id, name) VALUES (%(id)s, %(name)s)",
                {"id": 1, "name": "alice"},
                False,
                _make_context(),
            )
        params = captured["params"]
        assert params["id"] == 1
        assert params["name"] == "alice"
        # All four GUC params (name + value × 2) merged into the dict
        assert params["auditrum__guc_id_name"] == "auditrum.context_id"
        assert params["auditrum__context_id"]
        assert params["auditrum__guc_metadata_name"] == "auditrum.context_metadata"
        assert params["auditrum__context_metadata"]
