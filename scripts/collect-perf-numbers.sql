-- Performance numbers for docs/performance.md + case-studies/catalog.md.
--
-- Run on the production (or recently-restored staging) database after
-- at least a few days of representative traffic. Usage:
--
--     psql "$DATABASE_URL" -f scripts/collect-perf-numbers.sql \
--         -v retention='1 year' \
--         > perf-$(date +%Y%m%d).txt
--
-- Preconditions:
--   * track_functions = 'pl' in postgresql.conf (or per-session via SET).
--     Without this, pg_stat_user_functions is empty and section 1
--     returns zero rows.
--   * CREATE EXTENSION pg_stat_statements; (optional — section 4 is
--     gated via \if and skips cleanly if the extension is missing).
--
-- Numbers are snapshots of cumulative counters since the last stats
-- reset (pg_stat_reset()). If you want a clean window, reset stats
-- before the collection period:
--
--     SELECT pg_stat_reset();                     -- per-database counters
--     SELECT pg_stat_reset_shared('statements');  -- pg_stat_statements
--
-- Then wait for the traffic window you want to measure, and run this.
--
-- Interpreting the output:
--   * Section 1 feeds the "trigger overhead" row in performance.md.
--     self_ms per call IS the trigger cost — no subtraction needed.
--   * Section 2 feeds "audit events / second" and the volume table.
--   * Section 3 feeds "audit log size" and "retention lag".
--   * Section 4 feeds the "INSERT roundtrip delta" row — compare the
--     mean_exec_time of INSERTs on tracked tables against structurally
--     similar untracked tables.
--   * Section 5 is an integrity cross-check: if a tracked table
--     produced 100k INSERTs and auditlog has only 80k INSERT events
--     for it, the trigger is disabled on some rows (log_condition,
--     missing trigger, or the table was TRUNCATE'd without CASCADE).

\set retention '1 year'
\set ON_ERROR_STOP off

\echo '=== 1. Per-trigger function timings (pg_stat_user_functions) ==='
\echo ''
\echo 'Requires track_functions = pl. Reports cumulative time per'
\echo 'trigger function since the last pg_stat_reset(). Uses # as the'
\echo 'LIKE escape character so no backslash parsing surprises in psql.'
\echo ''
SELECT
    schemaname,
    funcname,
    calls,
    round(total_time::numeric, 2)   AS total_ms,
    round(self_time::numeric, 2)    AS self_ms,
    round((self_time / NULLIF(calls, 0))::numeric, 4) AS avg_ms_per_call,
    round((100.0 * self_time / NULLIF(sum(self_time) OVER (), 0))::numeric, 1)
        AS pct_of_all_audit_time
FROM pg_stat_user_functions
WHERE funcname LIKE 'audit#_%#_trigger' ESCAPE '#'
ORDER BY total_time DESC;

\echo ''
\echo '=== 2. Audit event rate and volume by (table, operation) ==='
\echo ''
\echo 'Last 7 days — adjust the interval if your window is different.'
\echo ''
SELECT
    table_name,
    operation,
    count(*)                        AS events,
    round(
        count(*)::numeric
        / GREATEST(extract(epoch FROM (max(changed_at) - min(changed_at))), 1),
        2
    )                               AS events_per_sec,
    min(changed_at)                 AS oldest,
    max(changed_at)                 AS newest
FROM auditlog
WHERE changed_at > now() - interval '7 days'
GROUP BY table_name, operation
ORDER BY events DESC;

\echo ''
\echo '=== 3. Disk footprint per auditlog partition ==='
\echo ''
SELECT
    inh.inhrelid::regclass          AS partition,
    pg_size_pretty(pg_total_relation_size(inh.inhrelid))
                                    AS total_size,
    pg_size_pretty(pg_relation_size(inh.inhrelid))
                                    AS heap_size,
    pg_size_pretty(pg_indexes_size(inh.inhrelid))
                                    AS indexes_size,
    pg_total_relation_size(inh.inhrelid)
                                    AS total_bytes
FROM pg_inherits inh
WHERE inh.inhparent = 'auditlog'::regclass
ORDER BY pg_total_relation_size(inh.inhrelid) DESC;

\echo ''
\echo '=== 3a. Retention lag (how far past the configured window) ==='
\echo ''
WITH oldest AS (
    SELECT min(changed_at) AS oldest_event FROM auditlog
)
SELECT
    oldest_event,
    now() - oldest_event                                AS audit_age,
    :'retention'                                        AS retention_window,
    (now() - oldest_event) - :'retention'::interval     AS lag_beyond_window
FROM oldest;

\echo ''
\echo '=== 3b. Average audit row on-disk size ==='
\echo ''
SELECT
    count(*)                                            AS rows,
    pg_size_pretty(pg_total_relation_size('auditlog'))  AS auditlog_total,
    pg_size_pretty(pg_total_relation_size('auditlog') / NULLIF(count(*), 0))
                                                        AS avg_bytes_per_row
FROM auditlog;

\echo ''
\echo '=== 4. INSERT roundtrip delta (pg_stat_statements) ==='
\echo ''
\echo 'Compare mean_exec_time of INSERTs against tracked vs untracked'
\echo 'tables. The delta is the trigger overhead as observed from the'
\echo 'application side (includes network roundtrip + trigger body).'
\echo ''
SELECT EXISTS(
    SELECT 1 FROM pg_extension WHERE extname = 'pg_stat_statements'
) AS has_pgss \gset

\if :has_pgss
    SELECT
        query,
        calls,
        round(mean_exec_time::numeric, 3)               AS mean_ms,
        round(
            (stddev_exec_time / NULLIF(mean_exec_time, 0))::numeric, 3
        )                                               AS cv,
        round(total_exec_time::numeric, 1)              AS total_ms
    FROM pg_stat_statements
    WHERE query ILIKE 'INSERT INTO %'
      AND query NOT ILIKE 'INSERT INTO auditlog%'
      AND query NOT ILIKE 'INSERT INTO audit_context%'
    ORDER BY total_exec_time DESC
    LIMIT 25;
\else
    \echo '>>> pg_stat_statements not installed — skipping section 4.'
    \echo '>>> Install with: CREATE EXTENSION pg_stat_statements;'
    \echo '>>> (also requires shared_preload_libraries = pg_stat_statements'
    \echo '>>>  in postgresql.conf and a PG restart).'
\endif

\echo ''
\echo '=== 5. Integrity cross-check: tracked INSERTs vs audit events ==='
\echo ''
\echo 'For each tracked table, compare live row count against INSERT'
\echo 'events in auditlog. A significant delta usually means a trigger'
\echo 'was disabled or the table was TRUNCATE-d without CASCADE.'
\echo ''
WITH tracked AS (
    SELECT
        table_name,
        count(*) FILTER (WHERE operation = 'INSERT')    AS audit_inserts,
        count(*) FILTER (WHERE operation = 'UPDATE')    AS audit_updates,
        count(*) FILTER (WHERE operation = 'DELETE')    AS audit_deletes
    FROM auditlog
    GROUP BY table_name
)
SELECT
    t.table_name,
    t.audit_inserts,
    t.audit_updates,
    t.audit_deletes,
    (SELECT reltuples::bigint
     FROM pg_class c
     WHERE c.relname = t.table_name)                    AS live_rows_estimate,
    CASE
        WHEN (SELECT reltuples
              FROM pg_class c
              WHERE c.relname = t.table_name) IS NULL
            THEN 'unknown table or deleted'
        WHEN t.audit_inserts
             < (SELECT reltuples
                FROM pg_class c
                WHERE c.relname = t.table_name) * 0.9
            THEN 'MISSING — fewer INSERTs than live rows'
        ELSE 'ok'
    END                                                 AS status
FROM tracked t
ORDER BY t.audit_inserts DESC;

\echo ''
\echo '=== 6. Hash chain status (if enabled) ==='
\echo ''
SELECT EXISTS(
    SELECT 1 FROM information_schema.columns
    WHERE table_name = 'auditlog' AND column_name = 'chain_seq'
) AS has_chain \gset

\if :has_chain
    SELECT
        count(*)                                        AS total_rows,
        count(*) FILTER (WHERE chain_seq IS NOT NULL)   AS chained_rows,
        count(*) FILTER (WHERE row_hash IS NOT NULL)    AS hashed_rows,
        max(chain_seq)                                  AS tip_chain_seq,
        max(changed_at) FILTER (WHERE chain_seq IS NOT NULL)
                                                        AS tip_changed_at
    FROM auditlog;
\else
    \echo '>>> hash chain not enabled on this auditlog.'
    \echo '>>> Enable with: auditrum enable-hash-chain'
\endif

\echo ''
\echo '=== 7. Context metadata: source / user distribution ==='
\echo ''
\echo 'Breakdown of where audit events come from (HTTP / Celery /'
\echo 'cron / shell / imports). Helps spot missing context wiring.'
\echo ''
SELECT
    metadata->>'source'                                 AS source,
    count(*)                                            AS contexts,
    (SELECT count(*)
     FROM auditlog a
     WHERE a.context_id = c.id)                         AS events_per_context
FROM audit_context c
WHERE c.created_at > now() - interval '7 days'
GROUP BY metadata->>'source', c.id
ORDER BY contexts DESC
LIMIT 50;

\echo ''
\echo '=== done ==='
