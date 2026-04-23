"""End-to-end admin-history regression tests.

Locks in the 0.4 fix for the ``content_type_id`` silent-empty-UI bug.
The 0.3.x Django admin ``AuditHistoryMixin.object_history_view``
filtered ``AuditLog`` on ``content_type``, but the framework-agnostic
PL/pgSQL trigger never wrote that column — so every history page
rendered "No audit records found" regardless of how many events
actually sat in ``auditlog``.

These tests exercise the real path: install a real trigger on a real
Postgres table (via the ``fresh_auditlog`` testcontainer fixture),
run real INSERT / UPDATE statements, and assert that
:meth:`AuditLogQuerySet.for_object` (the replacement filter) returns
them. Unit tests alone cannot catch this class of bug — the model
surface was well-typed, the queryset helpers were well-tested in
isolation, but the integration between trigger and queryset was
broken at the column level. One real round-trip assertion closes
the gap.
"""

from urllib.parse import urlparse

import pytest

django = pytest.importorskip("django")
pytest.importorskip("psycopg")

from django.conf import settings as django_settings  # noqa: E402


@pytest.fixture(scope="session")
def configured_django(pg_dsn):
    """Configure Django once per session to use the testcontainer Postgres.

    Skips with a clear message if Django is already configured with a
    different backend (e.g. sqlite from the unit-test modules that
    share this pytest process). Run integration tests in isolation —
    ``pytest tests/integration/`` — to avoid the collision.
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
        current_engine = django_settings.DATABASES.get("default", {}).get("ENGINE")
        if current_engine != pg_db_settings["ENGINE"]:
            pytest.skip(
                "Django is already configured with a non-Postgres backend "
                "(probably from a unit-test module). Run integration tests "
                "in isolation: pytest tests/integration/"
            )
        return pg_db_settings

    django_settings.configure(
        INSTALLED_APPS=[
            "django.contrib.contenttypes",
            "django.contrib.auth",
            "auditrum.integrations.django",
        ],
        DATABASES={"default": pg_db_settings},
        USE_TZ=True,
        TIME_ZONE="UTC",
    )
    django.setup()
    return pg_db_settings


def test_auditlog_schema_has_no_content_type_id(fresh_auditlog):
    """The legacy ``content_type_id`` column must not be re-introduced.

    The column was dead weight — the trigger never wrote it, the admin
    read from it. Removing it made ``AuditLog.objects.for_object``
    the only supported identity path.
    """
    conn = fresh_auditlog
    with conn.cursor() as cur:
        cur.execute(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_name = 'auditlog' ORDER BY ordinal_position"
        )
        columns = [r[0] for r in cur.fetchall()]
    assert "content_type_id" not in columns


def test_for_object_returns_trigger_rows(fresh_auditlog, configured_django):
    """End-to-end: trigger → auditlog row → ``for_object`` queryset.

    This is the test the content_type bug survived for: a real trigger
    writing into ``auditlog``, and the admin-history filter finding
    those rows by ``(table_name, object_id)``. If a future refactor
    re-routes the filter through a column the trigger doesn't populate,
    this fails immediately instead of silently shipping an empty UI.
    """
    from auditrum.integrations.django.models import AuditLog
    from auditrum.triggers import generate_trigger_sql

    conn = fresh_auditlog
    with conn.cursor() as cur:
        cur.execute("DROP TABLE IF EXISTS orders CASCADE")
        cur.execute(
            "CREATE TABLE orders (id serial PRIMARY KEY, status text NOT NULL)"
        )
        cur.execute(generate_trigger_sql("orders"))
        cur.execute("INSERT INTO orders (status) VALUES ('new') RETURNING id")
        (order_id,) = cur.fetchone()
        cur.execute("UPDATE orders SET status = 'paid' WHERE id = %s", (order_id,))

    class FakeMeta:
        db_table = "orders"

    class FakeOrder:
        _meta = FakeMeta
        pk = order_id

    ops = list(
        AuditLog.objects.for_object(FakeOrder())
        .order_by("id")
        .values_list("operation", flat=True)
    )
    assert ops == ["INSERT", "UPDATE"]


def test_for_object_matches_context_metadata(fresh_auditlog, configured_django):
    """End-to-end: context metadata reaches the audit row via the FK.

    Locks in the ``log.context.metadata.source`` path used by the fixed
    ``object_history.html`` template. The 0.3.x template rendered
    ``log.source`` and ``log.change_reason`` directly — neither field
    exists on ``AuditLog``, so both were silently empty. The fix reads
    through the ``context`` FK → ``audit_context.metadata`` JSON.
    """
    from auditrum.integrations.django.models import AuditLog
    from auditrum.triggers import generate_trigger_sql

    conn = fresh_auditlog
    ctx_uuid = "00000000-0000-0000-0000-0000000000cc"
    metadata_json = '{"source": "celery", "change_reason": "nightly sync"}'

    with conn.cursor() as cur:
        cur.execute("DROP TABLE IF EXISTS widgets CASCADE")
        cur.execute("CREATE TABLE widgets (id serial PRIMARY KEY, name text)")
        cur.execute(generate_trigger_sql("widgets"))
        cur.execute(
            "SELECT set_config('auditrum.context_id', %s, false), "
            "set_config('auditrum.context_metadata', %s, false)",
            (ctx_uuid, metadata_json),
        )
        cur.execute("INSERT INTO widgets (name) VALUES ('w1') RETURNING id")
        (widget_id,) = cur.fetchone()

    class FakeMeta:
        db_table = "widgets"

    class FakeWidget:
        _meta = FakeMeta
        pk = widget_id

    log = AuditLog.objects.for_object(FakeWidget()).select_related("context").first()
    assert log is not None
    assert log.context is not None
    assert log.context.metadata["source"] == "celery"
    assert log.context.metadata["change_reason"] == "nightly sync"
