def generate_trigger_sql(
    table_name: str,
    audit_table: str = "auditlog",
    track_only: list[str] | None = None,
    exclude_fields: list[str] | None = None,
    log_conditions: str | None = None,
    meta_fields: list[str] | None = None,
) -> str:
    if track_only is not None and exclude_fields is not None:
        raise ValueError(
            f"Cannot specify both track_only and exclude_fields for table {table_name}"
        )

    if track_only is not None:
        track_only_keys = [f"'{k}'" for k in track_only]
        keys_tuple = "(" + ", ".join(track_only_keys) + ")"
        # We want to ignore keys NOT in track_only
        ignored_keys_expr = f"ARRAY(SELECT key FROM jsonb_object_keys(to_jsonb(NEW)) AS key(key) WHERE key.key NOT IN {keys_tuple})::text[]"
    elif exclude_fields is not None:
        ignored_keys = [f"'{k}'" for k in exclude_fields]
        ignored_keys_expr = f"ARRAY[{', '.join(ignored_keys)}]::text[]"
    else:
        ignored_keys_expr = "ARRAY[]::text[]"

    default_meta_fields = [
        "'username', current_setting('session.myapp_username', true)",
        "'client_ip', current_setting('session.myapp_client_ip', true)",
        "'user_agent', current_setting('session.myapp_user_agent', true)",
        "'session_key', current_setting('session.myapp_session_key', true)",
        "'source', current_setting('session.myapp_source', true)",
        "'request_id', current_setting('session.myapp_request_id', true)",
        "'change_reason', current_setting('session.myapp_change_reason', true)"
    ]

    if meta_fields:
        extra_fields = [f"'{field}', to_jsonb(NEW.{field})" for field in meta_fields]
        meta_fields_expr = ", ".join(default_meta_fields + extra_fields)
    else:
        meta_fields_expr = ", ".join(default_meta_fields)

    log_conditions_expr = f"""
    IF NOT ({log_conditions}) THEN
        RETURN NULL;
    END IF;
""" if log_conditions else ""

    function_name = f"audit_{table_name}_trigger"

    sql = f"""
CREATE OR REPLACE FUNCTION {function_name}() RETURNS trigger AS $$
DECLARE
    data JSONB;
    diff JSONB;
    ignored_keys TEXT[] := {ignored_keys_expr};
    old_filtered jsonb := to_jsonb(OLD);
    new_filtered jsonb := to_jsonb(NEW);
    key text;
BEGIN
{log_conditions_expr}
    FOREACH key IN ARRAY ignored_keys LOOP
        old_filtered := old_filtered - key;
        new_filtered := new_filtered - key;
    END LOOP;
    RAISE NOTICE 'IGNORED_KEYS: %', ignored_keys;
    
    IF (TG_OP = 'UPDATE') THEN
        diff = jsonb_strip_nulls(jsonb_diff(old_filtered, new_filtered));
        RAISE NOTICE 'Diff after filtering: %', diff;
        RAISE NOTICE 'Diff is null?: %',  diff is null;
        IF diff is null THEN
            RETURN NULL;
        END IF;
    ELSIF TG_OP = 'INSERT' THEN
        diff = new_filtered;
        IF diff is null THEN
            RETURN NULL;
        END IF;
    ELSIF TG_OP = 'DELETE' THEN
        diff = old_filtered;
        IF diff is null THEN
            RETURN NULL;
        END IF;
    END IF;

    IF (TG_OP = 'DELETE') THEN
        data = to_jsonb(OLD);
    ELSE
        data = to_jsonb(NEW);
    END IF;

    INSERT INTO {audit_table} (
        operation, changed_at, content_type_id, object_id, table_name,
        user_id, old_data, new_data, diff, meta, request_id, change_reason, source
    )
    VALUES (
        TG_OP, now(), NULL, NULL, TG_TABLE_NAME,
        NULL, 
        CASE WHEN TG_OP IN ('UPDATE', 'DELETE') THEN to_jsonb(OLD) ELSE NULL END,
        CASE WHEN TG_OP IN ('UPDATE', 'INSERT') THEN to_jsonb(NEW) ELSE NULL END,
        CASE WHEN TG_OP = 'UPDATE' THEN diff ELSE NULL END,
        jsonb_build_object({meta_fields_expr}),
        current_setting('session.myapp_request_id', true),
        current_setting('session.myapp_change_reason', true),
        current_setting('session.myapp_source', true)
    );

    RETURN NULL;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS {function_name} ON {table_name};

CREATE TRIGGER {function_name}
AFTER INSERT OR UPDATE OR DELETE ON {table_name}
FOR EACH ROW EXECUTE FUNCTION {function_name}();
"""
    print(f"Generated trigger SQL for table '{table_name}':\n{sql}")
    return sql.strip()
