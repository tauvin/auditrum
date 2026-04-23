# Architecture

Under the hood: schema, indexes, trigger flow, context propagation,
drift detection, concurrency. Read this if you want to understand
auditrum, extend it, or debug weird behaviour.

## Schema overview

```
┌──────────────────────────────────────┐       ┌──────────────────────────┐
│  audit_context                       │       │  auditlog  (PARTITIONED) │
│  ─────────────────                   │       │  ────────                │
│  id          uuid PRIMARY KEY        │◄──────┤  context_id   uuid       │
│  metadata    jsonb                   │       │  id           serial     │
│  created_at  timestamptz             │       │  operation    text       │
│  updated_at  timestamptz             │       │  changed_at   timestamptz│
└──────────────────────────────────────┘       │  table_name   text       │
                                                │  object_id    text       │
┌──────────────────────────────────────┐       │  user_id      integer    │
│  auditrum_applied_triggers           │       │  old_data     jsonb      │
│  ─────────────────                   │       │  new_data     jsonb      │
│  trigger_name   text PRIMARY KEY     │       │  diff         jsonb      │
│  table_name     text                 │       │  meta         jsonb      │
│  checksum       text                 │       │  row_hash     text?      │
│  applied_at     timestamptz          │       │  prev_hash    text?      │
│  spec_fingerprint  jsonb             │       └──────────┬───────────────┘
└──────────────────────────────────────┘                  │
                                                          ▼
                                           ┌────────────────────────────┐
                                           │  auditlog partitions       │
                                           │  auditlog_p2026_04         │
                                           │  auditlog_p2026_05         │
                                           │  ...                       │
                                           │  auditlog_default  <- catch-all│
                                           └────────────────────────────┘
```

Three tables plus the tracked tables you opt in. `auditlog` is
partitioned by `changed_at` with a DEFAULT partition as safety net.
`audit_context` is unpartitioned because it's small — one row per
logical unit of work that actually produced at least one audit event.
`auditrum_applied_triggers` is a single-row-per-trigger registry
managed by `TriggerManager`.

### Why partitioning

Audit logs are the textbook use case for range partitioning by time:

- **Cheap retention.** Dropping a month of data is a metadata
  operation. `auditrum purge --drop-partitions "2 years"` drops 24
  tables; nothing is written to WAL.
- **Cheap pruning.** Queries like `SELECT … WHERE changed_at > ?` only
  scan partitions whose bound overlaps the range.
- **Index-per-partition.** Each partition has its own indexes, so
  vacuum, reindex, and stats are bounded.
- **Default partition as safety net.** Deliberate — if your cron job
  that creates next month's partition fails, writes still succeed. You
  lose the partitioning optimisation temporarily but not data.

## Indexes

```sql
-- Composite target index: "history of this table", "history of this row",
-- and "history of this row's column timeline" all hit this.
CREATE INDEX auditlog_target_idx
    ON auditlog (table_name, object_id, changed_at DESC);

CREATE INDEX auditlog_id_idx          ON auditlog (id);
CREATE INDEX auditlog_changed_at_idx  ON auditlog (changed_at);
CREATE INDEX auditlog_user_id_idx     ON auditlog (user_id);
CREATE INDEX auditlog_context_id_idx  ON auditlog (context_id);
CREATE INDEX auditlog_diff_gin_idx    ON auditlog USING GIN (diff);

-- audit_context
CREATE INDEX audit_context_created_at_idx ON audit_context (created_at);
CREATE INDEX audit_context_metadata_gin_idx ON audit_context USING GIN (metadata);
```

The **composite `target_idx`** is the one to understand. It's sorted
by `(table_name, object_id, changed_at DESC)` which means:

| Query pattern                                                 | Index behaviour          |
|---------------------------------------------------------------|--------------------------|
| `WHERE table_name = X`                                         | leftmost-prefix range scan |
| `WHERE table_name = X AND object_id = Y`                       | two-leftmost prefix scan |
| `WHERE table_name = X AND object_id = Y ORDER BY changed_at DESC` | index-only scan, no sort |
| `WHERE object_id = X` (no table_name)                          | full scan — never do this |

