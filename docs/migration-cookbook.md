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

## Recipe 5 — Removing a legacy in-house audit trigger

A variant of Recipe 4, but the failure mode is subtler: you adopted
auditrum, marked your models with ``@track``, and the new ``auditlog``
is filling up correctly — but an **older home-grown audit trigger,
predating auditrum**, is still firing on the same tables. It writes to
a *separate* legacy table (for example ``legacy_audit_log``), so
every audited write now pays for **two** triggers and lands two rows.
Nothing is broken, so it's easy to miss; you just silently doubled the
write overhead.

### Step 1 — Detect the double-fire

The cheap signal is in the query plan. Run an ``EXPLAIN (ANALYZE)`` of
a representative mutation inside a transaction you roll back:

```sql
BEGIN;
EXPLAIN (ANALYZE) UPDATE myapp_order SET status = status WHERE id = 1;
ROLLBACK;
```

A single auditrum trigger shows one ``Trigger ...`` line. If you see
**two** ``Trigger`` lines on a tracked table, something else is firing
alongside auditrum:

```text
Trigger audit_myapp_order_trigger: time=1.41 calls=1
Trigger myapp_order_audit_trigger: time=1.83 calls=1   <- the legacy one
```

To enumerate every audit-ish trigger across the schema:

```sql
SELECT c.relname, t.tgname
FROM pg_trigger t
JOIN pg_class c ON c.oid = t.tgrelid
WHERE NOT t.tgisinternal AND t.tgname LIKE '%audit%'
ORDER BY 1, 2;
```

auditrum's triggers are uniformly named ``audit_<table>_trigger``;
anything that doesn't match that shape is a candidate. Inspect where a
suspect trigger writes by dumping its function body:

```sql
SELECT t.tgname, pg_get_functiondef(t.tgfoid)
FROM pg_trigger t
WHERE t.tgname = 'myapp_order_audit_trigger';
```

The ``INSERT INTO legacy_audit_log ...`` (or whatever target) inside
the function body confirms it's the legacy path.

### Step 2 — Decide what to keep

Drop the legacy **triggers and their functions** — those are the
write-path tax. The legacy **table** is a separate decision. If it
holds history that predates your auditrum install, keep it as a frozen
archive rather than dropping it. Check the coverage overlap:

```sql
SELECT min(changed_at) FROM auditlog;          -- auditrum's earliest row
SELECT min(created_at)  FROM legacy_audit_log;   -- legacy's earliest row
```

If the legacy ``min()`` is older than auditrum's, the legacy table is
your only record of that earlier window — freeze it, don't drop it.
Stopping the triggers stops *new* writes; the existing rows stay
queryable.

### Step 3 — Grep the app before you touch anything

Reads of the legacy table will keep working after you drop its
triggers — the table itself stays. But confirm nothing depends on it
*continuing to receive new rows* (a report that joins recent
``legacy_audit_log`` activity, a reconciliation job, a dashboard):

```bash
rg -n "legacy_audit_log" --type py --type sql
```

If a live reader needs fresh data, migrate it to auditrum's
``auditlog`` first. Only once new writes to the legacy table are
genuinely unread should you proceed.

### Step 4 — The migration to drop them safely

Drop the legacy triggers and functions in one idempotent migration.
The key is to scope the drop by the legacy naming pattern **without**
matching auditrum's own triggers. The two patterns are distinct —
auditrum uses ``audit_<table>_trigger`` (prefix), the legacy setup here
used ``<table>_audit_trigger`` (suffix) — but verify the match list on
staging before you run anything destructive:

```sql
-- Dry run: list exactly what the migration will drop.
-- Must NOT contain any 'audit_<table>_trigger' (auditrum) rows —
-- those don't end in '_audit_trigger', so the LIKE skips them.
SELECT c.relname, t.tgname
FROM pg_trigger t
JOIN pg_class c ON c.oid = t.tgrelid
WHERE NOT t.tgisinternal AND t.tgname LIKE '%\_audit\_trigger'
ORDER BY 1, 2;
```

Eyeball that list. Once it contains only legacy triggers, the
migration is a ``DO`` block that drops each trigger and then its
function, ``IF EXISTS`` throughout so it's re-runnable:

