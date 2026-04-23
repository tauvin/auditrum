# Performance tuning

Playbook for when auditrum's write-path cost shows up on a flame
graph. Each section is a lever you can pull, roughly ordered from
highest-impact-per-effort at the top.

The numbers cited are **qualitative** — actual magnitudes depend on
your schema, PG version, and concurrency. See
[Performance](performance.md) for the methodology to measure your
own numbers.

## 1. Narrow the field filter

``@track(fields=[...])`` produces a trigger that only diffs the
listed columns. ``@track(exclude=[...])`` is the mirror. Either
shrinks ``old_filtered`` / ``new_filtered``, which shrinks
``jsonb_diff`` cost, which shrinks INSERT time into ``auditlog``.

```python
# Before — diffs every column, including large/volatile ones
@track()
class Order(models.Model): ...

# After — explicit whitelist
@track(fields=["status", "total", "shipped_at"])
class Order(models.Model): ...
```

Most impactful for tables with wide rows (many columns) or
frequently-changing timestamp-like fields (``updated_at``) that
don't carry semantic meaning.

## 2. Use ``log_condition`` to skip no-op rows

If there's a clear gate for "we don't care about this class of
change", push it into PL/pgSQL. The trigger fires, sees the
condition is false, and returns immediately — never touches
``auditlog``.

```python
@track(
    fields=["status", "total"],
    log_condition="NEW.status <> 'draft'",
)
class Order(models.Model): ...
```

Common patterns:

| Use case                              | ``log_condition``                                |
|---------------------------------------|--------------------------------------------------|
| Skip draft rows                       | ``NEW.status <> 'draft'``                        |
| Skip rows owned by a test tenant      | ``NEW.tenant_id <> 0``                           |
| Skip internal-system-user changes     | ``current_setting('auditrum.context_metadata', true)::jsonb->>'source' <> 'system'`` |