That last one is intentional: **`object_id` alone is meaningless**
across tables — row `42` in `orders` has nothing to do with row `42`
in `users`. Forcing a `table_name` prefix avoids accidental cross-
table scans.

The `(changed_at DESC)` direction is important: the most common query
is "most recent events first", so the index is pre-sorted that way and
no sort step is needed at query time.

### GIN on `diff`

`diff` is the jsonb blob of changed columns. Each entry is **paired** —
``{field: {"old": <before>, "new": <after>}}`` — so UI consumers can
render a full ``old → new`` diff without cross-referencing
``old_data``, and GIN containment queries can target either side:

```sql
-- events where status specifically moved to 'paid' (ignoring the old value)
SELECT * FROM auditlog
WHERE diff @> '{"status": {"new": "paid"}}'::jsonb;

-- events where status was 'pending' before the change
SELECT * FROM auditlog
WHERE diff @> '{"status": {"old": "pending"}}'::jsonb;
```

INSERT rows carry ``{"old": null, "new": <value>}`` per column;
DELETE rows carry ``{"old": <value>, "new": null}``. One uniform
shape across every operation — no special-casing in consumers.

`meta` does **not** have a GIN index by default, because per-row
extras are usually low-cardinality and seldom queried. Add one
manually if your workload needs it.

## The audit trigger

For a tracked table `orders`, the generator produces something like:

```sql
CREATE OR REPLACE FUNCTION audit_orders_trigger() RETURNS trigger AS $$
DECLARE
    data JSONB;
    diff JSONB;
    ignored_keys TEXT[] := ARRAY['password']::text[];   -- from field filter
    old_filtered jsonb := to_jsonb(OLD);
    new_filtered jsonb := to_jsonb(NEW);
    key text;
BEGIN
    FOREACH key IN ARRAY ignored_keys LOOP
        old_filtered := old_filtered - key;
        new_filtered := new_filtered - key;
    END LOOP;

    IF (TG_OP = 'UPDATE') THEN
        -- jsonb_diff returns {field: {old, new}} for fields that changed
        diff = jsonb_diff(old_filtered, new_filtered);
        IF diff IS NULL THEN RETURN NULL; END IF;
    ELSIF TG_OP = 'INSERT' THEN
        -- wrap each column value as {old: null, new: value}
        diff = (
            SELECT jsonb_object_agg(k, jsonb_build_object('old', NULL, 'new', v))
            FROM jsonb_each(new_filtered) AS t(k, v)
        );
    ELSIF TG_OP = 'DELETE' THEN
        diff = (
            SELECT jsonb_object_agg(k, jsonb_build_object('old', v, 'new', NULL))
            FROM jsonb_each(old_filtered) AS t(k, v)
        );
    END IF;

    IF (TG_OP = 'DELETE') THEN
        data = to_jsonb(OLD);
    ELSE
        data = to_jsonb(NEW);
    END IF;

    INSERT INTO auditlog (
        operation, changed_at, object_id, table_name,
        user_id, old_data, new_data, diff, context_id, meta
    ) VALUES (
        TG_OP, now(), NULL, TG_TABLE_NAME,
        _audit_current_user_id(),
        CASE WHEN TG_OP IN ('UPDATE', 'DELETE') THEN to_jsonb(OLD) ELSE NULL END,
        CASE WHEN TG_OP IN ('UPDATE', 'INSERT') THEN to_jsonb(NEW) ELSE NULL END,
        diff,      -- paired shape, written for every operation
        _audit_attach_context(),
        NULL
    );
    RETURN NULL;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER audit_orders_trigger
AFTER INSERT OR UPDATE OR DELETE ON orders
FOR EACH ROW EXECUTE FUNCTION audit_orders_trigger();
```

Notable decisions:

1. **AFTER row trigger**, not statement trigger. We need the per-row
   diff, and trigger overhead on the row scale is acceptable for
   typical OLTP workloads. Bulk loads that insert millions of rows
   will amplify the audit cost — consider disabling the trigger for
   those windows with `ALTER TABLE … DISABLE TRIGGER`.
