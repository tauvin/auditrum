# SQLAlchemy integration

Thin bridge on top of the framework-agnostic `auditrum.tracking` core.
The same `TrackSpec` / `TriggerManager` drive both Django and
SQLAlchemy paths, so behaviour is identical — only the plumbing
differs.

## Install

```bash
pip install "auditrum[sqlalchemy]"
```

Ships as an optional extra. `auditrum[sqlalchemy]` does **not**
install Django or observability libraries — extras are independent.

## What you get

```python
from auditrum.integrations.sqlalchemy import (
    SQLAlchemyExecutor,     # ConnectionExecutor wrapping sqlalchemy.engine.Connection
    track_table,            # declarative helper for SA Table objects
    sync,                   # one-line idempotent TriggerManager.sync
    bootstrap_schema,       # installs auditlog + context + helpers + partitions
    registered_specs,       # list of currently registered TrackSpecs
    clear_registry,         # test-only
)
```

## Declaring tracked tables

```python
from sqlalchemy import Column, Integer, MetaData, String, Table
from auditrum.integrations.sqlalchemy import track_table

meta = MetaData()

orders = Table(
    "orders", meta,
    Column("id", Integer, primary_key=True),
    Column("status", String),
    Column("total", Integer),
    Column("tenant_id", Integer),
)

users = Table(
    "users", meta,
    Column("id", Integer, primary_key=True),
    Column("email", String),
    Column("name", String),
)

track_table(orders, fields=["status", "total"], extra_meta=["tenant_id"])
track_table(users, exclude=["name"])
```

`track_table(Table, **kwargs)` accepts the same arguments as the
Django `@track` decorator:

| Argument        | Meaning                                                            |
|-----------------|--------------------------------------------------------------------|
| `fields`        | Whitelist                                                          |
| `exclude`       | Blacklist (mutually exclusive with `fields`)                       |
| `extra_meta`    | NEW.* references captured into `auditlog.meta` jsonb               |
| `log_condition` | Trusted PL/pgSQL expression controlling whether the trigger fires  |
| `audit_table`   | Destination audit log table (default `"auditlog"`)                 |
| `trigger_name`  | Override default `audit_<table>_trigger` name                      |

It returns the `TrackSpec` (handy for inspection) and stores it in a
module-level registry keyed by table name.

## Bootstrap and sync

On app startup, run once:

```python
from sqlalchemy import create_engine
from auditrum.integrations.sqlalchemy import bootstrap_schema, sync

engine = create_engine("postgresql+psycopg://user:pw@host/db")
meta.create_all(engine)       # create YOUR tables first
bootstrap_schema(engine)       # idempotent: auditlog, audit_context, helper functions
sync(engine)                   # idempotent: install triggers for all track_table() calls
```

`bootstrap_schema` issues `CREATE TABLE IF NOT EXISTS` for the audit
log, the context table, and the partition defaults, plus `CREATE OR
REPLACE FUNCTION` for the four helper functions
(`_audit_attach_context`, `_audit_current_user_id`,
`_audit_reconstruct_row`, `_audit_reconstruct_table`, `jsonb_diff`).
Safe to call on every boot.

`sync(engine)` delegates to `TriggerManager.sync` with a
`SQLAlchemyExecutor` around a transaction-scoped connection. It
bootstraps its own `auditrum_applied_triggers` tracking table (one-time
cost) and then installs, updates, or skips each registered spec based
on checksum drift:

```python
from auditrum.integrations.sqlalchemy import sync

report = sync(engine)
print(report.installed)      # list of trigger names freshly installed
print(report.updated)        # list of trigger names with drifted bodies
print(report.skipped)        # list of trigger names already up to date
print(report.uninstalled)    # only populated when called with prune=True
```

`sync(engine, prune=True)` drops triggers that exist in the tracking
table but are not in the current registry — useful for "config as
code" deployments where the registry is the sole source of truth. Off
by default to avoid accidental removals.

## Context propagation

Unlike the Django middleware, the SQLAlchemy path does **not**
automatically wire up context propagation per query. You control it
explicitly. Two patterns work well:

### Pattern 1 — one context per logical transaction

```python
import json, uuid
from sqlalchemy import text

with engine.begin() as conn:
    metadata = {
        "source": "cli",
        "user_id": 42,
        "request_id": str(uuid.uuid4()),
    }
    conn.execute(
        text(
            "SELECT set_config('auditrum.context_id', :cid, false), "
            "set_config('auditrum.context_metadata', :md, false)"
        ),
        {"cid": str(uuid.uuid4()), "md": json.dumps(metadata)},
    )
    conn.execute(orders.update().where(orders.c.id == 1).values(status="paid"))
```

Because `engine.begin()` is a single transaction, setting the GUCs
once at the start of the block with `is_local=false` (session scope)
carries them through every subsequent statement in the block. The
connection is returned to the pool after `commit()`, and the next use
of that connection either resets the GUCs or sets its own.

### Pattern 2 — hand-rolled execute wrapper for FastAPI / Flask

If you're running SQLAlchemy inside a web framework, mirror what the
Django integration does: a middleware that opens a context once per
request and installs a `before_cursor_execute` event listener that
prefixes `set_config` to every SQL statement.

