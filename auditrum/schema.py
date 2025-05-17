from datetime import UTC, datetime

from dateutil.relativedelta import relativedelta


def generate_auditlog_table_sql(table_name: str = "auditlog") -> str:
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
    meta jsonb,
    request_id text,
    change_reason text,
    source text
) PARTITION BY RANGE (changed_at);

CREATE INDEX IF NOT EXISTS {table_name}_id_idx ON {table_name} (id);

-- Initial partition
CREATE TABLE IF NOT EXISTS {table_name}_p0
    PARTITION OF {table_name}
    FOR VALUES FROM ('2000-01-01') TO ('2025-01-01');

-- Indexes
CREATE INDEX IF NOT EXISTS {table_name}_table_name_idx ON {table_name} (table_name);
CREATE INDEX IF NOT EXISTS {table_name}_user_id_idx ON {table_name} (user_id);
CREATE INDEX IF NOT EXISTS {table_name}_changed_at_idx ON {table_name} (changed_at);
CREATE INDEX IF NOT EXISTS {table_name}_object_id_idx ON {table_name} (object_id);
CREATE INDEX IF NOT EXISTS {table_name}_request_id_idx ON {table_name} (request_id);
CREATE INDEX IF NOT EXISTS {table_name}_source_idx ON {table_name} (source);
CREATE INDEX IF NOT EXISTS {table_name}_meta_gin_idx ON {table_name} USING GIN (meta);
CREATE INDEX IF NOT EXISTS {table_name}_diff_gin_idx ON {table_name} USING GIN (diff);
""".strip()


def generate_auditlog_partitions_sql(table_name: str = "auditlog", months_ahead: int = 3) -> str:
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


# Function to generate SQL for a custom jsonb_diff() function in PostgreSQL
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