2. **`jsonb_diff` is our own function**, not built-in. It lives
   alongside the trigger as part of the bootstrap SQL. The reason to
   keep it Python-generated rather than an extension is portability —
   works on any managed Postgres (RDS, Cloud SQL, etc.) without
   requiring `CREATE EXTENSION`.
3. **`to_jsonb(OLD)` / `to_jsonb(NEW)` stores the full row**, not just
   the filtered diff. This is what makes time travel work — the
   reconstruct functions don't need the diff, they just take the most
   recent `new_data`.
4. **`diff IS NULL` early return** for UPDATE. If the filtered `diff`
   is empty (every changed column was in `ignored_keys`), we skip the
   INSERT entirely. No rows in `auditlog`, no `audit_context` upsert.
   Silent noise reduction for workloads where the tracked column
   rarely actually changes.
5. **`_audit_attach_context()` is called only when inserting**, and
   returns NULL when no context is active. That's why the context
   table is populated **lazily**.
6. **`RETURN NULL`** at the end — trigger is AFTER, the return value
   is discarded. Non-NULL would have the same effect.

## Context propagation pipeline

The trickiest part of the system. High level:

```
Python: request starts
  │
  ▼
AuditrumMiddleware enters `auditrum_context(**metadata)` block
  │
  ▼
ContextVar is populated with {id: uuid, metadata: {...}}
  │
  ▼
connection.execute_wrapper installs `_inject_audit_context` for this request
  │
  ▼
────────────────────────────── every cursor.execute ──────────────────────────────
  │
  ▼
_inject_audit_context(execute, sql, params, many, context)
  │ Checks: is tracker set? is SQL injectable (not SELECT/VACUUM/...)? is cursor named?
  │ Builds prefix: "SELECT set_config('auditrum.context_id', %s, true),
  │                        set_config('auditrum.context_metadata', %s, true); "
  │ Prepends to sql, prepends context_id + metadata_json to params
  │
  ▼
execute(prefixed_sql, prepended_params, many, context)
  │ (SA / Django / psycopg actually submits the statement)
  │
  ▼
─────────────────────────────── server side ──────────────────────────────────────
  │
  ▼
PG executes as a single statement:
  1. set_config('auditrum.context_id', <uuid>, true)     -> SET LOCAL-like
  2. set_config('auditrum.context_metadata', <json>, true)
  3. the real user SQL (INSERT / UPDATE / DELETE)
  │
  ▼
User SQL modifies a tracked row; audit trigger fires
  │
  ▼
audit_orders_trigger() calls _audit_attach_context()
  │
  ▼
_audit_attach_context() reads current_setting('auditrum.context_id'/metadata)
  Upserts into audit_context
  Returns uuid -> stored in auditlog.context_id
```

The critical insight is `is_local=true`. `set_config(name, value, true)`
sets the GUC **for the current transaction**. Because PG autocommit
wraps each `cursor.execute` in an implicit transaction, and because we
ship the `set_config` **in the same statement** as the user SQL, the
GUC is active for the user SQL and automatically resets at implicit
commit. No leak across requests on pooled connections.

### Why not just `SET LOCAL` at request start?

Because without `transaction.atomic()` the session is in autocommit
and `SET LOCAL`'s effect ends at the next statement boundary — before
any user SQL runs. Django has `ATOMIC_REQUESTS` to force per-request
transactions, but that changes application semantics (now failed
views roll back everything) and many projects can't opt into it.

The in-statement prefix approach sidesteps both problems: you don't
need atomic requests, you don't risk leaks.

### SQL filter rules

We only inject into statements that are:

- Not in the `IGNORED_SQL_PREFIXES` list (select, vacuum, analyze,
  create, alter, drop, begin, start, commit, rollback, savepoint,
  release, ...)
- Not running on a **named cursor** (Django uses named cursors for
  `.iterator()`, and PG rejects multi-statement SQL on named cursors)
