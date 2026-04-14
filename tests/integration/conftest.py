import socket

import pytest


def _docker_available() -> bool:
    sock_path = "/var/run/docker.sock"
    try:
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(0.5)
        s.connect(sock_path)
        s.close()
        return True
    except OSError:
        pass
    import os

    user_sock = os.path.expanduser("~/.docker/run/docker.sock")
    if not os.path.exists(user_sock):
        return False
    try:
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(0.5)
        s.connect(user_sock)
        s.close()
        return True
    except OSError:
        return False


@pytest.fixture(scope="session")
def pg_container():
    if not _docker_available():
        pytest.skip("Docker not available; integration tests require testcontainers")
    from testcontainers.postgres import PostgresContainer

    with PostgresContainer("postgres:16-alpine") as container:
        yield container


@pytest.fixture(scope="session")
def pg_dsn(pg_container):
    # testcontainers returns an SQLAlchemy URL; convert to libpq DSN
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
    """Create a clean auditlog + audit_context schema + helper functions per test.

    Mirrors what the Django ``0001_initial`` migration installs in a real
    project: the audit log table, the context table, ``jsonb_diff``, the
    two PL/pgSQL helper functions that the audit trigger calls
    (``_audit_attach_context`` and ``_audit_current_user_id``), the
    reconstruct functions used by time-travel queries, and one month of
    rolling partitions.
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
    with pg_conn.cursor() as cur:
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
        cur.execute("DROP TABLE IF EXISTS auditlog CASCADE")
        cur.execute("DROP TABLE IF EXISTS audit_context CASCADE")
