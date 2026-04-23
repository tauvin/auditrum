# Migration cookbook

How to move to auditrum from the four starting points we've seen:

1. **No audit at all** — greenfield install.
2. **``django-pghistory``** — same trigger-based model, different API.
3. **``django-simple-history``** — Python-side save-override history
   tables.
4. **Hand-rolled Postgres triggers** — custom audit table, no library.

Each recipe below is a working sequence, not a theoretical overview.
The pattern is the same across all four: install auditrum's
schema → mark models → generate migrations → decide what to do with
existing history data.

## Recipe 1 — No prior audit

The easiest path. See [Getting started → Option 1: Django](getting-started.md#option-1-django).

```bash
pip install "auditrum[django]"
```

```python
# settings.py
INSTALLED_APPS = [
    # ...
    "django.contrib.contenttypes",
    "auditrum.integrations.django",
]

MIDDLEWARE = [
    # ...
    "auditrum.integrations.django.middleware.AuditrumMiddleware",
]
```

```python
# myapp/models.py
from auditrum.integrations.django import track, AuditedModelMixin

@track(fields=["status", "total"])
class Order(AuditedModelMixin, models.Model):
    status = models.CharField(max_length=32)
    total = models.DecimalField(max_digits=10, decimal_places=2)
```

```bash
python manage.py migrate auditrum_django
python manage.py auditrum_makemigrations
python manage.py migrate
```

No history migration needed — the first audit rows land on the next
row-level mutation.

## Recipe 2 — From ``django-pghistory``

Closest architectural cousin. Same PG-trigger-based approach, same
"captures bulk_update / raw SQL / migrations" guarantees. The
practical differences:

| Concern                 | ``django-pghistory``              | auditrum                                    |
|-------------------------|-----------------------------------|---------------------------------------------|
| History table layout    | One per tracked model             | Single shared ``auditlog`` with ``table_name``/``object_id`` |
| Context propagation     | ``Context`` model + middleware    | Session GUC + ``_audit_attach_context()`` lazy upsert         |
| Declarative API         | ``@pghistory.track(...)``          | ``@track(fields=[...], extra_meta=[...])``                    |
| Hash chain              | Not built in                       | ``auditrum.hash_chain`` + ``verify_chain``                     |
| Retention / partitions  | Manual                             | ``auditrum.retention`` + monthly partitions by default         |

### Step 1 — Install auditrum alongside pghistory

```bash
pip install "auditrum[django]"
```

Leave pghistory installed. The two can coexist; auditrum's triggers
are named ``audit_<table>_trigger``, which doesn't collide with
pghistory's ``_pgh_*`` naming.

### Step 2 — Translate `@pghistory.track` decorators

```python
# Before
from pghistory import track
from pghistory.models import Context

@track()
class Order(models.Model):
    ...

# After
from auditrum.integrations.django import track, AuditedModelMixin

@track()  # default = FieldFilter.all()
class Order(AuditedModelMixin, models.Model):
    ...
```

``fields=[...]`` → ``@track(fields=[...])`` maps directly.
``exclude=[...]`` → ``@track(exclude=[...])``.
``related_name="events"`` etc. don't have an equivalent — audit rows
live in the single ``auditlog`` table, not a per-model relation.
Access events via ``order.audit_events`` (added by
``AuditedModelMixin``) instead of ``order.event_set``.

### Step 3 — Install auditrum's schema

```bash
python manage.py migrate auditrum_django
python manage.py auditrum_makemigrations
python manage.py migrate
```

Both systems now write to their respective history tables in
parallel. That's fine for a migration window.

### Step 4 — (optional) Back-fill historic pghistory data

pghistory's event tables have columns like ``pgh_created_at``,
``pgh_label``, ``pgh_context_id``, plus a snapshot of every column.
To preserve that history in auditrum's format:

```sql
-- One-off per tracked model. Example for ``myapp_order``:
INSERT INTO auditlog (
    operation, changed_at, object_id, table_name, user_id,
    old_data, new_data, diff, context_id, meta
)
SELECT
    CASE
        WHEN pgh_label LIKE 'insert%' THEN 'INSERT'
        WHEN pgh_label LIKE 'update%' THEN 'UPDATE'
        WHEN pgh_label LIKE 'delete%' THEN 'DELETE'
    END,
    pgh_created_at,
    pgh_obj_id::text,
    'myapp_order',
    NULL,  -- pghistory carries user via context FK; reconstruct separately if needed
    NULL,  -- pghistory event rows are snapshots, not diff pairs
    to_jsonb(row.*) - ARRAY['pgh_created_at','pgh_label','pgh_context_id','pgh_obj_id','pgh_id'],
    NULL,  -- diff can be recomputed from consecutive snapshots; usually not worth it
    NULL,  -- rebuild context_id separately if pghistory Context rows are worth preserving
    jsonb_build_object('migrated_from', 'pghistory', 'source_label', pgh_label)
FROM myapp_order_event row;
```

The back-fill is optional — most users skip it, treat the pghistory
tables as read-only archive, and start the auditrum chain fresh.
Dropping pghistory later is then a schema migration step, not a
data migration.

### Step 5 — Remove pghistory

Once your observability dashboards read auditrum's tables and you're
confident the migration window is over:

```bash
pip uninstall django-pghistory
# Remove @pghistory.track decorators and the pghistory INSTALLED_APPS entry
# python manage.py makemigrations + migrate to drop the event tables
```

## Recipe 3 — From ``django-simple-history``

Different beast. simple-history uses **application-level**
``save()`` overrides plus per-model ``HistoricalFoo`` tables. That
means ``QuerySet.update()``, ``bulk_create``, raw SQL, and psql
edits **are not captured** — auditrum's whole pitch is to fix that
class of gap.

### Step 1 — Confirm you actually want to switch

If you rely on ``instance.history`` being a Django ORM relation,
auditrum's single-table approach is a different shape. Decide
between:

* **Keep simple-history for the Django-level "author" trail and add
  auditrum for bulk-operation coverage.** Both run, no conflict.
  Most of the cost is double-writes on regular ``save()``.
* **Replace simple-history entirely.** You lose
  ``instance.history`` and ``historical.instance_at(datetime)``; you
  gain ``instance.audit_events``, ``instance.audit_at(datetime)``,
  and trigger-level coverage.

### Step 2 — Install

```bash
pip install "auditrum[django]"
# Follow Recipe 1 steps 2-3
```

### Step 3 — Translate ``HistoricalRecords()`` → ``@track``

```python
# Before
from simple_history.models import HistoricalRecords

class Order(models.Model):
    status = models.CharField(max_length=32)
    history = HistoricalRecords(excluded_fields=["updated_at"])

# After
from auditrum.integrations.django import track, AuditedModelMixin

@track(exclude=["updated_at"])
class Order(AuditedModelMixin, models.Model):
    status = models.CharField(max_length=32)
```

### Step 4 — History preservation

simple-history stores a full snapshot per save in a sibling table
(``myapp_historicalorder``). Back-filling is straightforward
because the snapshot layout matches auditrum's ``new_data`` column:

```sql
INSERT INTO auditlog (
    operation, changed_at, object_id, table_name,
    user_id, old_data, new_data, diff, context_id, meta
)
SELECT
    CASE history_type
        WHEN '+' THEN 'INSERT'
        WHEN '~' THEN 'UPDATE'
        WHEN '-' THEN 'DELETE'
    END,
    history_date,
    id::text,
    'myapp_order',
    history_user_id,
    NULL,
    to_jsonb(row.*) - ARRAY['history_id','history_date','history_user_id','history_type','history_change_reason'],
    NULL,  -- diff: recompute from consecutive rows if needed
    NULL,
    jsonb_build_object(
        'migrated_from', 'simple-history',
        'change_reason', history_change_reason
    )
FROM myapp_historicalorder row;
```

### Step 5 — Decommission simple-history

Remove ``HistoricalRecords()`` from models, run makemigrations +
migrate to drop the ``HistoricalFoo`` tables. The archived data
from step 4 is now inside auditrum.

## Recipe 4 — From hand-rolled triggers

You have a trigger on some tables that writes into a custom audit
table (maybe ``audit.changes`` or similar). auditrum does the same
job with more features — hash chain, context propagation, retention,
time travel.

### Step 1 — Decide: replace or coexist

If your custom table has production queries hitting it, don't
rip it out. Install auditrum alongside and double-write during the
cutover. If you control all the readers, full replacement is
cleaner.

### Step 2 — Install

Same as Recipe 1 steps 1-3. Use a non-conflicting
``PGAUDIT_TABLE_NAME`` if your custom table is called ``auditlog``:

```python
# settings.py
PGAUDIT_TABLE_NAME = "auditrum_auditlog"  # or any non-conflicting name
```

### Step 3 — Drop custom triggers on migrated tables

Once an ``@track`` decorator is in place for a model and
``auditrum_makemigrations`` has produced a migration, the custom
trigger on that table is redundant. Drop it in a migration:

```python
# myapp/migrations/0042_drop_legacy_audit_trigger.py
from django.db import migrations

class Migration(migrations.Migration):
    dependencies = [("myapp", "0041_auditrum_install")]
    operations = [
        migrations.RunSQL(
            sql="DROP TRIGGER IF EXISTS legacy_audit_trg ON myapp_order;",
            reverse_sql="-- intentionally empty; the legacy trigger is gone",
        ),
    ]
```

### Step 4 — Back-fill

Depends entirely on the shape of your custom table — no generic
recipe. Key targets in the auditrum schema:

* ``auditlog.table_name`` — the ``_meta.db_table`` of the tracked model.
* ``auditlog.object_id`` — ``pk::text``.
* ``auditlog.operation`` — one of ``'INSERT'``, ``'UPDATE'``, ``'DELETE'``.
* ``auditlog.changed_at`` — event timestamp (partition key).
* ``auditlog.old_data`` / ``new_data`` / ``diff`` — JSONB. Diff is
  paired ``{field: {old, new}}`` since 0.4.
* ``auditlog.meta`` — anything per-row you want to keep from the
  legacy system.
* ``audit_context.metadata`` — per-request/job blob (``source``,
  ``user_id``, ``request_id``, …).

## Things to check after any migration

1. **Trigger presence.** ``auditrum status`` lists all installed
   triggers. Anything ``@track``-decorated should show up.
2. **Row flow.** Mutate a row, check ``AuditLog.objects.for_object(obj)``
   has a new entry.
3. **Context.** If you installed the middleware,
   ``AuditLog.objects.for_user(request.user)`` should return events
   from the current request.
4. **Retention / partitions.** ``auditrum_add_partitions`` management
   command extends partitions forward; make sure it's on cron.
5. **Hardening** (if the audit log matters for compliance): run
   ``auditrum harden`` to REVOKE ``INSERT`` / ``UPDATE`` / ``DELETE``
   on ``auditlog`` from ``PUBLIC``. See ``docs/hardening.md``.
