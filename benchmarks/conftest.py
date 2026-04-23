"""Shared fixtures for the benchmark suite.

Benchmarks intentionally live in a sibling directory to ``tests/``
rather than under ``tests/benchmark/`` — they have different
semantics (tracked metrics, higher wall-clock budget, per-PG-version
comparisons) and their own CI job. Keeping them separate avoids
accidentally lumping them into a ``pytest tests/`` run that's tuned
for fast iteration.

The Postgres fixture mirrors ``tests/integration/conftest.py``: a
single ``testcontainers.PostgresContainer`` per session, psycopg
connection per test. Each benchmark that mutates schema resets
``auditlog`` + ``audit_context`` between runs via the
``fresh_auditlog`` fixture.
"""

from __future__ import annotations

import socket

import pytest


def _docker_available() -> bool:
    for path in ("/var/run/docker.sock", "~/.docker/run/docker.sock"):
        import os

        expanded = os.path.expanduser(path)
        if not os.path.exists(expanded):
            continue
        try:
            s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            s.settimeout(0.5)
            s.connect(expanded)
            s.close()
            return True
        except OSError:
            continue
    return False


@pytest.fixture(scope="session")
def pg_container():
    if not _docker_available():
        pytest.skip("Docker not available; benchmarks need testcontainers")
    from testcontainers.postgres import PostgresContainer

    with PostgresContainer("postgres:16-alpine") as container:
        yield container


@pytest.fixture(scope="session")
def pg_dsn(pg_container):
    url = pg_container.get_connection_url()
    return url.replace("postgresql+psycopg2://", "postgresql://").replace(
        "postgresql+psycopg://", "postgresql://"
    )


@pytest.fixture
def pg_conn(pg_dsn):
    import psycopg

    with psycopg.connect(pg_dsn, autocommit=True) as conn:
        yield conn


@pytest.fixture
def fresh_auditlog(pg_conn):
    """Same shape as ``tests/integration/conftest.py::fresh_auditlog``.

    Installs the full audit schema (auditlog, audit_context,
    jsonb_diff, attach/current_user/reconstruct functions, one month
    of partitions) for each benchmark, so setup overhead is not part
    of the measurement — only trigger / chain / reconstruct work is.
    """
    from auditrum.schema import (
        generate_audit_attach_context_sql,
        generate_audit_context_table_sql,
        generate_audit_current_user_id_sql,
        generate_audit_reconstruct_sql,
        generate_auditlog_partitions_sql,
        generate_auditlog_table_sql,
        generate_jsonb_diff_function_sql,
    )

    with pg_conn.cursor() as cur:
        cur.execute("DROP TABLE IF EXISTS auditlog CASCADE")
        cur.execute("DROP TABLE IF EXISTS audit_context CASCADE")
        cur.execute("DROP FUNCTION IF EXISTS jsonb_diff(jsonb, jsonb) CASCADE")
        cur.execute("DROP FUNCTION IF EXISTS _audit_attach_context() CASCADE")
        cur.execute("DROP FUNCTION IF EXISTS _audit_current_user_id() CASCADE")
        cur.execute(
            "DROP FUNCTION IF EXISTS "
            "_audit_reconstruct_row(text, text, timestamptz) CASCADE"
        )
        cur.execute(
            "DROP FUNCTION IF EXISTS "
            "_audit_reconstruct_table(text, timestamptz) CASCADE"
        )
        cur.execute(generate_audit_context_table_sql("audit_context"))
        cur.execute(generate_auditlog_table_sql("auditlog"))
        cur.execute(generate_jsonb_diff_function_sql())
        cur.execute(generate_audit_attach_context_sql("audit_context"))
        cur.execute(generate_audit_current_user_id_sql())
        cur.execute(generate_audit_reconstruct_sql("auditlog"))
        cur.execute(generate_auditlog_partitions_sql("auditlog", months_ahead=1))
    yield pg_conn
