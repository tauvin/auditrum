from datetime import UTC, datetime

from dateutil.relativedelta import relativedelta

from auditrum.tracking.spec import validate_identifier

__all__ = [
    "generate_audit_attach_context_sql",
    "generate_audit_context_table_sql",
    "generate_audit_current_user_id_sql",
    "generate_audit_reconstruct_sql",
    "generate_auditlog_partitions_sql",
    "generate_auditlog_table_sql",
    "generate_jsonb_diff_function_sql",
]


def generate_audit_context_table_sql(context_table: str = "audit_context") -> str:
    """Generate SQL for the context table that groups events by request/job/command.

    Rows are inserted lazily by :func:`generate_audit_attach_context_sql` only when
    an audit event actually fires, so read-only requests do not pay any write cost.
    """
    validate_identifier(context_table, "context_table")
    return f"""
CREATE TABLE IF NOT EXISTS {context_table} (
    id uuid PRIMARY KEY,
    metadata jsonb NOT NULL DEFAULT '{{}}'::jsonb,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS {context_table}_created_at_idx
    ON {context_table} (created_at);
CREATE INDEX IF NOT EXISTS {context_table}_metadata_gin_idx
    ON {context_table} USING GIN (metadata);
""".strip()


def generate_audit_attach_context_sql(
    context_table: str = "audit_context",
    guc_id: str = "auditrum.context_id",
    guc_metadata: str = "auditrum.context_metadata",
) -> str:
    """Generate the ``_audit_attach_context()`` PL/pgSQL upsert function.

    Audit triggers call this function on every event. It reads the current
    session's ``context_id`` and ``context_metadata`` GUCs (set per query by
    :mod:`auditrum.integrations.django.runtime`), upserts a row into the
    context table, and returns the UUID. Returns NULL if no context is set,
    so triggers fired outside a request boundary still work.

    The function is ``SECURITY DEFINER`` so it runs under the privileges
    of the admin role that owns it (typically the role that ran the
    migration). This is what allows the regular app role to have
    ``INSERT`` on ``audit_context`` revoked while audit rows still flow.
    """
    from auditrum.tracking._template import render

    validate_identifier(context_table, "context_table")
    return render(
        "audit_attach_context.sql",
        context_table=context_table,
        guc_id=guc_id,
        guc_metadata=guc_metadata,
    ).strip()


def generate_audit_reconstruct_sql(audit_table: str = "auditlog") -> str:
    """Generate the ``_audit_reconstruct_row`` and ``_audit_reconstruct_table``
    SQL helpers used by time-travel queries.

    ``_audit_reconstruct_row(table, object_id, at)`` returns the full row
    as it existed at ``at``, or ``NULL`` if the row didn't exist (before
    its INSERT or after its DELETE).

    ``_audit_reconstruct_table(table, at)`` returns every surviving row
    in the given tracked table at the target timestamp, as ``(object_id,
    row_data jsonb)`` pairs — DELETE'd rows are filtered out.

    Both rely on the ``(table_name, object_id, changed_at DESC)``
    composite index for fast lookups and work on arbitrary historical
    timestamps without a separate temporal_tables extension.
    """
    from auditrum.tracking._template import render

    validate_identifier(audit_table, "audit_table")
    row_sql = render("audit_reconstruct_row.sql", audit_table=audit_table)
    table_sql = render("audit_reconstruct_table.sql", audit_table=audit_table)
    return f"{row_sql.strip()}\n\n{table_sql.strip()}"


def generate_audit_current_user_id_sql(
    guc_metadata: str = "auditrum.context_metadata",
) -> str:
    """Generate the ``_audit_current_user_id()`` PL/pgSQL helper.

    Extracts the integer ``user_id`` from the current session's
    ``context_metadata`` JSONB GUC. Lets audit triggers store a typed
    ``auditlog.user_id`` column instead of forcing every user-facing query
    through ``context.metadata->>'user_id'`` jsonb traversals. Returns NULL
    when the GUC is unset, missing the key, or non-coercible.

    Also ``SECURITY DEFINER`` — matches the privilege model used by
    :func:`generate_audit_attach_context_sql`.
    """
    from auditrum.tracking._template import render

    return render("audit_current_user_id.sql", guc_metadata=guc_metadata).strip()


def generate_auditlog_table_sql(table_name: str = "auditlog") -> str:
    validate_identifier(table_name, "table_name")
    return f"""
CREATE TABLE IF NOT EXISTS {table_name} (
    id serial,
    operation text NOT NULL,
    changed_at timestamp with time zone NOT NULL DEFAULT now(),
    content_type_id integer,
    object_id text,
    table_name text NOT NULL,
    user_id integer,
    old_data jsonb,
    new_data jsonb,
    diff jsonb,
    context_id uuid,
    meta jsonb
) PARTITION BY RANGE (changed_at);

CREATE INDEX IF NOT EXISTS {table_name}_id_idx ON {table_name} (id);

-- Default partition catches any row outside of month-partitioned ranges,
-- so a missing cron job does not break writes to tracked tables.
CREATE TABLE IF NOT EXISTS {table_name}_default
    PARTITION OF {table_name} DEFAULT;

-- Primary lookup index: "history of this record across time" and "history of
-- this table" both hit it via leftmost-prefix. Replaces separate
-- table_name_idx and object_id_idx.
CREATE INDEX IF NOT EXISTS {table_name}_target_idx
    ON {table_name} (table_name, object_id, changed_at DESC);

CREATE INDEX IF NOT EXISTS {table_name}_user_id_idx ON {table_name} (user_id);
CREATE INDEX IF NOT EXISTS {table_name}_changed_at_idx ON {table_name} (changed_at);
CREATE INDEX IF NOT EXISTS {table_name}_context_id_idx ON {table_name} (context_id);
CREATE INDEX IF NOT EXISTS {table_name}_diff_gin_idx ON {table_name} USING GIN (diff);
""".strip()


def generate_auditlog_partitions_sql(table_name: str = "auditlog", months_ahead: int = 3) -> str:
    validate_identifier(table_name, "table_name")
    now = datetime.now(UTC).replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    parts = []

    for i in range(months_ahead):
        start = now + relativedelta(months=i)
        end = start + relativedelta(months=1)
        start_str = start.strftime("%Y-%m-%d")
        end_str = end.strftime("%Y-%m-%d")
        suffix = start.strftime("p%Y_%m")

        parts.append(
            f"""
CREATE TABLE IF NOT EXISTS {table_name}_{suffix}
    PARTITION OF {table_name}
    FOR VALUES FROM ('{start_str}') TO ('{end_str}');
""".strip()
        )

    return "\n\n".join(parts)


def generate_jsonb_diff_function_sql() -> str:
    return """
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
""".strip()
