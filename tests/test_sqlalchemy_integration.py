"""Unit tests for the SQLAlchemy integration core — no real DB.

Verifies the executor cursor protocol, parameter translation from
``%s``-style to SQLAlchemy ``:name`` binds, track_table registry
behaviour, and sync() wiring through TriggerManager.
"""

from unittest.mock import MagicMock

import pytest

sqlalchemy = pytest.importorskip("sqlalchemy")

from sqlalchemy import Column, Integer, MetaData, String, Table  # noqa: E402

from auditrum.integrations.sqlalchemy import (  # noqa: E402
    SQLAlchemyExecutor,
    clear_registry,
    registered_specs,
    track_table,
)
from auditrum.integrations.sqlalchemy.core import _SQLAlchemyCursor  # noqa: E402
from auditrum.tracking import FieldFilter  # noqa: E402


@pytest.fixture(autouse=True)
def _reset_registry():
    clear_registry()
    yield
    clear_registry()


def _make_table(name="users", *, cols=("id", "name", "email")):
    meta = MetaData()
    columns = [Column("id", Integer, primary_key=True)]
    for c in cols:
        if c == "id":
            continue
        columns.append(Column(c, String))
    return Table(name, meta, *columns)


class TestTrackTable:
    def test_registers_spec_with_table_name(self):
        t = _make_table()
        spec = track_table(t, fields=["email"])
        assert spec.table == "users"
        assert spec.fields.kind == "only"
        assert spec.fields.fields == ("email",)

    def test_all_registered(self):
        t1 = _make_table("users")
        t2 = _make_table("orders", cols=("id", "total"))
        track_table(t1, fields=["email"])
        track_table(t2)
        specs = registered_specs()
        assert len(specs) == 2
        assert {s.table for s in specs} == {"users", "orders"}

    def test_exclude_mode(self):
        t = _make_table()
        spec = track_table(t, exclude=["name"])
        assert spec.fields.kind == "exclude"

    def test_extra_meta(self):
        t = _make_table()
        spec = track_table(t, extra_meta=["name"])
        assert spec.extra_meta_fields == ("name",)

    def test_all_filter_by_default(self):
        t = _make_table()
        spec = track_table(t)
        assert spec.fields == FieldFilter.all()

    def test_fields_and_exclude_together_rejected(self):
        t = _make_table()
        with pytest.raises(ValueError, match="cannot pass both"):
            track_table(t, fields=["a"], exclude=["b"])


class TestSQLAlchemyCursorTranslation:
    def test_passthrough_text_without_params(self):
        conn = MagicMock()
        cur = _SQLAlchemyCursor(conn)
        cur.execute("SELECT 1")
        # SQLAlchemy's execute() called once with text(sql)
        args, kwargs = conn.execute.call_args
        assert str(args[0]) == "SELECT 1"

    def test_translates_percent_s_to_named(self):
        conn = MagicMock()
        cur = _SQLAlchemyCursor(conn)
        cur.execute("SELECT * FROM t WHERE a = %s AND b = %s", ("alpha", 7))
        args, kwargs = conn.execute.call_args
        rendered = str(args[0])
        # After translation the SQL has :p0 :p1 bind markers
        assert ":p0" in rendered
        assert ":p1" in rendered
        # The bind params dict is the second positional argument
        assert args[1] == {"p0": "alpha", "p1": 7}

    def test_dict_params_pass_through(self):
        conn = MagicMock()
        cur = _SQLAlchemyCursor(conn)
        cur.execute("SELECT * FROM t WHERE a = :x", {"x": 5})
        args, kwargs = conn.execute.call_args
        assert args[1] == {"x": 5}

    def test_fetchone_and_fetchall(self):
        conn = MagicMock()
        result = MagicMock()
        result.fetchone.return_value = ("alpha",)
        result.fetchall.return_value = [("alpha",), ("beta",)]
        conn.execute.return_value = result

        cur = _SQLAlchemyCursor(conn)
        cur.execute("SELECT 1")
        assert cur.fetchone() == ("alpha",)
        assert cur.fetchall() == [("alpha",), ("beta",)]


class TestExecutorProtocol:
    def test_cursor_context_manager(self):
        conn = MagicMock()
        conn.execute.return_value = MagicMock()
        ex = SQLAlchemyExecutor(conn)
        with ex.cursor() as cur:
            cur.execute("SELECT 1")
        # fine if no exception

    def test_usable_with_trigger_manager(self):
        """Smoke test: TriggerManager.bootstrap() through the SA executor
        should issue a CREATE TABLE via conn.execute()."""
        from auditrum.tracking import TriggerManager

        conn = MagicMock()
        # Make SA's conn.execute return a fake result with no rows
        result = MagicMock()
        result.fetchone.return_value = None
        result.fetchall.return_value = []
        conn.execute.return_value = result

        TriggerManager(SQLAlchemyExecutor(conn)).bootstrap()

        # Grab all the text strings that were passed to execute
        executed = []
        for call in conn.execute.call_args_list:
            args = call[0]
            if args:
                executed.append(str(args[0]))
        assert any("CREATE TABLE IF NOT EXISTS auditrum_applied_triggers" in s for s in executed)
