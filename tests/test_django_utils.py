"""Unit tests for auditrum.integrations.django.utils.

Most of this file serves as a regression guard against fixes that landed
in the 0.4 typing pass:

* :class:`TestSetVar` locks in the SQL-injection fix — the helper used
  to build ``SET {key} = %s`` via f-string, which meant a
  caller-controlled ``key`` could smuggle DDL. It now uses
  ``SELECT set_config(%s, %s, false)``; every branch here asserts that
  no attacker-shaped input ever reaches the SQL string.
* :class:`TestResolveFieldValue` covers the ``timezone.datetime``
  ``AttributeError`` bug — the string-date branch previously crashed for
  every caller because ``django.utils.timezone`` never exposed a
  ``.datetime`` attribute.
* :class:`TestLinkToRelatedObject` covers the ``name=None`` default that
  used to be annotated as ``str``.
"""

from unittest.mock import MagicMock, patch

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
        USE_TZ=True,
        TIME_ZONE="UTC",
    )
    django.setup()

from auditrum.integrations.django.utils import (  # noqa: E402
    get_user_display,
    link,
    link_to_related_object,
    model_for_table,
    render_log_changes,
    resolve_field_value,
    set_var,
)


class TestSetVar:
    """Regression guard for the 0.4 SQL-injection fix."""

    def test_uses_set_config_not_raw_set_statement(self):
        cursor = MagicMock()
        set_var(cursor, "auditrum.user_id", "42")

        sql, params = cursor.execute.call_args[0]
        assert "set_config" in sql.lower()
        # A raw "SET" at the start would put us back in the pre-fix
        # injection surface, so assert it is not there.
        assert not sql.lstrip().upper().startswith("SET ")

    def test_key_is_bound_as_parameter(self):
        cursor = MagicMock()
        malicious_key = "foo; DROP TABLE users; --"
        set_var(cursor, malicious_key, "anything")

        sql, params = cursor.execute.call_args[0]
        # The attacker-shaped key must never appear in the SQL string;
        # it travels through psycopg's parameter binding.
        assert malicious_key not in sql
        assert params[0] == malicious_key

    def test_value_is_bound_as_parameter(self):
        cursor = MagicMock()
        quote_injection = "x'; DROP TABLE t; --"
        set_var(cursor, "auditrum.user_id", quote_injection)

        sql, params = cursor.execute.call_args[0]
        assert quote_injection not in sql
        assert params[1] == quote_injection

    def test_none_value_is_noop(self):
        cursor = MagicMock()
        set_var(cursor, "auditrum.user_id", None)
        cursor.execute.assert_not_called()

    def test_non_string_value_is_coerced_to_str(self):
        cursor = MagicMock()
        set_var(cursor, "auditrum.user_id", 42)
        _, params = cursor.execute.call_args[0]
        assert params[1] == "42"

    def test_is_local_flag_is_false(self):
        # session-scoped (matches the semantics the old f-string version
        # emitted via "SET key = value") not transaction-scoped.
        cursor = MagicMock()
        set_var(cursor, "auditrum.user_id", "1")
        sql, _ = cursor.execute.call_args[0]
        assert "false" in sql.lower()


class TestLink:
    def test_escapes_href_and_text(self):
        out = link('<script>alert("x")</script>', '<b>boom</b>')
        # format_html escapes HTML-unsafe characters in both args.
        assert "<script>" not in out
        assert "&lt;" in out


class TestLinkToRelatedObject:
    def _obj(self, pk=7, label="widget-7"):
        obj = MagicMock()
        obj._meta = MagicMock()
        obj.pk = pk
        obj.__str__ = MagicMock(return_value=label)
        return obj

    def test_uses_str_obj_when_name_is_none(self):
        # The signature fix promoted name from `str` to `str | None`;
        # the runtime has always accepted None via the `name or str(obj)`
        # guard. This test locks that in.
        obj = self._obj()
        with patch(
            "auditrum.integrations.django.utils.resolve_url",
            return_value="/admin/app/widget/7/change/",
        ):
            out = link_to_related_object(obj)
        assert "widget-7" in out
        assert "/admin/app/widget/7/change/" in out

    def test_uses_name_when_provided(self):
        obj = self._obj()
        with patch(
            "auditrum.integrations.django.utils.resolve_url",
            return_value="/x/",
        ):
            out = link_to_related_object(obj, name="explicit-label")
        assert "explicit-label" in out
        assert "widget-7" not in out


