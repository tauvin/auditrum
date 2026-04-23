CREATE OR REPLACE FUNCTION {function_name}() RETURNS trigger AS $$
DECLARE
    data JSONB;
    diff JSONB;
    ignored_keys TEXT[] := {ignored_keys_expr};
    old_filtered jsonb := to_jsonb(OLD);
    new_filtered jsonb := to_jsonb(NEW);
    key text;
BEGIN
{log_conditions_block}
    FOREACH key IN ARRAY ignored_keys LOOP
        old_filtered := old_filtered - key;
        new_filtered := new_filtered - key;
    END LOOP;

    IF (TG_OP = 'UPDATE') THEN
        diff = jsonb_diff(old_filtered, new_filtered);
        IF diff is null THEN
            RETURN NULL;
        END IF;
    ELSIF TG_OP = 'INSERT' THEN
        diff = (
            SELECT jsonb_object_agg(
                k, jsonb_build_object('old', NULL, 'new', v)
            )
            FROM jsonb_each(new_filtered) AS t(k, v)
        );
        IF diff is null THEN
            RETURN NULL;
        END IF;
    ELSIF TG_OP = 'DELETE' THEN
        diff = (
            SELECT jsonb_object_agg(
                k, jsonb_build_object('old', v, 'new', NULL)
            )
            FROM jsonb_each(old_filtered) AS t(k, v)
        );
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
        operation, changed_at, object_id, table_name,
        user_id, old_data, new_data, diff, context_id, meta
    )
    VALUES (
        TG_OP, now(),
        CASE
            WHEN TG_OP = 'DELETE' THEN to_jsonb(OLD)->>'id'
            ELSE to_jsonb(NEW)->>'id'
        END,
        TG_TABLE_NAME,
        _audit_current_user_id(),
        CASE WHEN TG_OP IN ('UPDATE', 'DELETE') THEN to_jsonb(OLD) ELSE NULL END,
        CASE WHEN TG_OP IN ('UPDATE', 'INSERT') THEN to_jsonb(NEW) ELSE NULL END,
        diff,
        _audit_attach_context(),
        {meta_expr}
    );

    RETURN NULL;
END;
$$ LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, public;

DROP TRIGGER IF EXISTS {trigger_name} ON {table_name};

CREATE TRIGGER {trigger_name}
AFTER INSERT OR UPDATE OR DELETE ON {table_name}
FOR EACH ROW EXECUTE FUNCTION {function_name}();
