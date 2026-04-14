# Core concepts

A short tour of the moving parts. If you just want to ship, the
[getting started](getting-started.md) guide gives you working code
without reading any of this.

## The big picture

```
   your code
       |
       | INSERT / UPDATE / DELETE on tracked table
       v
 +-----------------+           +----------------------+
 | tracked table   |  fires    | audit trigger (plpgsql) |
 |   orders        +---------->|  - compute diff       |
 |   users         |           |  - attach context     |
 |   products      |           |  - INSERT into auditlog|
 +-----------------+           +----------+-----------+
                                          |
                                          v
 +----------------------+        +---------------+
 |  audit_context       |<-------+   auditlog    |  <- partitioned by changed_at
 |  (id uuid, metadata) |        |   (one row    |
 |  upserted lazily     |        |    per event) |
 +----------------------+        +---------------+
```

Every tracked table gets **one** AFTER-row trigger. That trigger writes
one row into a **single shared `auditlog` table** — not per-model event
tables. The row's `context_id` FK points at a row in `audit_context`
that holds the request/job-level metadata (user, IP, URL, trace id, ...).
Both tables are partitioned by time so retention and purging stay cheap
at scale.

## Why a single `auditlog` table

Most Python audit libraries build one event model per tracked model.
`auditrum` deliberately does not. A few reasons:

- **Schema immutability.** When you drop a field from `users`, a
  per-model event table either drops the column (losing history) or
  keeps a zombie. Our `jsonb old_data / new_data` columns survive
  refactors without touching historic rows.
- **Cross-model queries.** "All changes by user X across every tracked
  table in the last week" is a one-line `WHERE user_id = ? AND changed_at
  > ?` with a plain btree index. With per-model tables it's a `UNION`
  across however many audit tables you have.
- **One hash chain, one retention policy, one REVOKE.** The
  [hardening and compliance](hardening.md) story all presumes a single
  linear log. N independent chains of N independent tables give you
  neither global tamper detection nor a single purge operation.
- **Partitioning is easier.** One set of monthly partitions, one cron,
  one `auditrum purge` command.

The [audit-aware ORM](django.md#orm-helpers) helpers paper over the
mild UX gap (fast per-row history, typed filters) using the composite
`(table_name, object_id, changed_at DESC)` index.

## `TrackSpec` — what to audit

`TrackSpec` is a pure value object. Immutable, hashable, no side
effects. You describe a trigger declaratively and then hand the spec to
`TriggerManager` (or to one of the framework integrations, which wrap
this for you).

```python
from auditrum.tracking import TrackSpec, FieldFilter

spec = TrackSpec(
    table="orders",
    fields=FieldFilter.only("status", "total"),
    extra_meta_fields=("tenant_id",),
    log_condition="NEW.status <> 'draft'",
)
```

Three mutually-exclusive modes for the field filter:

| Constructor                  | Semantics                                  |
|------------------------------|--------------------------------------------|
| `FieldFilter.all()`          | diff every column (default)                |
| `FieldFilter.only(*names)`   | whitelist — diff only these                |
| `FieldFilter.exclude(*names)`| blacklist — diff everything except these   |

Identifiers (`table`, `audit_table`, `fields`, `extra_meta_fields`) are
validated against `^[A-Za-z_][A-Za-z0-9_]*$` in `__post_init__`, so
SQL-injection through user-supplied names fails at **construction**
time, not at SQL execution time.

`log_condition` is trusted PL/pgSQL and embedded into the trigger body
verbatim. Never interpolate untrusted input into it.

`spec.build()` renders the SQL and returns a `TriggerBundle`:

```python
bundle = spec.build()
print(bundle.trigger_name)    # audit_orders_trigger
print(bundle.install_sql)     # full CREATE OR REPLACE FUNCTION + CREATE TRIGGER
print(bundle.uninstall_sql)   # DROP TRIGGER + DROP FUNCTION
print(bundle.checksum)        # sha256 of install_sql — used for drift detection
```

`spec.to_fingerprint()` returns a JSON-safe dict for storing in the
tracking table. Useful when reconstructing state during `diff()`.

## `TriggerManager` — when things are installed

Where `TrackSpec` is the "what", `TriggerManager` is the "how and
when". It owns install / uninstall / drift detection / idempotent sync
against a live connection.

```python
from auditrum.executor import PsycopgExecutor
from auditrum.tracking import TriggerManager