```python
import json, uuid
from contextvars import ContextVar
from sqlalchemy import event

_request_ctx: ContextVar[dict | None] = ContextVar("audit_ctx", default=None)


def install_audit_context_hook(engine):
    @event.listens_for(engine, "before_cursor_execute")
    def _inject(conn, cursor, statement, parameters, context, executemany):
        ctx = _request_ctx.get()
        if ctx is None:
            return
        cursor.execute(
            "SELECT set_config('auditrum.context_id', %s, true), "
            "set_config('auditrum.context_metadata', %s, true)",
            (ctx["id"], ctx["metadata_json"]),
        )
```

Then in your per-request middleware:

```python
async def audit_middleware(request, call_next):
    ctx = {
        "id": str(uuid.uuid4()),
        "metadata_json": json.dumps({
            "user_id": getattr(request.state, "user_id", None),
            "url": request.url.path,
            "source": "http",
        }),
    }
    token = _request_ctx.set(ctx)
    try:
        return await call_next(request)
    finally:
        _request_ctx.reset(token)
```

Downside vs the Django approach: `is_local=true` in a
`before_cursor_execute` hook works **only** inside an active
transaction. If your SA session is in autocommit mode, the set_config
from one cursor.execute is gone by the next. Use `is_local=false` and
reset on session checkin for autocommit pools.

## Alembic integration

You have two options depending on how strictly you want audit triggers
to be version-controlled.

### Option A — runtime sync (simple)

Call `sync(engine)` at app startup. Triggers live outside Alembic's
model graph; they reconcile on boot. Zero Alembic changes needed.
Best for rapid iteration.

### Option B — Alembic migration operations (strict)

Invoke `sync` from your Alembic `env.py`'s `run_migrations_online` so
trigger reconciliation happens as part of `alembic upgrade head`:

```python
# alembic/env.py
from alembic import context
from sqlalchemy import engine_from_config, pool

def run_migrations_online():
    connectable = engine_from_config(
        context.config.get_section(context.config.config_ini_section),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata)
        with context.begin_transaction():
            context.run_migrations()

        # After your normal migrations run, reconcile audit triggers
        from auditrum.integrations.sqlalchemy import bootstrap_schema, sync
        bootstrap_schema(connectable)
        sync(connectable)
```

Each `alembic upgrade` now picks up any changes to `track_table(...)`
calls and installs/updates triggers accordingly, using the same drift
detection as the Django path.

## Async (SQLAlchemy 2.0 asyncio)

`TriggerManager` and friends are synchronous because they issue DDL
that rarely benefits from async. Call them from sync code at startup,
or bridge through `asyncio.to_thread`:

```python
import asyncio
from auditrum.integrations.sqlalchemy import sync

async def startup():
    await asyncio.to_thread(sync, engine.sync_engine)
```

Query-time context propagation via `before_cursor_execute` works
identically in async sessions — SA fires the event for both sync and
async cursors.

## Examples

### End-to-end with FastAPI

```python
from contextvars import ContextVar
import json, uuid
from fastapi import FastAPI, Request
from sqlalchemy import Column, Integer, MetaData, String, Table, create_engine, event, text

from auditrum.integrations.sqlalchemy import (
    bootstrap_schema, sync, track_table,
)

# Schema
meta = MetaData()
orders = Table(
    "orders", meta,
    Column("id", Integer, primary_key=True),
    Column("status", String),
)

track_table(orders, fields=["status"])

engine = create_engine("postgresql+psycopg://u:p@localhost/app")
meta.create_all(engine)
bootstrap_schema(engine)
sync(engine)

# Per-request context propagation
_request_ctx: ContextVar[dict | None] = ContextVar("audit", default=None)

@event.listens_for(engine, "before_cursor_execute")
def _inject(conn, cursor, sql, params, ctx, executemany):
    active = _request_ctx.get()
    if active is None:
        return
    cursor.execute(
        "SELECT set_config('auditrum.context_id', %s, false), "
        "set_config('auditrum.context_metadata', %s, false)",
        (active["id"], active["metadata"]),
    )

app = FastAPI()

@app.middleware("http")
async def audit_scope(request: Request, call_next):
    token = _request_ctx.set({
        "id": str(uuid.uuid4()),
        "metadata": json.dumps({
            "url": request.url.path,
            "method": request.method,
            "user_id": getattr(request.state, "user_id", None),
            "source": "http",
        }),
    })
    try:
        return await call_next(request)
    finally:
        _request_ctx.reset(token)

@app.post("/orders/{id}/pay")
def pay(id: int):
    with engine.begin() as conn:
        conn.execute(orders.update().where(orders.c.id == id).values(status="paid"))
    return {"status": "ok"}
```

Every `POST /orders/.../pay` now produces one row in `auditlog` with
the diff `{"status": {"old": "pending", "new": "paid"}}` and one row
in `audit_context` with the URL, user id, and the auto-generated
request UUID.

## What's next

- [Time travel](time-travel.md) — `reconstruct_row`, `reconstruct_table`
- [Hardening](hardening.md) — hash chain, retention, append-only roles
- [Observability](observability.md) — wire OTel trace ids through to audit events