- Not in an errored transaction (would fail anyway, no point)

For all other statements we unconditionally prefix. This is
essentially free — the PG parser has to parse the `set_config` call
but the actual function invocation is O(1).

## Drift detection

Every `TriggerManager.install(spec)` records the spec's SHA-256
checksum into `auditrum_applied_triggers.checksum`. On subsequent
`inspect(spec)` calls, the current spec's computed checksum is
compared against the stored one:

| Stored checksum | New checksum | Status                                |
|-----------------|--------------|---------------------------------------|
| (none)          | any          | `NOT_INSTALLED`                       |
| `X`             | `X`          | `INSTALLED`                           |
| `X`             | `Y`          | `DRIFT`                               |

`sync([specs])` runs `inspect` on each spec and installs missing or
drifted ones. With `prune=True`, it also removes tracked triggers
whose names don't appear in the incoming spec list.

Checksum is computed from the **rendered SQL**, so a change to the
template, the field filter, the `extra_meta`, or the log condition all
produce a different hash and trigger re-install.

## Concurrency

Advisory locks serialise install/uninstall at the trigger level. Every
mutation on `auditrum_applied_triggers` is preceded by:

```sql
SELECT pg_advisory_xact_lock(hashtext('audit_orders_trigger'));
```

`pg_advisory_xact_lock` auto-releases at transaction end (commit or
rollback). Two concurrent deploys both trying to install
`audit_orders_trigger` serialise on this lock — neither sees the
other's partially-written state. Different triggers don't block each
other (different hashtexts).

This means **you don't need a deployment lock for audit triggers**.
The concurrency model is "PG decides". If a second deploy actually
contains the same spec (same checksum), it's a no-op after acquiring
the lock.

## File layout

```
auditrum/
├── __init__.py            # Public API re-exports
├── cli.py                 # Typer app, every CLI subcommand
├── triggers.py            # Legacy facade over auditrum.tracking.TrackSpec
├── schema.py              # generate_*_sql() functions for the fixed schema
├── executor.py            # ConnectionExecutor protocol + Null/Psycopg impls
├── context.py             # Legacy AuditContext (superseded by integrations/*/runtime)
├── timetravel.py          # reconstruct_row/_table/_field_history + HistoricalRow
├── blame.py               # fetch_blame + format_blame (used by CLI blame)
├── revert.py              # generate_revert_sql_from_log
├── hardening.py           # generate_revoke_sql + generate_grant_admin_sql
├── hash_chain.py          # generate_hash_chain_sql + verify_chain
├── retention.py           # generate_purge_sql + drop_old_partitions
│
├── tracking/
│   ├── __init__.py        # TrackSpec, FieldFilter, TriggerBundle, TriggerManager exports
│   ├── spec.py            # TrackSpec, FieldFilter, TriggerBundle dataclasses
│   ├── manager.py         # TriggerManager, TriggerStatus, TriggerAction, SyncReport
│   ├── _template.py       # _StrictMap + render() helpers
│   └── templates/
│       ├── audit_trigger.sql              # The AFTER row trigger body
│       ├── audit_attach_context.sql       # _audit_attach_context() function
│       ├── audit_current_user_id.sql      # _audit_current_user_id() function
│       ├── audit_reconstruct_row.sql      # _audit_reconstruct_row() function
│       └── audit_reconstruct_table.sql    # _audit_reconstruct_table() function
│
├── integrations/
│   ├── django/
│   │   ├── __init__.py                    # lazy __getattr__ re-exports
│   │   ├── apps.py                        # AppConfig.ready wires up DjangoExecutor
│   │   ├── executor.py                    # DjangoExecutor
│   │   ├── runtime.py                     # auditrum_context + _inject_audit_context wrapper
│   │   ├── middleware.py                  # AuditrumMiddleware, RequestIDMiddleware
│   │   ├── tracking.py                    # @track decorator + per-process registry
│   │   ├── operations.py                  # InstallTrigger/UninstallTrigger migration ops
│   │   ├── audit.py                       # Legacy register() facade
│   │   ├── models.py                      # AuditLog, AuditContext, AuditLogManager
│   │   ├── mixins.py                      # AuditedModelMixin, AuditHistoryMixin
│   │   ├── admin.py                       # Django admin registration
│   │   ├── settings.py                    # audit_settings proxy over Django settings
│   │   ├── templatetags/                  # {% audit_diff %} template filter
│   │   ├── migrations/                    # 0001_initial (bootstrap schema)
│   │   └── management/commands/
│   │       ├── auditrum_makemigrations.py # generate migrations for @track specs
│   │       └── audit_add_partitions.py    # monthly partition helper
│   │
│   └── sqlalchemy/
│       ├── __init__.py                    # Public re-exports
│       └── core.py                        # SQLAlchemyExecutor, track_table, sync
│
└── observability/
    ├── __init__.py
    ├── otel.py                            # enrich_metadata()
    ├── prometheus.py                      # AuditrumCollector
    └── sentry.py                          # add_breadcrumb_for_context()
```

