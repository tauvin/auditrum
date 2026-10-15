"""Integration: the SQLAlchemy bootstrap + sync path against real Postgres."""

import pytest

sqlalchemy = pytest.importorskip("sqlalchemy")

from sqlalchemy import Column, Integer, MetaData, String, Table, create_engine, text  # noqa: E402

from auditrum.integrations.sqlalchemy import (  # noqa: E402
    bootstrap_schema,
    clear_registry,
    sync,
    track_table,
)
from auditrum.timetravel import reconstruct_row  # noqa: E402


@pytest.fixture
def engine(fresh_auditlog, pg_dsn):
    # Start from an empty audit schema so bootstrap_schema really creates it.
    with fresh_auditlog.cursor() as cur:
        cur.execute("DROP TABLE IF EXISTS auditlog CASCADE")
        cur.execute("DROP TABLE IF EXISTS audit_context CASCADE")
        cur.execute("DROP TABLE IF EXISTS auditrum_applied_triggers CASCADE")
        cur.execute("DROP TABLE IF EXISTS sa_things CASCADE")
    eng = create_engine(pg_dsn.replace("postgresql://", "postgresql+psycopg://", 1))
    clear_registry()
    yield eng
    clear_registry()
    with eng.begin() as conn:
        conn.execute(text("DROP TABLE IF EXISTS sa_things CASCADE"))
    eng.dispose()


def test_bootstrap_sync_and_time_travel(engine, fresh_auditlog):
    bootstrap_schema(engine, months_ahead=1)
    meta = MetaData()
    things = Table(
        "sa_things",
        meta,
        Column("id", Integer, primary_key=True),
        Column("name", String),
    )
    meta.create_all(engine)
    track_table(things, storage="diff")

    report = sync(engine)
    assert report.installed == ["audit_sa_things_trigger"]

    with engine.begin() as conn:
        conn.execute(text("INSERT INTO sa_things VALUES (1, 'a')"))
    with engine.connect() as conn:
        # The database clock, not the test host's: they can drift apart.
        mid = conn.execute(text("SELECT clock_timestamp()")).scalar()
    with engine.begin() as conn:
        conn.execute(text("UPDATE sa_things SET name = 'b' WHERE id = 1"))
        ops = conn.execute(text("SELECT operation FROM auditlog ORDER BY id")).scalars().all()
    assert ops == ["INSERT", "UPDATE"]

    assert reconstruct_row(fresh_auditlog, table="sa_things", object_id="1", at=mid) == {
        "id": 1,
        "name": "a",
    }
