# Case study — Catalog

**Status:** draft. Shipping numbers, final schema, and a sign-off
quote land once catalog's team has signed off on publication and
their retention window has accumulated meaningful production data.

<!-- TODO(catalog): confirm wording, numbers, and the commit point. -->

Catalog is a Django 5 / PostgreSQL 16 internal product at the same
company that maintains auditrum. It was the first production
deployment to integrate auditrum end-to-end and has been the
primary real-world feedback loop driving 0.4.

## Why catalog

The roadmap originally named a sister project as the first user.
That shifted: catalog's integration was ready first, and catalog's
workload — bursty write traffic against wide tables with meaningful
business state — is a better stress test for the library than the
earlier target. See ``ROADMAP.md`` for the rationale.

## Workload shape

<!-- TODO(catalog): fill in numbers once published approval is in -->

* **Peak write rate:** _tbd_ tracked rows/second.
* **Tracked models:** _tbd_ tables, _tbd_ total columns under
  ``@track``.
* **Audit log size:** _tbd_ rows across _tbd_ months of history.
* **Hot tables:** _tbd_ — the 3-5 models that produce the majority
  of events.
* **Context propagation path:** HTTP middleware (the standard
  ``AuditrumMiddleware``) for interactive traffic, ``@audit_task``
  (Celery decorator path) for background imports.

## What we measured

Three comparisons matter:

1. **Trigger overhead** on the write path (pre-auditrum vs
   post-auditrum, same workload).
2. **Query-side audit usage** — how often does
   ``AuditLog.objects.for_object(...)`` / ``for_user(...)`` /
   ``for_context(...)`` actually get called in production code?
   (Answers the "are we using it?" question.)
3. **Retention disk footprint** — audit-to-tracked-data ratio
   per month of retention.

<!-- TODO(catalog): drop actual trigger latency, event rate, disk ratio numbers here -->

## What broke

Every bug caught during the catalog integration cycle:

* **``content_type_id`` null-column rendering (0.4, Fixed).** Django
  admin "History" tab showed an empty table for every tracked row.
  Root cause: ``AuditHistoryMixin.object_history_view`` filtered on
  a ``ContentType`` FK the PL/pgSQL trigger never populated. The
  column was removed outright; the view routes through
  ``AuditLogQuerySet.for_object`` now. Regression test:
  ``tests/integration/test_django_history_pg.py``.

* **Admin history template referenced non-existent fields (0.4,
  Fixed).** ``object_history.html`` rendered ``{{ log.source }}``
  and ``{{ log.change_reason }}`` — both live in
  ``log.context.metadata``, not on the ``AuditLog`` model. Same
  class of bug as the ``content_type`` one; the E2E test added
  alongside catches both.

* **``auditlog.diff`` format was sparse (0.4, Breaking).** 0.3
  diff shape was ``{field: new_value}`` — forced every UI consumer
  to cross-reference ``old_data`` and dropped NULL-target updates
  via ``jsonb_strip_nulls``. Paired ``{field: {old, new}}`` shape
  in 0.4; trigger now writes diff for every operation, not just
  UPDATE.

* **Silent drop of ``UPDATE … SET x = NULL`` (0.4, Fixed).** Side
  effect of ``jsonb_strip_nulls``. Null ``new`` values were
  stripped. Removed the wrapper alongside the paired-diff migration.

* **Admin search crashed on context_id (0.4, Fixed).**
  ``AuditLogAdmin.search_fields`` contained the bare ``"context_id"``
  string — a ``db_column`` of a ForeignKey, not a concrete field.
  Any search term raised ``FieldError: Unsupported lookup
  'icontains' for ForeignKey``. Replaced with
  ``context__id__exact`` which Django's ORM optimises into a
  direct comparison on the underlying column, no JOIN.

* **Async decorators silently no-op'd (0.4, Added).**
  ``@with_context`` / ``@with_change_reason`` wrapped ``async def``
  targets with a sync ``with`` block that closed before the
  coroutine ran. Every event fired inside the task had an empty
  context. Fixed by ``iscoroutinefunction`` autodetect +
  ``async`` wrapper.

## Lessons

Themes recurring across the bugs above:

1. **UI-layer bugs evade unit tests.** The queryset helpers were
   well-tested in isolation; the integration between them and
   the PL/pgSQL trigger was not. The E2E test that catches the
   whole class lives in
   ``tests/integration/test_django_history_pg.py`` — one real
   install-trigger → mutate → admin-history-render round trip.
2. **Template field references are silent failures.** Django
   templates return empty strings for missing attributes. Two bugs
   in this list (content_type, source/change_reason) were
   "template reads a field that doesn't exist on the model, shows
   nothing, nobody noticed". Every custom template change in
   auditrum should now come with an integration test that asserts
   rendered output.
3. **Declared-but-unpopulated columns are a liability.**
   ``content_type_id`` existed in the DDL but was never written.
   A lifetime of "why is this always NULL?" debugging got
   short-circuited by dropping the column outright.

## Integration patterns worth stealing

* **HTTP requests** — ``AuditrumMiddleware`` in ``MIDDLEWARE``,
  done. Context carries user, URL, method, session hash, request
  id automatically.
* **Celery tasks** — one-shot wiring:
  ```python
  # celery_config.py
  from auditrum.integrations.django.tasks import install_celery_signals
  install_celery_signals()
  ```
  Every task gets wrapped in ``auditrum_context(source="celery",
  task_name=…, task_id=…)`` via Celery signals. No per-task
  decoration needed.
* **Management commands / cron** — ``audit_tracked`` block inside
  ``transaction.atomic()``:
  ```python
  with transaction.atomic(), audit_tracked(source="cron", change_reason="nightly reindex"):
      ...
  ```

<!-- TODO(catalog): quote from their team on the integration experience, if approved. -->
