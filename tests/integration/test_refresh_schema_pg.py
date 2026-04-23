"""End-to-end regression: the 0003 migration + refresh command
actually replace stale PL/pgSQL function bodies with the current
release's versions.

This is the test that should have existed before the 0.4 paired-
diff change shipped. Without it, every release that modifies a
``generate_*_sql`` body silently fails to self-heal on upgrade —
the initial migration emits the helpers once and nothing ever
brings them forward. The user's catalog deployment hit this
exact gap on 0.4.1: ``jsonb_diff`` stayed on the 0.3 values-only
body, so new UPDATE audit rows were written in the old format
despite the library being on 0.4.

We simulate the upgrade here by:

1. Installing the schema via ``fresh_auditlog`` (which runs the
   *current* generate_*_sql — i.e. the paired versions).
2. Overwriting ``jsonb_diff`` with a synthetic stand-in body that
   looks like the 0.3 version (values-only).
3. Running the refresh logic (either via the management command or
   the 0003 migration's ``_refresh_schema`` helper directly).
4. Asserting ``jsonb_diff`` is back to the paired body.
"""

from __future__ import annotations

from urllib.parse import urlparse

import pytest

django = pytest.importorskip("django")
pytest.importorskip("psycopg")

from django.conf import settings as django_settings  # noqa: E402


@pytest.fixture(scope="session")
def configured_django_pg(pg_dsn):
    """Session-scoped Django configuration pointing at testcontainer PG.

    Skips if Django is already configured with a different backend
    (sqlite from a unit-test module that got imported first).
    """
    parsed = urlparse(pg_dsn)
    pg_db_settings = {
        "ENGINE": "django.db.backends.postgresql",
        "NAME": parsed.path.lstrip("/"),
        "USER": parsed.username,
        "PASSWORD": parsed.password,
        "HOST": parsed.hostname,
        "PORT": parsed.port,
    }

    if django_settings.configured:
        if (
            django_settings.DATABASES.get("default", {}).get("ENGINE")
            != pg_db_settings["ENGINE"]
        ):
            pytest.skip(
                "Django is already configured with a non-Postgres backend. "
                "Run integration tests in isolation: pytest tests/integration/"
            )
        return pg_db_settings

    django_settings.configure(
        SECRET_KEY="integration-test-secret",
        INSTALLED_APPS=[
            "django.contrib.contenttypes",
            "django.contrib.auth",
            "django.contrib.sessions",
            "django.contrib.messages",
            "django.contrib.admin",
            "auditrum.integrations.django",
        ],
        DATABASES={"default": pg_db_settings},
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
    return pg_db_settings


# Stand-in for the 0.3 ``jsonb_diff`` body — the actual pre-0.4
# function text. The important property is that it produces
# ``{field: value}`` (no ``jsonb_build_object``), so an assertion
# on the paired marker can distinguish old from new.
_STALE_JSONB_DIFF = """
CREATE OR REPLACE FUNCTION jsonb_diff(old jsonb, new jsonb)
RETURNS jsonb AS $$
BEGIN
  RETURN (
    SELECT jsonb_object_agg(key, value)
    FROM jsonb_each(new)
    WHERE old -> key IS DISTINCT FROM value
  );
END;
$$ LANGUAGE plpgsql IMMUTABLE;
"""


def _read_function_body(conn, name: str) -> str:
    with conn.cursor() as cur:
        cur.execute("SELECT prosrc FROM pg_proc WHERE proname = %s", (name,))
        row = cur.fetchone()
    assert row is not None, f"function {name!r} is not installed"
    return row[0]


def test_refresh_command_replaces_stale_jsonb_diff(
    fresh_auditlog, configured_django_pg
):
    from django.core.management import call_command

    conn = fresh_auditlog
    with conn.cursor() as cur:
        cur.execute(_STALE_JSONB_DIFF)

    stale_body = _read_function_body(conn, "jsonb_diff")
    assert "jsonb_object_agg(key, value)" in stale_body
    assert "jsonb_build_object" not in stale_body

    call_command("auditrum_refresh_schema")

    refreshed_body = _read_function_body(conn, "jsonb_diff")
    assert "jsonb_build_object('old', old -> key, 'new', value)" in refreshed_body
    assert "jsonb_object_agg(key, value)" not in refreshed_body


def test_migration_0003_replaces_stale_jsonb_diff(
    fresh_auditlog, configured_django_pg
):
    """The same regression covered at the migration-graph level.

    Imitates a user running ``migrate`` after upgrading from 0.3
    to 0.4 — the 0003 migration's ``_refresh_schema`` helper
    re-emits the current helper bodies over whatever 0.3 left.

    We import the migration module by dotted path (``importlib``
    handles names that start with a digit; the ``from X import Y``
    form doesn't) and invoke its ``_refresh_schema`` function
    directly — that's exactly what Django's ``RunPython`` does
    internally, just without the migration graph machinery.
    """
    import importlib

    conn = fresh_auditlog
    with conn.cursor() as cur:
        cur.execute(_STALE_JSONB_DIFF)

    assert "jsonb_object_agg(key, value)" in _read_function_body(
        conn, "jsonb_diff"
    )

    migration = importlib.import_module(
        "auditrum.integrations.django.migrations.0003_refresh_schema_04"
    )
    migration._refresh_schema(None, _FakeSchemaEditor(conn))

    refreshed_body = _read_function_body(conn, "jsonb_diff")
    assert "jsonb_build_object('old', old -> key, 'new', value)" in refreshed_body


class _FakeSchemaEditor:
    """Minimal stand-in for Django's ``schema_editor`` that exposes
    ``.connection.cursor()`` with psycopg's shape. The migration
    helper only needs this one attribute, so we wrap the psycopg
    connection we already have.
    """

    def __init__(self, pg_conn):
        self.connection = pg_conn