mgr = TriggerManager(PsycopgExecutor(conn))
mgr.bootstrap()                    # creates auditrum_applied_triggers
mgr.install(spec)                  # or sync([...]) for batch
mgr.inspect(spec)                  # -> TriggerStatus.INSTALLED | DRIFT | NOT_INSTALLED
mgr.sync([spec1, spec2], prune=False)  # idempotent install/update
mgr.diff([spec1, spec2])           # dry-run: what would sync do?
mgr.uninstall(spec)
```

### Drift detection

On install, `TriggerManager` stores the spec's checksum and fingerprint
in `auditrum_applied_triggers`. On subsequent `inspect(spec)` calls it
compares the stored checksum against `spec.build().checksum`. If they
differ, the status is `DRIFT` — re-install will update the trigger
body.

### Concurrency

Each install / uninstall acquires a transaction-level advisory lock
keyed by `hashtext(trigger_name)`. Two parallel deploys racing on the
same trigger serialise cleanly; different triggers don't block each
other. You don't need a deploy-lock of your own.

### Framework independence

`TriggerManager` only needs a `ConnectionExecutor`. Four ship out of
the box:

| Executor                | Source module                                            | When to use                    |
|-------------------------|----------------------------------------------------------|--------------------------------|
| `NullExecutor`          | `auditrum.executor`                                      | default; no-op for tests/docs  |
| `PsycopgExecutor`       | `auditrum.executor`                                      | raw psycopg                    |
| `DjangoExecutor`        | `auditrum.integrations.django.executor`                  | Django (installed automatically by AppConfig.ready) |
| `SQLAlchemyExecutor`    | `auditrum.integrations.sqlalchemy`                       | SQLAlchemy Core/ORM            |

Write your own `ConnectionExecutor` by matching the protocol — a
callable `cursor()` that returns a context manager yielding something
with `execute(sql, params)` / `fetchone()` / `fetchall()`. That's it.

## Context propagation

Every audit row is tagged with a **context** — a UUID + JSONB metadata
blob that typically holds `user_id`, `username`, `source`, `request_id`,
`url`, `method`, plus anything you shove in. Context rows live in the
`audit_context` table and are **upserted lazily by the trigger** — a
read-only request that never writes to any tracked table produces zero
audit rows *and* zero context rows.

The propagation pipeline for Django:

```
AuditrumMiddleware.__call__(request)
  |
  v
auditrum_context(**metadata)  <- context manager, one per request
  |
  v
connection.execute_wrapper(_inject_audit_context)  <- Django hook
  |
  v
every user SQL statement is prefixed in-place with:
   SELECT set_config('auditrum.context_id', $1, true),
          set_config('auditrum.context_metadata', $2, true);
   <real SQL>
  |
  v
audit trigger fires, calls _audit_attach_context()
  |
  v
_audit_attach_context() reads both session GUCs, upserts into
audit_context, returns the UUID -> stored in auditlog.context_id
```

Two things worth understanding about this design:

1. **`is_local=true`** in the `set_config` call means the GUC is scoped
   to the **statement's transaction**. Because the `set_config` and the
   user's real SQL ship together as one statement, they share a
   transaction even in autocommit mode. This is how we guarantee GUCs
   cannot leak across requests on a pooled connection — no extra
   `transaction.atomic()` wrapping required.

2. **Lazy upsert.** The `audit_context` row is only written when a
   trigger actually fires. A `GET /health` endpoint that reads nothing
   from tracked tables produces no context row. This matters at scale:
   thousands of idle requests per second don't balloon the context table.

For non-Django frameworks you can either:

- **Roll your own wrapper**: apply `SELECT set_config(...)` before every
  cursor execute through whatever hook your framework provides.
- **Set the GUCs manually** per logical unit of work:

  ```python
  with conn.cursor() as cur:
      cur.execute(
          "SELECT set_config('auditrum.context_id', %s, false), "
          "set_config('auditrum.context_metadata', %s, false)",
          (str(uuid.uuid4()), json.dumps({"source": "cli", "user_id": 42})),
      )
      cur.execute("UPDATE orders SET status = 'paid' WHERE id = 1")
  ```

  Note `is_local=false` here because you're setting the value for the
  whole session, not a single statement.

## The `audit_context` table

```sql
CREATE TABLE audit_context (
    id          uuid PRIMARY KEY,
    metadata    jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at  timestamptz NOT NULL DEFAULT now(),
    updated_at  timestamptz NOT NULL DEFAULT now()
);
```

Every `auditlog` row that was produced inside an active context links
to one row here via `auditlog.context_id`. If you want to pull the full
event trail for a single request:

```sql
SELECT a.*
FROM auditlog a
WHERE a.context_id = '<ctx-uuid>'
ORDER BY a.changed_at;
```

Or via the Django manager:

```python
AuditLog.objects.for_context(ctx_uuid).order_by("changed_at")
```

## What's next

- [Django integration](django.md) — `@track`, middleware, migrations, ORM helpers
- [SQLAlchemy integration](sqlalchemy.md) — `track_table`, `sync`, Alembic
- [Time travel](time-travel.md) — reconstruct row state at any past timestamp
- [Architecture](architecture.md) — schema, indexes, performance considerations