class TestResolveFieldValue:
    """Uses the real AuditLog model so isinstance() branches hit."""

    def test_string_datetime_value_does_not_raise(self):
        # Pre-fix this branch raised AttributeError on every call because
        # ``django.utils.timezone`` has no ``.datetime`` attribute.
        from auditrum.integrations.django.models import AuditLog

        label, formatted = resolve_field_value(
            AuditLog, "changed_at", "2024-06-01T12:00:00+00:00"
        )
        assert label  # verbose_name.title()
        # The formatter produced *something*; we don't pin the exact
        # locale-formatted string.
        assert formatted not in (None, "—")

    def test_charfield_value_passes_through(self):
        from auditrum.integrations.django.models import AuditLog

        label, formatted = resolve_field_value(
            AuditLog, "operation", "INSERT"
        )
        assert label == "Operation"
        assert formatted == "INSERT"

    def test_none_value_returns_em_dash(self):
        from auditrum.integrations.django.models import AuditLog

        _, formatted = resolve_field_value(AuditLog, "object_id", None)
        assert formatted == "—"

    def test_unknown_field_falls_through_gracefully(self):
        from auditrum.integrations.django.models import AuditLog

        label, formatted = resolve_field_value(
            AuditLog, "definitely_not_a_field", "x"
        )
        # Exception path: label falls back to the field name, value passes
        # through.
        assert label == "definitely_not_a_field"
        assert formatted == "x"

    def test_choices_are_resolved_to_display(self):
        from auditrum.integrations.django.models import AuditLog

        fake_field = MagicMock()
        fake_field.verbose_name = "status"
        fake_field.choices = [("a", "Active"), ("b", "Blocked")]
        with patch.object(AuditLog._meta, "get_field", return_value=fake_field):
            _, formatted = resolve_field_value(AuditLog, "status", "a")
        assert formatted == "Active"


class TestGetUserDisplay:
    def test_username_from_user_attribute(self):
        log = MagicMock()
        log.user.username = "alice"
        log.meta = {}
        assert get_user_display(log) == "alice"

    def test_fallback_to_meta_username(self):
        log = MagicMock()
        log.user = None
        log.meta = {"username": "bob"}
        assert get_user_display(log) == "bob"

    def test_fallback_to_system_when_unknown(self):
        log = MagicMock()
        log.user = None
        log.meta = {}
        assert get_user_display(log) == "System"


class TestRenderLogChanges:
    def _log(self, **overrides):
        log = MagicMock()
        log.table_name = "orders"
        log.operation = "UPDATE"
        log.old_data = {"name": "old"}
        log.new_data = {"name": "new"}
        for k, v in overrides.items():
            setattr(log, k, v)
        return log

    def test_unknown_table_returns_em_dash(self):
        # model_for_table returns None for tables that don't map to any
        # installed model (cross-service audit rows, dropped models).
        log = self._log()
        with patch(
            "auditrum.integrations.django.utils.model_for_table",
            return_value=None,
        ):
            assert render_log_changes(log) == "—"

    def test_insert_renders_field_list(self):
        log = self._log(operation="INSERT", new_data={"name": "x"}, old_data=None)
        with (
            patch(
                "auditrum.integrations.django.utils.model_for_table",
                return_value=MagicMock(),
            ),
            patch(
                "auditrum.integrations.django.utils.resolve_field_value",
                return_value=("Name", "x"),
            ),
        ):
            out = render_log_changes(log)
        assert "Name" in out
        assert "x" in out

    def test_delete_renders_strikethrough_list(self):
        log = self._log(operation="DELETE", old_data={"name": "gone"}, new_data=None)
        with (
            patch(
                "auditrum.integrations.django.utils.model_for_table",
                return_value=MagicMock(),
            ),
            patch(
                "auditrum.integrations.django.utils.resolve_field_value",
                return_value=("Name", "gone"),
            ),
        ):
            out = render_log_changes(log)
        assert "line-through" in out
        assert "gone" in out

    def test_update_renders_diffs(self):
        log = self._log()
        with (
            patch(
                "auditrum.integrations.django.utils.model_for_table",
                return_value=MagicMock(),
            ),
            patch(
                "auditrum.integrations.django.utils.resolve_field_value",
                side_effect=lambda mc, k, v: (k.title(), str(v) if v is not None else "—"),
            ),
        ):
            out = render_log_changes(log)
        assert "old" in out
        assert "new" in out
        assert "→" in out

    def test_update_with_no_changes_returns_placeholder(self):
        log = self._log(old_data={"x": 1}, new_data={"x": 1})
        with (
            patch(
                "auditrum.integrations.django.utils.model_for_table",
                return_value=MagicMock(),
            ),
            patch(
                "auditrum.integrations.django.utils.resolve_field_value",
                return_value=("X", "1"),
            ),
        ):
            out = render_log_changes(log)
        assert "No changes" in out


class TestModelForTable:
    def test_returns_matching_model(self):
        # ContentType is an installed model with a well-known db_table;
        # we use it as a real lookup target so the test doesn't depend on
        # any project-local model being defined.
        from django.contrib.contenttypes.models import ContentType

        resolved = model_for_table(ContentType._meta.db_table)
        assert resolved is ContentType

    def test_returns_none_for_unknown_table(self):
        assert model_for_table("__no_such_table__") is None
