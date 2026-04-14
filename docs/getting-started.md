# Getting started

Five minutes from `pip install` to your first audit event. Pick the
option that matches your stack.

## Requirements

- Python 3.11 or newer
- PostgreSQL 13 or newer (partitioning and `jsonb_diff` need the modern
  syntax; `pgcrypto` is required only if you enable the optional hash
  chain)
- Write access to a database where you're allowed to create tables,
  functions, and triggers

## Install

Pick the extra that matches your framework. Extras are **independent**
— installing `auditrum[django]` does not drag in SQLAlchemy, and vice
versa.

```bash
# Core only — raw psycopg or bring-your-own framework
pip install auditrum

# Django integration
pip install "auditrum[django]"

# SQLAlchemy integration
pip install "auditrum[sqlalchemy]"

# Observability (OpenTelemetry, Prometheus, Sentry)
pip install "auditrum[observability]"

# Combine as needed
pip install "auditrum[django,observability]"
```

---

## Option 1: Django

### 1. Add to `INSTALLED_APPS` and `MIDDLEWARE`

```python
# settings.py
INSTALLED_APPS = [
    # ...
    "django.contrib.contenttypes",
    "auditrum.integrations.django",
    # ...
]

MIDDLEWARE = [
    # ...
    "auditrum.integrations.django.middleware.AuditrumMiddleware",
    # ...
]
```

The middleware installs a database connection wrapper that propagates
per-request context (user id, IP, URL, request id, optional OTel trace
id) into every query as a session GUC, so triggers can pick it up
without any app-level plumbing.

### 2. Mark a model for auditing

```python
# myapp/models.py
from django.db import models
from auditrum.integrations.django import track, AuditedModelMixin


@track(fields=["status", "total"], extra_meta=["tenant_id"])
class Order(AuditedModelMixin, models.Model):
    status = models.CharField(max_length=32)
    total = models.DecimalField(max_digits=10, decimal_places=2)
    tenant_id = models.IntegerField()
```

`@track(...)` stores a declarative `TrackSpec` for the model.
`AuditedModelMixin` adds read-only helpers (`order.audit_events`,
`order.audit_at(ts)`, `Order.audit_state_as_of(ts)`) without touching
the model's schema.

### 3. Generate and apply migrations

```bash
# First time: install the audit log, context table, partitions, and helpers
python manage.py migrate auditrum_django

# Then generate a migration for every @track'd model
python manage.py auditrum_makemigrations

# Apply it
python manage.py migrate
```

`auditrum_makemigrations` walks the `@track` registry, groups specs by
the app label of their tracked model, and writes one migration file per
app under `<app>/migrations/`. The generated file contains
`InstallTrigger` operations that pgtrigger-style integrate with Django's
normal migration flow.

### 4. Watch an event land

```python
from myapp.models import Order

o = Order.objects.create(status="new", total=99.00, tenant_id=1)
o.status = "paid"
o.save()

for event in o.audit_events.order_by("changed_at"):
    print(event.operation, event.diff, event.context.metadata if event.context else None)
# INSERT None None
# UPDATE {'status': 'paid'} {'url': '...', 'user_id': 42, 'source': 'http', ...}
```

See the [Django guide](django.md) for details on the ORM helpers,
middleware configuration, and admin integration.

---

## Option 2: SQLAlchemy

### 1. Describe tracked tables

```python
# schema.py
from sqlalchemy import Column, Integer, MetaData, String, Table
from auditrum.integrations.sqlalchemy import track_table

meta = MetaData()

users = Table(
    "users", meta,
    Column("id", Integer, primary_key=True),
    Column("email", String),
    Column("name", String),
)

track_table(users, fields=["email"])
```

### 2. Bootstrap schema and sync triggers on startup

```python
# app.py
from sqlalchemy import create_engine
from auditrum.integrations.sqlalchemy import bootstrap_schema, sync
from schema import meta

engine = create_engine("postgresql+psycopg://user:pw@host/db")
meta.create_all(engine)          # your tables
bootstrap_schema(engine)          # auditlog + audit_context + helper functions
sync(engine)                      # install triggers for every track_table() call
```

`bootstrap_schema` is idempotent — call it on every app boot. `sync`
detects drift against an `auditrum_applied_triggers` table and
re-installs only what's changed. Safe to run repeatedly.

### 3. See events flow

```python
with engine.begin() as conn:
    conn.execute(users.insert().values(id=1, email="a@x.com", name="alice"))
    conn.execute(users.update().where(users.c.id == 1).values(email="a2@x.com"))

    result = conn.execute(text(
        "SELECT operation, diff FROM auditlog WHERE table_name = 'users' ORDER BY id"
    ))
    for row in result:
        print(row)
# ('INSERT', None)
# ('UPDATE', {'email': 'a2@x.com'})
```

See the [SQLAlchemy guide](sqlalchemy.md) for context propagation,
Alembic integration, and asyncio considerations.

---

## Option 3: Raw psycopg / any other framework

Nothing framework-specific — just `TriggerManager` and a psycopg
connection.

```python
import psycopg
from auditrum.executor import PsycopgExecutor
from auditrum.tracking import FieldFilter, TrackSpec, TriggerManager
from auditrum.schema import (
    generate_audit_context_table_sql,
    generate_auditlog_table_sql,
    generate_jsonb_diff_function_sql,
    generate_audit_attach_context_sql,
    generate_audit_current_user_id_sql,
    generate_audit_reconstruct_sql,
    generate_auditlog_partitions_sql,
)

with psycopg.connect("postgresql://user:pw@host/db") as conn:
    # One-time bootstrap: tables and helper functions
    with conn.cursor() as cur:
        cur.execute(generate_audit_context_table_sql())
        cur.execute(generate_auditlog_table_sql())
        cur.execute(generate_jsonb_diff_function_sql())
        cur.execute(generate_audit_attach_context_sql())
        cur.execute(generate_audit_current_user_id_sql())
        cur.execute(generate_audit_reconstruct_sql())
        cur.execute(generate_auditlog_partitions_sql(months_ahead=3))

    # Install a trigger on the `orders` table
    mgr = TriggerManager(PsycopgExecutor(conn))
    mgr.bootstrap()   # creates auditrum_applied_triggers tracking table

    spec = TrackSpec(
        table="orders",
        fields=FieldFilter.only("status", "total"),
    )
    mgr.sync([spec])
    conn.commit()

    # Any UPDATE on `orders` now writes into `auditlog`.
```

For context propagation in this setup, call
`SELECT set_config('auditrum.context_id', '<uuid>', false)` and
`set_config('auditrum.context_metadata', '<json>', false)` yourself
before the statement, or wire up whatever cursor-wrapping hook your
framework provides. See [Core concepts](concepts.md#context-propagation)
for the gritty details.

---

## What to read next

- [Core concepts](concepts.md) to understand the moving parts
- [Time travel](time-travel.md) to query past state
- [Hardening](hardening.md) to make the audit log tamper-evident
- [CLI reference](cli.md) for one-off operations