```python
# myapp/migrations/0050_drop_legacy_audit_triggers.py
from django.db import migrations

DROP_LEGACY = r"""
DO $$
DECLARE
    r record;
BEGIN
    FOR r IN
        SELECT c.relname AS tbl, t.tgname AS trg, t.tgfoid::regprocedure AS fn
        FROM pg_trigger t
        JOIN pg_class c ON c.oid = t.tgrelid
        WHERE NOT t.tgisinternal
          AND t.tgname LIKE '%\_audit\_trigger'   -- legacy suffix, not auditrum's prefix
    LOOP
        EXECUTE format('DROP TRIGGER IF EXISTS %I ON %I', r.trg, r.tbl);
        EXECUTE format('DROP FUNCTION IF EXISTS %s', r.fn);
    END LOOP;
END $$;
"""


class Migration(migrations.Migration):
    dependencies = [("myapp", "0049_auditrum_install")]
    operations = [
        migrations.RunSQL(
            sql=DROP_LEGACY,
            # The legacy trigger DDL isn't ours to recreate; leave reverse a no-op.
            reverse_sql="-- legacy triggers intentionally not restored",
        ),
    ]
```

Run it on staging first, re-run the Step 1 ``EXPLAIN (ANALYZE)`` there,
and confirm exactly one ``Trigger`` line remains before promoting to
production. Because it's idempotent, a second apply is a no-op.

A note on partitioning: this is unrelated to auditrum's partitioned
``auditlog``. The legacy triggers live on the **tracked tables**
(``myapp_order`` and friends), not on the partitioned audit log, so
there's no parent/partition trigger-propagation subtlety to worry
about here.

## Switching a table to diff storage

