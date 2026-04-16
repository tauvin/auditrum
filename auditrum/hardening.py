"""Tamper-resistance helpers for the audit log.

Audit log tables are high-value targets. auditrum's compliance story is
"the application can only produce truthful audit rows, never forged or
modified ones". Achieving that requires two things together:

1. **Direct writes** to ``auditlog`` / ``audit_context`` from the app
   role are revoked, so a compromised application cannot forge rows by
   issuing its own ``INSERT INTO auditlog``.
2. **Audit trigger functions** are declared ``SECURITY DEFINER`` so
   they run with the privileges of their **owner** (typically the
   admin role that ran the migration), not with the privileges of the
   calling app role. The trigger fires from a regular ``INSERT`` /
   ``UPDATE`` / ``DELETE`` on a tracked table, but the actual write
   into ``auditlog`` happens under the admin's privileges.

This module generates the SQL that wires up that split. The recommended
deployment model is:

* ``myapp_admin`` — runs migrations, owns the audit trigger functions,
  has full privileges on ``auditlog`` / ``audit_context``, used by
  retention / purge cron jobs
* ``myapp`` — the runtime application role, can read and append to
  tracked tables, has **no direct write access** to ``auditlog`` or
  ``audit_context``; produces audit rows only through the
  ``SECURITY DEFINER`` trigger functions

See :doc:`../../docs/hardening.md` for the full deployment guide.
"""

from auditrum.tracking.spec import validate_identifier

__all__ = [
    "generate_grant_admin_sql",
    "generate_revoke_sql",
]


def generate_revoke_sql(
    table_name: str = "auditlog",
    app_role: str | None = None,
    *,
    context_table: str = "audit_context",
) -> str:
    """Generate SQL revoking write privileges on the audit log and context tables.

    Revokes ``INSERT``, ``UPDATE``, ``DELETE``, ``TRUNCATE`` on both
    ``table_name`` and ``context_table`` from ``PUBLIC``. If ``app_role``
    is provided, revokes from that role specifically as well — this
    protects against an app role that has been explicitly granted
    privileges via a ``GRANT … TO myapp`` elsewhere in the migration.

    After running this SQL, direct writes from the app role are blocked.
    Audit rows continue to flow because the trigger functions installed
    by :mod:`auditrum.tracking` are ``SECURITY DEFINER`` and run under
    the privileges of their owner (the migration role).
    """
    validate_identifier(table_name, "table_name")
    validate_identifier(context_table, "context_table")
    tables = (table_name, context_table)
    parts: list[str] = []
    for tbl in tables:
        parts.append(f"REVOKE INSERT, UPDATE, DELETE, TRUNCATE ON {tbl} FROM PUBLIC;")
    if app_role is not None:
        validate_identifier(app_role, "app_role")
        for tbl in tables:
            parts.append(f"REVOKE INSERT, UPDATE, DELETE, TRUNCATE ON {tbl} FROM {app_role};")
    # Readability of the audit log is usually still desired for the app role
    # (for in-app history views via AuditLog.objects...). SELECT is not touched.
    return "\n".join(parts)


def generate_grant_admin_sql(
    table_name: str,
    admin_role: str,
    *,
    context_table: str = "audit_context",
) -> str:
    """Grant full write privileges on both audit tables to a dedicated admin role.

    Pair with :func:`generate_revoke_sql` so the admin role (typically
    ``<app>_admin``) can drop partitions, purge old rows, and run
    retention jobs, while the regular app role can only append new
    entries through the trigger functions.

    The granted privileges include ``INSERT`` so the admin role can be
    the owner of the trigger functions (which run ``SECURITY DEFINER``),
    and ``UPDATE, DELETE, TRUNCATE`` for maintenance.

    Also grants ``USAGE`` on ``<table>_id_seq`` so the admin role can
    actually INSERT — direct INSERTs into ``auditlog`` consume the
    serial-PK sequence, and Postgres requires ``USAGE`` on it
    independently of the table-level grant.
    """
    validate_identifier(table_name, "table_name")
    validate_identifier(context_table, "context_table")
    validate_identifier(admin_role, "admin_role")
    return "\n".join(
        [
            f"GRANT SELECT, INSERT, UPDATE, DELETE, TRUNCATE, REFERENCES, TRIGGER "
            f"ON {table_name} TO {admin_role};",
            f"GRANT SELECT, INSERT, UPDATE, DELETE, TRUNCATE, REFERENCES, TRIGGER "
            f"ON {context_table} TO {admin_role};",
            f"GRANT USAGE, SELECT ON SEQUENCE {table_name}_id_seq TO {admin_role};",
        ]
    )