## Test organisation

```
tests/
├── test_tracking_spec.py              # TrackSpec, FieldFilter, TriggerBundle
├── test_tracking_manager.py           # TriggerManager with a fake executor
├── test_django_runtime.py             # auditrum_context + inject wrapper
├── test_django_operations.py          # InstallTrigger/UninstallTrigger migration ops
├── test_django_tracking_decorator.py  # @track decorator + auditrum_makemigrations
├── test_django_models.py              # AuditLogManager / AuditLogQuerySet
├── test_mixins.py                     # AuditedModelMixin helpers
├── test_sqlalchemy_integration.py     # SQLAlchemyExecutor + track_table
├── test_blame.py                      # fetch_blame / format_blame
├── test_timetravel.py                 # reconstruct_row/_table/_field_history
├── test_historical_row.py             # HistoricalRow dataclass behaviour
├── test_schema.py                     # generate_*_sql functions
├── test_triggers.py                   # legacy triggers.py facade
├── test_revert.py                     # generate_revert_sql
├── test_hardening.py                  # REVOKE / GRANT generation
├── test_hash_chain.py                 # hash chain SQL generation
├── test_retention.py                  # purge SQL, interval parsing
├── test_context.py                    # legacy AuditContext
├── test_executor.py                   # ConnectionExecutor protocol
├── test_utils.py                      # audit_tracked contextmanager
├── test_cli.py                        # Typer CliRunner smoke tests
├── test_observability.py              # OTel/Prometheus/Sentry helpers
│
└── integration/                        # testcontainers[postgres] — skipped without Docker
    ├── test_trigger_roundtrip.py
    ├── test_revert_pg.py
    ├── test_hardening_pg.py            # REVOKE + hash chain + retention on real PG
    ├── test_partitions.py
    ├── test_manager_pg.py              # composite index EXPLAIN verification
    ├── test_timetravel_pg.py           # full row lifecycle + reconstruct
    └── test_blame_pg.py                # blame against real row history
```

Unit tests run in ~0.4s. Integration tests require Docker; they're
automatically skipped when the docker socket isn't reachable.

## Performance notes

- **Trigger overhead**: per-row AFTER triggers add ~5-15% to write
  latency on typical OLTP workloads. Use `track_only=[...]` to reduce
  the amount of jsonb serialisation per row.
- **Context propagation adds one `set_config` call per SQL statement.**
  The call itself is O(1) in PG. The overhead is the wire traffic for
  the extra ~120 bytes per statement. Measurable but small.
- **Composite `target_idx` + partition pruning** means per-row history
  queries are O(log n) in the size of the relevant partition, not the
  size of the whole log. Scale to hundreds of millions of events
  without query regression.
- **Hash chain serialises inserts**. If you enable it, expect a
  throughput ceiling equal to "one audit insert at a time per
  partition". Measure before enabling on write-heavy workloads.

## What's next

- [Hardening](hardening.md) — the operational hardening story
- [Observability](observability.md) — metrics and tracing
- [CLI reference](cli.md) — all the commands you'll run in cron