To stop writing `old_data` / `new_data` for a table (see
[Storage mode](concepts.md#storage-mode)), set `storage="diff"` on its
spec and apply the change like any other trigger change:

```python
@track(storage="diff")
class Order(models.Model): ...
```

```bash
python manage.py auditrum_makemigrations
python manage.py migrate
```

Rows already in `auditlog` keep their snapshots; to clear them see
[Clearing old snapshots](#clearing-old-snapshots). Reconstruction decides
per row, so a history that starts as `full` and continues as `diff`
reads correctly; see [Time travel](time-travel.md#how-it-works). The
switch is reversible the same way. It cannot be combined with
`log_condition`.

## Clearing old snapshots

Switching modes does not rewrite existing rows, and the snapshots of the
old rows are most of the audit log's size. `compact-snapshots` clears
them in place, while the application keeps running. Look first, then run:

```bash
auditrum compact-snapshots --table orders --dry-run
auditrum compact-snapshots --table orders --table invoices --older-than '6 months'
```

```sql
VACUUM (ANALYZE) auditlog;
```

For the `orders` table the dry run prints
`orders: would clear snapshots of N row(s)`; the real run prints
`cleared`. Django: `python manage.py auditrum_compact_snapshots --table orders`.
Options are in the [CLI reference](cli.md#compact-snapshots).

Reconstruction reads a cleared row like a diff-only one, so past states
stay the same. The table is processed `--batch-size` objects at a time
(default 1000). Each batch is first checked with the
[diff chain](diff-chain.md), with snapshot values included, so columns
that no diff ever touched are compared too. The command leaves a row or
object alone when clearing it would lose information:

- Objects with a diff chain finding are kept whole and reported (up to
  10 of them). While snapshots exist, reconstruction is right even across
  a trigger bypass; without them only the diff chain is left.
- Objects with a snapshot that records a change of a column the diff does
  not are kept and reported the same way. Snapshots written before 0.6.2
  under a narrower field filter can do that; reconstruction from the diff
  alone would lose the change.
- Rows covered by the hash chain are kept and counted: the hash was
  computed over the snapshots.
- Rows without a fully paired `{old, new}` diff (pre-0.4 rows, rows with
  no diff) keep their snapshots.

Only `storage="diff"` tables are accepted. After its check each batch is
cleared with one `UPDATE`. The CLI and a Django command outside
`atomic()` commit every batch, so a long run does not hold one big
transaction or lock; inside a transaction the whole run is one. A second
run finds nothing to do.

The caller needs `UPDATE` on the audit table (under
[hardening](hardening.md) that is the admin role, not the app role) and
the privileges `verify-diff-chain` needs.

Limits to know before running it:

- Past states now come from diffs, so the diff-mode limits apply to the
  old history too. If the field filter changed over time, reconstructed
  past states use the current filter. A column added after the snapshots
  were written appears in past states with its current value; a dropped
  column disappears from them.
- Snapshots written before 0.6.2 contain excluded columns. The check
  reads snapshots through the current field filter, and after
  compaction those columns no longer appear in reconstructed past
  states.
- `tstzrange` and `timestamptz[]` values are compared as text, which
  depends on the session `TimeZone` of the write. Objects whose history
  was written under different time zones can be kept although nothing is
  wrong with them (plain `timestamptz` columns are compared by value).
- Each batch reads the full history of its objects, snapshots included,
  so a run costs about one pass over the table's audit rows plus the
  check. With `jit` on, each batch also pays the JIT compile of the
  check; a larger `--batch-size` spreads it.
- Tables with `log_condition` are refused.
- A bypass between the check and the update is not caught. Run it when
  no bulk load is running.
- Disk files do not shrink. `VACUUM` lets new rows reuse the freed
  space; to return it to the OS see below.

### Returning the space to the OS

`pg_repack` rewrites a table or partition online and gives the space back.
Without it, copy a month partition without `old_data` / `new_data`. The
example assumes a partition `p` that no writer touches any more; do it
inside a maintenance window.

```sql
BEGIN;

CREATE TABLE p_new (LIKE p INCLUDING ALL);

-- Rows of diff tables with a paired diff: drop the snapshots, keep
-- everything else (including id: reconstruction orders by it).
INSERT INTO p_new (id, operation, changed_at, object_id, table_name, user_id,
                   old_data, new_data, diff, context_id, meta)
SELECT id, operation, changed_at, object_id, table_name, user_id,
       NULL, NULL, diff, context_id, meta
FROM p
WHERE table_name IN ('orders', 'invoices')   -- your diff tables
  AND diff IS NOT NULL
  AND NOT EXISTS (
      SELECT 1 FROM jsonb_each(diff) e
      WHERE jsonb_typeof(e.value) <> 'object'
         OR NOT (e.value ? 'old' AND e.value ? 'new')
  );

-- Every other row, untouched: exactly the complement of the predicate above.
INSERT INTO p_new (id, operation, changed_at, object_id, table_name, user_id,
                   old_data, new_data, diff, context_id, meta)
SELECT id, operation, changed_at, object_id, table_name, user_id,
       old_data, new_data, diff, context_id, meta
FROM p
WHERE NOT (
    table_name IN ('orders', 'invoices')
    AND diff IS NOT NULL
    AND NOT EXISTS (
        SELECT 1 FROM jsonb_each(diff) e
        WHERE jsonb_typeof(e.value) <> 'object'
           OR NOT (e.value ? 'old' AND e.value ? 'new')
    )
);

-- Stop here if the counts differ.
SELECT (SELECT count(*) FROM p) AS old_rows,
       (SELECT count(*) FROM p_new) AS new_rows;

ALTER TABLE auditlog DETACH PARTITION p;
ALTER TABLE auditlog ATTACH PARTITION p_new
    FOR VALUES FROM ('2026-01-01') TO ('2026-02-01');
DROP TABLE p;

COMMIT;
```

Run the count check and compare the two numbers before the `DETACH`;
roll back if they differ. `LIKE ... INCLUDING ALL` does not copy foreign
keys or triggers, which is fine for `ATTACH`.

The column list above is the base `auditlog` schema. If you enabled the
hash chain, append `row_hash, prev_hash, chain_seq` to both the target
and the select lists, in the same order. Only rows whose `diff` is in
the paired `{old, new}` shape can lose their snapshots (the guard
above); rows written before 0.4 carry a values-only `diff` and are
copied as they are. With the hash chain enabled, rewriting the
snapshots still breaks it for the rewritten rows: `row_hash` was
computed over the snapshots, and `auditrum verify-chain` will report
them.

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