`log_condition` is **trusted input** — never interpolate user data.
See [concepts.md](concepts.md#tracking-primitives).

## 3. Use ``FieldFilter.only`` instead of many ``extra_meta``

``extra_meta=['a', 'b', 'c']`` stamps per-row static columns into
``auditlog.meta`` for every event. That's useful for sharded /
multi-tenant queries. But if you're using it to capture data that
could go into ``diff`` instead, the duplication doubles storage.

Prefer ``fields=[...]`` (shows up once in ``diff``) over
``extra_meta=[...]`` (shows up once in ``meta`` and again in
``new_data``).

## 4. Prune ``auditlog`` partitions proactively

Audit data is append-only. Once it ages past your retention window,
keeping it on the hot partition hurts every write (index pages
stay warm, GIN on ``diff`` grows) and every point query.

Two patterns:

* **Drop whole partitions** — fastest, but loses the data:
  ```python
  from auditrum.retention import drop_old_partitions
  drop_old_partitions(conn, table="auditlog", older_than="2 years")
  ```
* **Row-level purge** — slower, but selective (e.g. for per-tenant
  retention): ``auditrum.retention.generate_purge_sql``.

Run either on cron. ``auditrum_add_partitions`` extends the
forward window so a missed run doesn't break writes.

## 5. Decide on hash chain scope

The BEFORE INSERT chain trigger takes a per-``audit_table``
advisory lock. That serialises chain writes — a real ceiling on
insert throughput under heavy concurrency.

Trade-offs:

| Setup                           | Concurrency ceiling                          | Tamper evidence |
|---------------------------------|----------------------------------------------|-----------------|
| No chain                        | unlimited (just PG insert throughput)        | none            |
| Chain on default ``auditlog``   | capped by advisory lock                      | full-log chain   |
| Chain on a secondary audit table | unlimited on primary, capped on secondary  | targeted        |

If you only need tamper evidence for a subset of events (PII writes,
high-value transactions), route them to a dedicated audit table via
``audit_table=`` in ``@track(...)`` and enable the chain only on
that table.

## 6. Batch writes behind the trigger

If your workload does ``QuerySet.update()`` on millions of rows,
each row fires the trigger and produces an audit event. Two ways
to handle:

* **Apply ``@track(log_condition=...)`` to short-circuit** if the
  bulk write falls into a known "we don't care" bucket.
* **Use ``auditrum_context`` wrapping** to mark the bulk operation
  as such, then query by ``context_id`` later to coalesce:
  ```python
  with auditrum_context(source="bulk_migration_42"):
      Order.objects.filter(status="pending").update(status="closed")
  # Later:
  # AuditLog.objects.for_context(ctx_id).count() == rows_updated
  ```

The write cost is still N rows of audit events, but the `meta` of
each event carries the same context for easy filtering.

## 7. Check ``connection.execute_wrapper`` overhead

The Django middleware prefixes every query with a ``set_config``
call. On a read-heavy page (100+ queries), that's 100+ extra
round-trips of a short ``SELECT``. Negligible in most cases;
measurable in the absolute worst case.

Tuning options:

* **``PGAUDIT_MIDDLEWARE_METHODS``** — limit the wrapper to
  non-idempotent HTTP methods:
  ```python
  PGAUDIT_MIDDLEWARE_METHODS = ("POST", "PUT", "PATCH", "DELETE")
  ```
  GET / HEAD requests skip the wrapper entirely.
* **Read-only Django views with ``@transaction.atomic(using='ro')``**
  routed to a replica — the wrapper is installed on the default
  connection, so a replica alias is untouched.

## 8. GIN index on ``diff`` — keep or drop

Default: ``auditlog_diff_gin_idx ON auditlog USING GIN (diff)``.

* **Keep** if you run ``diff @> '{"status": …}'`` style queries —
  the GIN makes them near-instant regardless of log size.
* **Drop** if your read pattern is always
  ``for_object(obj)`` / ``for_user(user)`` / ``for_context(ctx)`` —
  the composite index on ``(table_name, object_id, changed_at DESC)``
  plus btrees on ``user_id`` / ``context_id`` already cover those.
  GIN maintenance cost is real on write-heavy audit tables.

Measure on your data before dropping. See ``EXPLAIN ANALYZE``
against a representative query.

## 9. ``reconstruct_row`` / ``reconstruct_table`` at scale

For time-travel queries against deep histories (1000+ events on
one row):

* **``reconstruct_row``** — latency scales in history depth.
  Composite index keeps it linear, not quadratic. If it's slow
  on your data, check that the index is actually being used
  (``EXPLAIN`` plan should show ``Index Scan using
  auditlog_target_idx``); if not, PG's planner is confused by
  statistics drift — run ``ANALYZE auditlog``.
* **``reconstruct_table(stream=True)``** — required for whole-
  table as-of queries on anything bigger than a few thousand rows.
  The default (``stream=False``) does ``fetchall()``; the streaming
  path uses a server-side named cursor and is memory-bounded.

## 10. Connection pooling

* **pgbouncer transaction mode** — supported since 0.3.1.
  ``is_local=true`` GUC propagation ships in the same SQL
  submission as the user query, so it can't leak across
  transactions.
* **Django ``CONN_MAX_AGE > 0``** — same story. The middleware
  tested against both patterns; the 0.3.1 tests in
  ``tests/integration/test_sync_concurrency_pg.py`` confirm it.

If you see GUC-leak-style bugs ("this event is attributed to the
wrong user"), the cause is almost always a code path that sets
the context manually with ``is_local=false`` — audit the call
sites of ``set_config(..., false)`` and switch to the middleware
or ``auditrum_context(...)`` block.

## When the levers aren't enough

If after running through this list you're still too slow:

1. Open an issue with ``EXPLAIN ANALYZE`` output and a
   ``benchmarks/`` delta.
2. Consider whether you're trying to do something the trigger model
   is genuinely bad at (millisecond-sensitive hot-path writes on a
   single high-contention row, for example). A row-level audit log
   may be the wrong tool; append-only event logs in an external
   queue can be a better fit for that shape.
