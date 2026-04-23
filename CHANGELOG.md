# Changelog

All notable changes to **auditrum** are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project
aims at [Semantic Versioning](https://semver.org/spec/v2.0.0.html) — with
the usual caveat that 0.x minors may still carry breaking changes while
the API stabilises.

## [Unreleased]

This release kicks off the 0.4 API-stabilization cycle per
``ROADMAP.md``: public surface locked via ``__all__``, a strict static
typing gate enforced in CI, and a handful of latent bugs — plus one
real SQL-injection vector — surfaced by the typing pass.

### Security

#### SQL injection fix in ``auditrum.integrations.django.utils.set_var``

The helper previously built a ``SET {key} = %s`` statement via f-string
interpolation of the ``key`` argument, which meant any caller-controlled
``key`` could inject arbitrary SQL ahead of the ``=``. It now uses
``SELECT set_config(%s, %s, false)`` — both the GUC name and the value
go through psycopg's parameter binding, so there is no raw string path
for an attacker-controlled identifier to reach the wire.

Behaviour for well-formed callers is unchanged (session-level variable
assignment). Ill-formed keys that previously produced a SQL syntax
error — or worse, silently widened an injection — now raise an explicit
``InvalidParameterValue`` from Postgres.

The ``cursor`` parameter also gained a ``Protocol`` type annotation, so
misuse (passing a non-cursor) is caught by the type checker rather than
at runtime.

### Added

- **``auditrum_refresh_schema`` management command.** Safety
  valve for refreshing the PL/pgSQL helper bodies (``jsonb_diff``,
  ``_audit_attach_context``, ``_audit_current_user_id``, the
  ``_audit_reconstruct_*`` pair) against the currently-installed
  Python release. Idempotent — each helper is emitted as
  ``CREATE OR REPLACE FUNCTION`` so running repeatedly just
  overwrites with the current body. Supports ``--dry-run`` for
  auditing what would execute. Primarily exists so users who
  deploy outside the migration graph (raw psycopg, SQLAlchemy,
  emergency recovery) can self-heal after an upgrade.
- **Background-task context helpers.** New
  ``auditrum.integrations.django.tasks`` module with a per-task
  ``@audit_task(source="celery", **metadata)`` decorator and a
  one-shot ``install_celery_signals()`` helper that wires
  ``task_prerun`` / ``task_postrun`` to auto-wrap every Celery task
  in ``auditrum_context``. Middleware still covers HTTP only — this
  closes the ~80% of background-job cases that were hand-written
  ``with auditrum_context(...)`` blocks before. Works with Celery,
  RQ, Dramatiq, APScheduler — anything that invokes the decorated
  callable on the worker.
- **``@with_context`` / ``@with_change_reason`` are now async-aware.**
  Both decorators (and ``@audit_task``) detect ``async def`` targets
  via :func:`asyncio.iscoroutinefunction` and return an ``async``
  wrapper that keeps the context open across every ``await`` inside
  the task. The 0.3 sync wrappers closed the block *before* the
  coroutine was awaited, silently dropping metadata from every
  audit event emitted by the task body. Sync callsites behave
  exactly as before.
- **Strict static typing gate.** ``ty`` (Astral, pinned to ``0.0.31``)
  checks ``auditrum/`` core and ``auditrum/integrations/django/`` on
  every push and PR. Config lives in ``ty.toml`` for now; it moves into
  ``pyproject.toml`` as part of the 0.7 RC cleanup. SQLAlchemy and
  observability helpers stay out of the gate — both are marked
  ``public-experimental`` in the roadmap and get polished alongside
  other integrations in the 1.x line.
- **``django-stubs>=5.0``** pinned in the new ``typecheck`` extras group
  so the Django integration gets strict type coverage against the
  community stubs.
- **CI workflow** at ``.github/workflows/ci.yml`` running ``ty``,
  ``pytest``, and ``ruff`` on every push to ``main`` and every PR.
  ``publish.yml`` remains the release-time gate.
- **Property-based test suite** at ``tests/test_properties.py`` using
  ``hypothesis`` and ``pglast``. Four blocks per ROADMAP 0.4: identifier
  regex fuzz against ``validate_identifier``, ``FieldFilter`` ``only`` /
  ``exclude`` combinatorics, ``TrackSpec.build`` checksum stability, and
  render → parse round-trip that feeds every generated trigger SQL back
  through a real PostgreSQL parser. Catches template regressions
  (missing semicolons, unbalanced ``DO $$…$$`` blocks, bad quoting)
  that string-level assertions silently miss.
- **Coverage gate** at 80% floor (``.coveragerc``, wired into
  ``ci.yml``). Initial baseline; the target is 90% by the 0.7 RC per
  ROADMAP 0.4. New unit suites landed in this release to lift the floor:
  ``test_django_utils.py`` (locks in the ``set_var`` injection fix and
  the ``timezone.datetime`` bug fix), ``test_django_partitions_command``
  (covers the ``audit_add_partitions`` management command with a mocked
  psycopg connection), ``test_django_templatetags``,
  ``test_django_shell_context``, ``test_django_init``,
  ``test_django_audit`` (the legacy ``_LegacyRegistryView`` dict-proxy).
- **Dead-code audit** (``vulture`` + ``ruff F401/F811/F841``) across
  ``auditrum/`` core and the Django integration turned up 13 candidates,
  all of which were protocol-required parameters (``__exit__``
  ``exc_type``/``tb``, Django ``Operation.database_forwards``
  ``state``/``from_state``/``to_state``) or ``TYPE_CHECKING``-only
  imports used in string annotations. No real removals; documenting
  the clean result so future audits don't re-investigate the same
  false positives.
- ``__all__`` declarations on all public modules (core, Django
  integration, SQLAlchemy integration, observability). Locks the public
  surface so wildcard imports, static analysis, and auto-generated docs
  see a consistent view of what is and is not part of the supported API.

### Fixed

- ``auditrum.integrations.django.utils.resolve_field_value`` no longer
  crashes on string date values with ``AttributeError: module
  'django.utils.timezone' has no attribute 'datetime'``. The helper now
  uses ``datetime.fromisoformat`` from the standard library, which was
  the intended call — ``django.utils.timezone`` never exposed a
  ``.datetime`` attribute, so this code path raised for every caller
  that passed a string date.
- ``auditrum.integrations.django.shell_context`` now actually keeps the
  ``source="shell"`` stamp alive for the lifetime of the shell session.
  It previously called ``audit_tracked(...).__enter__()`` on a
  dangling reference — CPython immediately reclaimed the context
  manager, which triggered its ``__exit__`` and popped the stamp
  before the first query could see it. The fix binds the manager to a
  module-level name and registers an ``atexit`` hook to unwind it
  cleanly at process shutdown.
- ``link_to_related_object(obj, name=None)`` now declares
  ``name: str | None`` instead of ``name: str``. The runtime already
  accepted ``None`` via the ``name or str(obj)`` guard; the signature
  was simply wrong.
- ``auditrum blame`` no longer papers over its ``fmt`` argument with a
  stale ``# type: ignore[arg-type]`` comment. Runtime validation is
  unchanged; the signature now reflects it via ``cast(Literal[...])``
  after the existing runtime check.

### Changed

- **Breaking: ``auditlog.diff`` now stores a paired
  ``{field: {"old": <before>, "new": <after>}}`` shape for every
  operation.** The 0.3 shape was ``{field: new_value}`` on UPDATE,
  ``new_row`` on INSERT, ``old_row`` on DELETE — three different
  formats that forced every UI consumer to cross-reference
  ``old_data`` and special-case per-operation rendering. The new
  shape is self-sufficient: one iteration over ``log.diff.items()``
  renders a ``old → new`` timeline for any event. INSERT rows carry
  ``{"old": null, "new": value}`` per column; DELETE rows carry
  ``{"old": value, "new": null}``. The trigger now also writes
  ``diff`` for **every** operation (0.3 skipped it for INSERT/DELETE).

  **Upgrade path for existing data.** New rows use the new shape
  automatically after re-running ``auditrum_makemigrations`` +
  ``migrate`` (the trigger body is part of each tracked app's
  migration graph and drift-detected by ``TriggerManager``). Existing
  rows written under 0.3 stay in the old format unless you back-fill
  them. A one-off SQL script does the conversion:

  ```sql
  -- UPDATE rows: {field: new_val} → {field: {old: old_val, new: new_val}}
  UPDATE auditlog
  SET diff = (
      SELECT jsonb_object_agg(
          d.key,
          jsonb_build_object('old', old_data -> d.key, 'new', d.value)
      )
      FROM jsonb_each(diff) d
  )
  WHERE operation = 'UPDATE'
    AND diff IS NOT NULL
    AND NOT EXISTS (
        SELECT 1 FROM jsonb_each(diff) d
        WHERE jsonb_typeof(d.value) = 'object'
          AND d.value ? 'old' AND d.value ? 'new'
    );

  -- INSERT rows: historically had diff = NULL → rebuild from new_data
  UPDATE auditlog
  SET diff = (
      SELECT jsonb_object_agg(
          d.key,
          jsonb_build_object('old', NULL, 'new', d.value)
      )
      FROM jsonb_each(new_data) d
  )
  WHERE operation = 'INSERT'
    AND diff IS NULL
    AND new_data IS NOT NULL;

  -- DELETE rows: same story from old_data
  UPDATE auditlog
  SET diff = (
      SELECT jsonb_object_agg(
          d.key,
          jsonb_build_object('old', d.value, 'new', NULL)
      )
      FROM jsonb_each(old_data) d
  )
  WHERE operation = 'DELETE'
    AND diff IS NULL
    AND old_data IS NOT NULL;
  ```

  Run in a transaction on a quiet window; the UPDATE branch's guard
  clause (`NOT EXISTS … 'old' AND 'new'`) keeps the script idempotent
  so a partial run can be retried safely. For a GIN index on ``diff``,
  the queries fall back to a sequential scan regardless — partition
  by ``changed_at`` range if the table is large.
- **Error messages in the public API now include remediation hints**,
  not just "what's wrong". Auditing each ``raise ValueError`` /
  ``raise RuntimeError`` site produced five upgrades:
  ``FieldFilter.all() must not carry field names`` now tells the caller
  to use ``.only(...)`` or ``.exclude(...)``;
  ``FieldFilter.only()/exclude() requires at least one field`` explains
  the calling convention and points to ``FieldFilter.all()`` for the
  no-filter case; ``generate_trigger_sql`` with both ``track_only`` and
  ``exclude_fields`` now spells out what each argument does and that
  they're mutually exclusive; the retention-interval "Unsupported unit"
  error lists every recognised unit; and ``TriggerManager.bootstrap``
  points users at CREATE-privilege checks and the Postgres server log
  when the DuplicateTable retry path fails. The exception classes are
  unchanged so ``try / except ValueError`` still catches the same sites.
- **``TriggerManager.tracking_table`` is now a read-only property.** The
  value is still validated via ``validate_identifier`` once in
  ``__init__``; making the attribute read-only ensures the validated
  identifier cannot be swapped out post-construction and sneak an
  unchecked string into the f-string-built SQL in ``_fetch_stored``,
  ``list_installed``, ``_upsert_tracking``, or ``_delete_tracking``.
  Callers that only read ``mgr.tracking_table`` are unaffected; callers
  that *assigned* to it (not a documented pattern) now raise
  ``AttributeError``.
- ``_tracking_table_ddl(table_name)`` re-validates its argument at
  entry (defence in depth) so direct internal misuse can't bypass the
  check established at ``TriggerManager.__init__``.

### Removed

- **Breaking:** the ``_validate_ident`` legacy alias in
  ``auditrum.tracking.spec`` and ``auditrum.triggers``. The canonical
  name has been ``validate_identifier`` since 0.3.0; the underscore-
  prefixed alias was only retained as a drop-in for 0.2 callers. Import
  ``validate_identifier`` directly. Nothing else in the public surface
  changed.
- **Breaking:** ``auditlog.content_type_id`` column plus
  ``AuditLog.content_type`` / ``AuditLog.content_object``
  (``GenericForeignKey``) on the Django model. The column was never
  populated by the framework-agnostic trigger path and every
  ``AuditLog.objects.filter(content_type=…)`` quietly matched zero
  rows. The 0.3 admin history page and the ``linked_object`` admin
  column both routed through this path and were effectively broken —
  history rendered "No audit records found" for every row, and
  ``linked_object`` rendered "-" for every entry. The canonical
  identity key has been ``table_name`` since 0.3.0 (that is what
  ``AuditLog.objects.for_object`` / ``for_model`` already keyed off),
  so the GenericForeignKey was dead weight that pulled
  ``django.contrib.contenttypes``-specific state into a
  framework-agnostic schema. A new ``0002_drop_content_type_id``
  Django migration drops the column for existing installs; fresh
  installs skip it at ``0001_initial`` time.

### Fixed

- ``AuditHistoryMixin.object_history_view`` (Django admin "History"
  tab) now actually renders audit events instead of a blank table.
  The 0.3 view filtered ``AuditLog.objects.filter(content_type=…)``,
  but the PL/pgSQL trigger never wrote ``content_type_id`` — so the
  admin page silently reported "No audit records found" even when
  the ``auditlog`` table held rows for the object. The view now
  routes through :meth:`AuditLogQuerySet.for_object`, the same path
  the rest of the public API already used. Also adds
  ``django.contrib.admin.utils.unquote`` on the ``object_id`` URL
  kwarg and the standard "object does not exist" redirect so the
  view behaves like the built-in admin history view.
- ``AuditLogAdmin.linked_object`` resolves the target instance from
  ``log.table_name`` (via a new ``model_for_table`` helper) instead
  of the always-NULL GenericForeignKey, so the admin list view's
  "Linked Object" column renders a real link again.
- ``render_log_changes`` (shared template helper for diff rendering)
  resolves the model class from ``log.table_name`` for the same
  reason — the 0.3 implementation read ``log.content_type`` and
  always returned the em-dash fallback.
- **Admin history template** no longer renders permanently-empty
  ``Source`` and ``Reason`` columns. ``object_history.html`` read
  ``{{ log.source }}`` and ``{{ log.change_reason }}`` — neither
  field exists on :class:`AuditLog` (both live under
  ``audit_context.metadata``), so both columns were silently blank
  for every row. The template now reads through
  ``log.context.metadata.source`` and
  ``log.context.metadata.change_reason`` with an em-dash default so
  events without an attached context render cleanly too. Same bug
  class as the content_type fix above — both caught by the new
  ``tests/integration/test_django_history_pg.py`` regression suite.
- **UPDATEs that set a column to ``NULL`` are no longer silently
  dropped from ``auditlog.diff``.** The 0.3 trigger wrapped the
  diff in ``jsonb_strip_nulls``, which removed entries with null
  values — so ``UPDATE orders SET reviewed_at = NULL WHERE id = 1``
  produced an audit row whose ``diff`` was an empty object. The
  paired shape makes the wrapper obsolete (the outer value is
  always a ``{"old", "new"}`` object, never null) so we removed it.
  Null-target UPDATE events now appear in ``diff`` as
  ``{field: {"old": <prev>, "new": null}}``. The ``test_update_to_null_value_is_not_dropped``
  integration test locks the behaviour in.
- **Admin ``AuditContextAdmin`` surfaces events and metadata
  directly.** The list view now shows ``source``, ``user``, and
  ``change_reason`` pulled from ``AuditContext.metadata`` as
  top-level columns — no more clicking into every row to see which
  request produced what. The detail view gains a read-only
  ``Events in this context`` link that opens the pre-filtered
  ``AuditLog`` changelist (``?context=<uuid>``); a full inline
  would OOM the page under bulk operations that fan out to thousands
  of events per context, so we route through the paginated
  changelist instead.
- **Admin history template renders a real ``old → new`` diff.** The
  default ``object_history.html`` now iterates ``log.diff.items``
  with the paired shape — ``<del>{{ change.old }}</del> →
  <ins>{{ change.new }}</ins>`` — and escapes to an ``∅`` glyph when
  a side is null (INSERT / DELETE boundaries). The 0.3 template
  rendered the raw ``{{ log.diff }}`` blob in a ``<pre>`` tag, which
  required every project to ship a ``get_item`` template filter just
  to cross-reference ``old_data``.
- **Admin search box no longer 500s.**
  ``AuditLogAdmin.search_fields`` included the bare string
  ``"context_id"`` — but that's only the ``db_column`` of the
  ``context`` FK, not a concrete model field. Django's default
  ``__icontains`` lookup raised ``FieldError: Unsupported lookup
  'icontains' for ForeignKey or join on the field not permitted.``
  the moment an operator typed anything into the search box. The
  fix routes the lookup through ``context__id__exact`` — UUIDs are
  unique identifiers, substring matching has no use, and ``exact``
  avoids the ``UPPER(uuid::text)`` cast that ``iexact`` would need.
  Django's ORM recognises that ``id`` is the PK of the referenced
  model and the FK column already holds that value, so the compiled
  SQL is a direct ``auditlog.context_id = <uuid>`` — it hits the
  existing ``auditlog_context_id_idx`` btree without joining
  ``audit_context``. Lived in ``auditrum/integrations/django/admin.py``
  since 0.3; the content_type bug masked earlier failures by
  returning an empty changelist before the search filter ran.
- **``Model.objects.create`` no longer returns a UUID as the pk.**
  Reported by the eridan-catalog team on 0.4.2 after integrating
  the async-ORM fix below. The context-propagation wrapper prepends
  ``SELECT set_config(...);`` to every user statement — turning a
  single ``INSERT … RETURNING id`` into a two-statement submission.
  psycopg3 leaves the cursor on the *first* result set after
  ``execute``, which is the ``SELECT set_config`` row. That row's
  first column is the context UUID (``set_config`` returns the
  value it was set to), so Django's ORM ``cursor.fetchone()``
  picks up the UUID string and assigns it to ``instance.pk`` —
  breaking every downstream ``.save()``, FK-assignment, and
  ``filter(pk=…)`` call, because ``int('d93aa383-…')`` raises
  ``ValueError``. The database row itself was correct (bigint
  ``id`` populated by the serial sequence); only the in-memory
  ``pk`` was corrupt.

  The bug existed latently since the injection pattern was
  introduced, but was masked in pre-0.4.2 releases by the old
  wrapper-registration shape: async ORM went through an unwrapped
  connection (the original async-ORM bug), and sync code paths
  that happened to read ``cursor.rowcount`` or ``.lastrowid``
  instead of ``fetchone()`` escaped the issue. The 0.4.2
  connection-wide signal-based registration put the wrapper on
  *every* connection, so every ``create()`` now goes through it —
  and the latent bug surfaced on the primary ORM code path.

  The fix is a single ``cursor.nextset()`` call inside
  ``_inject_audit_context`` after the user statement executes.
  That advances the cursor past the ``set_config`` result onto
  the user query's result set, so ``fetchone()`` /
  ``fetchall()`` / ``rowcount`` all see exactly what they would
  without the wrapper. Regression tests in
  ``tests/integration/test_django_history_pg.py``
  (``test_insert_returning_inside_context_returns_real_id`` and
  ``test_django_orm_create_inside_context_has_int_pk``) drive the
  raw-cursor and ORM code paths against a real Postgres.
- **Async Django ORM writes now get the right ``context_id``.**
  Pre-fix, ``auditrum_context`` registered its execute wrapper by
  calling ``connection.execute_wrapper(...)`` — where ``connection``
  is a thread-local proxy resolving to the current thread's
  ``DatabaseWrapper``. Django's async ORM dispatches SQL onto
  thread-pool workers via ``sync_to_async``; each worker has its
  own per-thread ``DatabaseWrapper`` that never saw the wrapper, so
  ``await Model.objects.acreate(...)`` / ``asave`` /
  ``afilter(...).aupdate`` silently wrote audit rows with
  ``context_id = NULL`` despite the calling task's ContextVar
  being propagated correctly by ``asgiref``. The attribution gap
  was loudest in admin rendering where every async-origin event
  showed a blank "Source" column, but also quietly broke any
  query keyed on ``context_id`` downstream.

  The wrapper is now registered once per ``DatabaseWrapper`` via
  the ``django.db.backends.signals.connection_created`` signal,
  plus a one-time walk of ``connections.all()`` in
  ``PgAuditIntegrationConfig.ready`` to cover wrappers that
  already exist when the app boots. The wrapper short-circuits
  when ``_tracker.get() is None`` so permanent registration costs
  one dict lookup per query outside an active context — nothing
  measurable on a real workload. ``auditrum_context.__enter__`` no
  longer needs the per-entry hook lifecycle; it only sets the
  ContextVar now. New integration test
  ``tests/integration/test_django_history_pg.py::test_async_orm_propagates_context``
  drives a real ``sync_to_async(thread_sensitive=False)`` dispatch
  and asserts ``log.context.metadata`` is populated from the
  outer-thread context — the sync-only unit tests can't reproduce
  the thread-pool path.
- **Upgrading from 0.3.x no longer silently keeps the old
  ``jsonb_diff`` body.** Schema helpers like ``jsonb_diff``,
  ``_audit_attach_context``, ``_audit_current_user_id``, and the
  ``_audit_reconstruct_*`` pair were emitted exactly once by
  ``auditrum_django.0001_initial`` and never refreshed on upgrade.
  Users who ``pip install -U auditrum && migrate`` ended up with
  DB-side function bodies frozen at the 0.3 revision — new audit
  rows were written in the pre-0.4 ``{field: new_value}`` diff
  shape despite the library being on 0.4. Two mechanisms close
  the gap: a new ``auditrum_django.0003_refresh_schema_04``
  migration re-emits every version-dependent helper via
  ``CREATE OR REPLACE FUNCTION`` on ``migrate``, and a new
  ``auditrum_refresh_schema`` management command does the same
  thing on demand as an ops escape hatch (also supports
  ``--dry-run`` for review). Integration test
  ``tests/integration/test_refresh_schema_pg.py`` replaces a live
  ``jsonb_diff`` with the 0.3 body, runs both the migration and
  the command, and asserts the paired body is restored — exactly
  the regression catalog's 0.4.1 upgrade hit.

  From 0.4 onwards, every release that changes a
  ``generate_*_sql`` body ships a corresponding
  ``auditrum_django.000N_refresh_schema_*`` migration alongside
  the version bump.
- **End-to-end admin-history regression test.** New
  ``tests/integration/test_django_history_pg.py`` installs a real
  trigger via ``TriggerManager``, mutates a row, and asserts
  :meth:`AuditLogQuerySet.for_object` returns the events — a path
  neither ``test_mixins.py`` (pure ORM) nor ``test_trigger_roundtrip.py``
  (pure SQL) covered. Closes the coverage gap that let the
  content_type bug ship in 0.3. Also asserts
  ``log.context.metadata`` is populated end-to-end, locking in the
  template fix.

## [0.3.1] — 2026-04-14

A security and correctness follow-up to 0.3.0. After tagging 0.3.0 the
codebase went through a three-pass independent review (senior dev,
security auditor, QA) which surfaced 21 findings — 5 critical, 6 high,
10 medium. **All 21 are fixed in this release.** No new public APIs
were broken; the upgrade is drop-in for projects already on 0.3.0.

### Security

#### Hardening model — `auditrum harden` is now actually append-only

In 0.3.0 the `auditrum harden` command revoked `UPDATE` / `DELETE` /
`TRUNCATE` from `PUBLIC` but **left `INSERT` intact**, on the
assumption that audit triggers needed it to write rows. They didn't —
because the trigger functions were `SECURITY INVOKER` (the default),
they actually ran with the calling role's privileges and would have
broken the moment INSERT was revoked. The result was a marketing claim
("application can only append truthfully") that the code didn't deliver:
a compromised app role could write its own forged audit rows directly
via `INSERT INTO auditlog (...) VALUES ('FAKE_USER', ...)`.

- **`SECURITY DEFINER` audit trigger functions.** Every audit trigger
  function — and the helpers `_audit_attach_context`,
  `_audit_current_user_id`, `_audit_reconstruct_row`,
  `_audit_reconstruct_table`, and the `<table>_hash_chain_trigger`
  function — now declare `SECURITY DEFINER` with
  `SET search_path = pg_catalog, public`. They run with the
  privileges of their owner (the migration role), not the caller.
- **`auditrum harden` now revokes `INSERT` too**, on both `auditlog`
  and `audit_context`. The new `--context-table` flag covers custom
  context table names. `generate_grant_admin_sql` grants the admin
  role full privileges on both tables.
- **Two-role deployment model documented in `docs/hardening.md`**:
  separate `myapp_admin` (migrations, retention, function owner) and
  `myapp_runtime` (app traffic, SELECT-only on the audit tables,
  INSERT only via triggers). Includes a snippet for transferring
  function ownership after the fact via `ALTER FUNCTION ... OWNER`.
- **Append-only verification in integration tests**. New tests in
  `tests/integration/test_hardening_pg.py` create a limited Postgres
  role, apply the hardening, and prove that
  `INSERT INTO auditlog` / `INSERT INTO audit_context` /
  `UPDATE auditlog` / `DELETE FROM auditlog` all raise
  `InsufficientPrivilege` from the app role, while
  `INSERT INTO widgets` still produces an audit row through the
  trigger path. Smoke tests for the admin role round-trip too.

#### Hash chain integrity

- **Canonical JSON encoding for the hash payload.** The 0.3.0 chain
  hashed `field1 || '|' || field2 || ...` text concatenation, which
  allowed trivial collision attacks: a forged row whose `operation`
  field contained `|` could replicate a legitimate row's hash. The
  new payload is `jsonb_build_object('id', NEW.id, 'changed_at',
  NEW.changed_at, ...)::text` — JSON object structure is an
  unforgeable delimiter. Trigger and `verify_chain` share a single
  `_CANONICAL_PAYLOAD_EXPR` constant so they cannot drift.
- **Tail-row deletion detection via tip anchors.** New
  `auditrum.hash_chain.get_chain_tip(conn)` returns the current
  `(id, chain_seq, row_hash, changed_at)` of the most recent chained
  row. `verify_chain(conn, expected_tip=...)` accepts that anchor
  back, verifies the tip row is still present and unmodified, and
  reports `tip row missing`, `tip row_hash mismatch`, or
  `tip id missing` when an attacker truncates the tail. Without an
  anchor the LAG-based check is blind to deletion of the most recent
  rows. Document recommends storing the anchor periodically in a
  tamper-evident external store (S3 with Object Lock, separate WORM
  database, paper printout for the truly paranoid).
- **`chain_seq bigint` column + dedicated sequence assigned inside
  the advisory lock.** The 0.3.0 chain used `id` for ordering, but
  Postgres assigns serial defaults *before* BEFORE INSERT triggers
  fire — concurrent transactions could grab `id=10` and `id=11` in
  reverse commit order, producing a chain whose `prev_hash` pointers
  didn't form a contiguous line. `chain_seq` is now allocated from
  a dedicated sequence **after** the trigger takes the per-table
  advisory lock, guaranteeing strict monotonicity in lock-acquisition
  order. Verify orders by `chain_seq NULLS FIRST, id` so legacy
  rows from before this change still chain correctly.
- **Advisory lock now uses `hashtextextended('table', 0)`** (64-bit)
  instead of `hashtext('table')` (32-bit). Eliminates the chance of
  advisory-lock-key collisions with other lock users sharing the
  database.

#### Context propagation safety

- **`_Context` is now genuinely immutable.** The 0.3.0 implementation
  was a `namedtuple` whose `metadata` slot held a plain `dict` that
  nested `__init__` would mutate in place. Two async tasks sharing
  the same outermost context could lose-write each other's metadata.
  The dataclass is now `frozen=True` with `metadata` wrapped in
  `types.MappingProxyType`, and nested entries push a fresh
  `_Context` onto the `ContextVar` (with the outer id preserved and
  metadata merged copy-on-push) instead of mutating. Outer state is
  restored verbatim on inner `__exit__` — inner metadata cannot leak
  into the outer block.
- **GUC names are now bound parameters in the runtime injection.**
  The `_inject_audit_context` execute_wrapper used to f-string
  interpolate `audit_settings.guc_id` / `audit_settings.guc_metadata`
  into the prefix SQL. Both are now passed as bound parameters and
  the `AuditSettings` properties validate them against
  `^[a-z_][a-z0-9_]*\.[a-z_][a-z0-9_]*$` (the Postgres custom GUC
  name format). Defence in depth even though the trust boundary is
  Django settings.
- **Legacy `AuditContext.use()` switched to `is_local=true`.** The
  cursor-based legacy path used `set_config(..., false)` (session
  scope), which leaked across requests on pooled connections
  (pgbouncer, Django `CONN_MAX_AGE > 0`). It now uses
  transaction-local semantics with a clear docstring warning that
  `audit_tracked` requires being inside `transaction.atomic()` for
  correct behaviour. HTTP traffic should use the
  execute_wrapper-based `auditrum_context` instead.
- **Session token PII protection.** `AuditrumMiddleware` no longer
  stores the raw `session_key` (a live bearer token) in the audit
  context. By default it stores
  `hmac.sha256(SECRET_KEY, session_key)[:16]` — short enough to be a
  useful correlation id, long enough to be collision-resistant per
  deployment, and one-way so a leaked audit log doesn't compromise
  active sessions. Controlled by `PGAUDIT_HASH_SESSION_KEY` (default
  `True`). A companion `PGAUDIT_REDACT_USER_AGENT` setting drops
  the user-agent header for strict GDPR setups.

#### CTE statement handling

- **`WITH` added to `IGNORED_SQL_PREFIXES`.** The execute_wrapper
  prefixes `SELECT set_config(...);` to every injectable statement.
  Django emits `WITH ...` for some `QuerySet.annotate()` chains; the
  resulting `SELECT set_config(...); WITH ...` was invalid SQL.
  CTE statements now bypass injection entirely.

#### Migration operation correctness

- **`InstallTrigger` operation now uses `DjangoExecutor` with the
  schema editor's connection.** The 0.3.0 path wrapped
  `schema_editor.connection` (a Django `DatabaseWrapper`) in
  `PsycopgExecutor` (which expects a raw psycopg connection). Worked
  by accident on `psycopg` 3 for the default alias but was
  undocumented and fragile on multi-DB setups. `DjangoExecutor` now
  optionally accepts a `connection=` argument and goes through
  Django's cursor protocol explicitly. Tests cover both the default
  (lazy global connection) and explicit-connection modes.

#### Generator output validation

- **`auditrum_makemigrations` output is now verified to be loadable**
  by a new `TestAuditrumMakemigrationsLoadability` class. The 0.3.0
  tests only `--dry-run`'d and substring-matched the output. The new
  tests write the file to a tmp app dir, parse it with `compile()`,
  and re-import it via `importlib` to confirm
  `Migration.operations[0]` is a real `InstallTrigger` instance with
  the right `TrackSpec`. Round-trip test for `log_condition` with
  single quotes — caught no bugs but closes the door on future
  string-concat regressions.

#### Defence in depth

- **`uninstall_by_name` re-validates identifiers** even when reading
  trigger/table names from the tracking table. If an attacker can
  write to `auditrum_applied_triggers`, they should not also get
  DDL execution via a maintenance call.
- **Blame markup escape.** `auditrum blame` (rich mode) now escapes
  `[` characters in user-controlled metadata fields (`username`,
  `change_reason`, `source`) to prevent terminal markup injection
  via attacker-controlled strings like `[red]VICTIM[/red]`.
- **Concurrency tests for `TriggerManager.sync()`**. New
  `tests/integration/test_sync_concurrency_pg.py` runs N parallel
  syncs against the same spec via `ThreadPoolExecutor` against a
  shared testcontainer, asserts the tracking table converges to
  exactly one row, and verifies the trigger function is actually
  installed in `pg_proc`. Confirms the per-trigger advisory lock
  story under load.

### Added

- **`auditrum.hash_chain.get_chain_tip(conn)`** — capture the current
  chain tip as `(id, chain_seq, row_hash, changed_at)` for external
  anchoring against tail-row deletion.
- **`verify_chain(conn, expected_tip=...)`** — verify the chain
  against a previously captured tip anchor, detecting truncation that
  the LAG-based check is blind to.
- **`auditrum.tracking.spec.validate_identifier`** — public name for
  the previously-private `_validate_ident`. The underscore-prefixed
  alias remains for backwards compatibility.
- **`reconstruct_table(conn, ..., stream=True)`** — server-side
  named-cursor streaming for whole-table time-travel queries on
  large logs. The default `stream=False` keeps the previous
  ``fetchall()`` behaviour for small tables.
- **Django settings**: `PGAUDIT_HASH_SESSION_KEY` (default `True`),
  `PGAUDIT_REDACT_USER_AGENT` (default `False`), `PGAUDIT_GUC_ID`
  and `PGAUDIT_GUC_METADATA` (now validated against the Postgres
  custom GUC name format).
- **`DjangoExecutor(connection=...)`** — optional explicit
  connection argument for use inside migration operations.

### Changed

- **Retention intervals are now calendar-aware.** `_parse_interval`
  returns a `dateutil.relativedelta` instead of `timedelta`. `1 year`
  now means one calendar year (handles leap years correctly), and
  `6 months` means six calendar months (e.g. April 14 → October 14)
  rather than 180 days. This matters for GDPR retention deadlines
  that map to calendar boundaries.
- **`auditrum status` CLI command** now matches triggers via the
  `audit_<table>_trigger` name pattern instead of the legacy
  `audit_trigger_fn` action_statement substring (which was renamed
  in 0.3 and silently reported zero triggers in the meantime).
- **`specs_by_app_label` is now O(M+N) instead of O(M×N)** and
  emits a `logging.warning` when a registered spec references a
  `db_table` that no longer matches any installed model (e.g. the
  user renamed the model after `@track`-ing it). Previously the
  spec was silently dropped from the migration output.
- **`TriggerManager.__init__`** moved its `validate_identifier`
  import to module level — minor consistency cleanup.

### Fixed

- All findings from the pre-release review (5 critical / 6 high /
  10 medium). The full breakdown is above.

### Migration from 0.3.0

Most users do not need to do anything beyond `pip install
auditrum==0.3.1`. Two exceptions:

1. **Existing hash-chained logs** get a new `chain_seq` column and
   sequence the next time you run a migration that reaches
   `generate_hash_chain_sql`. Existing rows have `chain_seq = NULL`
   and continue to verify via the legacy id-ordering fallback. New
   rows get monotonic `chain_seq` and the chain is correct under
   concurrency from that point forward.
2. **If you ran `auditrum harden` against 0.3.0** and rely on the
   "append-only" guarantee, re-run it after upgrading. The 0.3.0
   command left `INSERT` intact; the 0.3.1 command also revokes
   `INSERT` and covers `audit_context`. You should also transfer
   trigger function ownership to a dedicated admin role — see the
   snippet in `docs/hardening.md`.

## [0.3.0] — 2026-04-14

This is a big one. Most of the internals have been rewritten — the audit
trigger generator, the Django integration, the context propagation, the
test suite — with the goal of making auditrum a genuinely
framework-agnostic Postgres audit library rather than "Django with some
extras". If you were on 0.2.0, expect a migration step (see
**Breaking changes** below).

### Added

#### Framework-agnostic core

- **`auditrum.tracking`** — a new top-level module that owns trigger
  generation and lifecycle:
  - `TrackSpec` — a frozen dataclass that declaratively describes an
    audit trigger for one table. `FieldFilter.all()` / `.only(...)` /
    `.exclude(...)` enforces the "pick one" invariant at construction time.
  - `TriggerBundle` — rendered install/uninstall SQL plus a deterministic
    checksum for drift detection.
  - `TriggerManager` — runtime controller with `bootstrap()`,
    `inspect()`, `install()`, `uninstall()`, `sync()`, `diff()`. Tracks
    applied triggers in an `auditrum_applied_triggers` table and guards
    concurrent installs with a `pg_advisory_xact_lock` keyed by trigger
    name, so two parallel deploys can't leave the DB inconsistent with
    the tracking table.
- **`ConnectionExecutor` protocol** (`auditrum.executor`) — the abstraction
  that decouples the core from any specific database client. Three
  executors ship: `NullExecutor` (no-op, safe default), `PsycopgExecutor`
  (raw psycopg), and `DjangoExecutor` (lives under the Django integration).

#### Tamper detection, retention, compliance tooling

- **SHA-256 hash chain** for tamper-evident audit logs
  (`auditrum.hash_chain`). `enable-hash-chain` and `verify-chain` CLI
  commands, plus server-side recomputation via a single `WITH` query so
  full-log verification doesn't ship rows to the client. Uses
  `pg_advisory_xact_lock` to serialise chaining under concurrent inserts.
- **Hardening helpers** (`auditrum.hardening`) — `generate_revoke_sql` and
  `generate_grant_admin_sql` plus a `harden` CLI command that revokes
  `UPDATE`/`DELETE`/`TRUNCATE` from `PUBLIC` (and optionally a named app
  role), turning the audit log genuinely append-only for the application.
- **Retention tooling** (`auditrum.retention`) — `generate_purge_sql`
  returns a safely parameterised `DELETE`, and `drop_old_partitions`
  detaches and drops whole month-partitions that are older than a given
  interval. Exposed as `auditrum purge --older-than '2 years'` and
  `--drop-partitions` for the WAL-friendly variant.

#### Per-request context as a first-class table

- **`audit_context` table** + `_audit_attach_context()` PL/pgSQL function.
  Every audit event stores a `context_id uuid` foreign key instead of
  duplicating user/session/IP into every row. Context rows are upserted
  **lazily** by the trigger — read-only requests pay zero write cost.
- **`_audit_current_user_id()`** PL/pgSQL helper reads `user_id` out of
  the context metadata JSONB and returns it as a typed integer, so the
  `auditlog.user_id` column is populated directly and queries like
  `AuditLog.objects.for_user(request.user)` hit a plain btree index
  instead of traversing jsonb.

#### Time travel

- **`auditrum.timetravel`** — reconstruct the state of any row (or any
  whole table) at any past timestamp, without PostgreSQL's
  `temporal_tables` extension:
  - `_audit_reconstruct_row(table, id, at)` and
    `_audit_reconstruct_table(table, at)` `STABLE` SQL helper functions.
  - Python wrappers `reconstruct_row`, `reconstruct_table`,
    `reconstruct_field_history`.
  - `HistoricalRow` value object with dict + attribute access plus a
    `.to_model(cls)` helper that instantiates an unsaved Django/SA model
    while gracefully ignoring columns that no longer exist.
  - CLI: `auditrum as-of orders 2025-01-01T00:00:00+00:00 --id 42`
    (single row) or without `--id` (streams the whole table as JSON Lines).

#### `auditrum blame`

- **Git-style history for one row**. `auditrum blame users 42` prints
  the full event timeline for that row; `auditrum blame users 42 email`
  narrows to events that touched the given column. Renders in three
  formats: `rich` (colored terminal), `text` (plain), `json` (machine
  readable). Backed by the same composite index as the ORM manager.

#### Django integration overhaul

- **`@track` decorator** and `register()` function in
  `auditrum.integrations.django` attach declarative `TrackSpec`s to
  Django models through a per-process registry.
- **Custom migration operations** `InstallTrigger` / `UninstallTrigger`
  are real `django.db.migrations.operations.base.Operation` subclasses
  with `deconstruct` / `database_forwards` / `database_backwards`, so
  auditrum triggers participate in the normal `migrate`, `sqlmigrate`,
  and `showmigrations` flow.
- **`auditrum_makemigrations` management command** — walks the
  `@track` registry, groups specs by the app label of their tracked
  model, and emits a Django migration file per app under
  `<app>/migrations/`. Dependencies on `auditrum_django.0001_initial`
  and the app's latest migration are resolved automatically.
- **`AuditLogQuerySet` / `AuditLogManager`** on the Django `AuditLog`
  model with single-table-aware helpers: `for_model(Model)`,
  `for_object(instance)`, `for_user(user_or_id)`, `for_context(uuid)`,
  `by_table(name)`, `recent(limit)`. All chainable.
- **`AuditedModelMixin`** grows a suite of time-travel helpers:
  - `instance.audit_events` — queryset of every event on the row
  - `instance.audit_at(timestamp)` — reconstructed `HistoricalRow` or `None`
  - `instance.audit_field_history('email')` — `[(ts, value), …]`
  - `Model.audit_history()` — class-level queryset of all events
  - `Model.audit_state_as_of(timestamp)` — iterator over every surviving row
- **Request-level context propagation** via Django's
  `connection.execute_wrapper`. `auditrum_context(...)` installs a
  wrapper that prepends
  `SELECT set_config('auditrum.context_id', …, true), set_config('auditrum.context_metadata', …, true); <real SQL>`
  to every query. Because the `set_config` and the user query are in the
  same statement, `is_local=true` (SET LOCAL semantics) works without
  wrapping the request in `transaction.atomic()`, and GUCs cannot leak
  between requests on pooled connections.
- **Lazy re-exports** in `auditrum.integrations.django.__init__.py` so
  importing the Django integration doesn't trip `AppRegistryNotReady`.

#### SQLAlchemy integration

- **`auditrum.integrations.sqlalchemy`** — new opt-in extra. Ships:
  - `SQLAlchemyExecutor` wrapping a `sqlalchemy.engine.Connection`,
    with an internal cursor adapter that translates psycopg `%s`-style
    parameters into SQLAlchemy `:name` bind markers.
  - `track_table(Table, fields=[...])` declarative helper.
  - `bootstrap_schema(engine)` — installs audit log, context table,
    helper functions, and default partition.
  - `sync(engine)` — one-line idempotent install of every registered
    spec through the same `TriggerManager` used by the Django path.
- Pair with Alembic's `env.py` if you want audit triggers to live in
  your migration timeline.

#### Observability

- **OpenTelemetry** (`auditrum.observability.otel`) — when
  `opentelemetry-api` is importable, `auditrum_context.__enter__`
  automatically reads the current span and merges `trace_id` /
  `span_id` into the audit context metadata, giving you a direct join
  between distributed traces and database-side audit events. Graceful
  no-op without OTel.
- **Prometheus** (`auditrum.observability.prometheus`) —
  `AuditrumCollector` registers with `prometheus_client.REGISTRY` and
  exposes an `auditrum_events{table, operation}` gauge sourced from a
  windowed `GROUP BY` against the audit log. Failures during scrape
  never crash the metrics endpoint.
- **Sentry** (`auditrum.observability.sentry`) —
  `add_breadcrumb_for_context` attaches audit context metadata as a
  Sentry breadcrumb, so exceptions captured downstream include the
  user/source/request-id trail.
- All three are soft dependencies. Install via
  `pip install auditrum[observability]`.

#### Templates as first-class files

- PL/pgSQL bodies for the audit trigger, `_audit_attach_context`,
  `_audit_current_user_id`, and the reconstruct functions now live as
  `.sql` files under `auditrum/tracking/templates/`. They're rendered
  with `str.format_map` + a strict mapping that raises on any missing
  placeholder, so a typo surfaces at load time instead of producing
  broken SQL in production.

#### Tests

- Test suite grew from ~0 to **295 unit tests + 30 integration tests**
  (skipped automatically when Docker is unavailable). Integration tests
  use `testcontainers[postgres]` and verify real trigger roundtrip,
  hash-chain tamper detection, time-travel against a real row
  lifecycle, lazy context attachment, and composite-index usage via
  `EXPLAIN`.

### Changed

- **Trigger generator is now declarative.** `TrackSpec.build()` renders
  SQL from template + `FieldFilter` + extra_meta_fields; the old
  f-string inside `triggers.py` is gone. `generate_trigger_sql()` still
  works as a legacy facade and preserves its historical error labels.
- **`auditlog` schema** now carries a `context_id uuid` column (FK-style
  reference to `audit_context`) and a `(table_name, object_id, changed_at
  DESC)` composite index. The standalone `object_id` btree is gone — the
  composite covers it via leftmost prefix. Existing GIN index on `diff`
  is preserved; the GIN on `meta` is dropped (`meta` is now reserved for
  per-row custom extras, not bulk request metadata).
- **`auditlog` partitions**: the initial `2000–2025` placeholder partition
  is replaced with a `DEFAULT` partition so a missed cron job can no
  longer block writes to tracked tables.
- **Middleware** uses the `connection.execute_wrapper` pattern instead of
  a one-shot `SET` at request entry, eliminating leak-across-requests on
  pooled connections.
- **`register()` legacy API** in `auditrum.integrations.django.audit`
  now delegates to the new `@track` decorator and keeps the old
  `registry` dict-shaped view as a read-only compatibility layer.
- **Python version**: `requires-python` bumped to `>=3.11` (the code was
  already using 3.10+ type syntax and 3.11 `datetime.UTC`, so 3.8/3.9/3.10
  classifiers were misleading and have been removed).
- **Dependencies cleaned up**: `dateutils` → `python-dateutil`, `dotenv`
  → `python-dotenv` (the previous names were *different* broken forks
  silently installed by a typo), added explicit `pydantic-settings` and
  `rich` entries that the code was already importing.
- **CLI entry point** fixed from `auditrum.cli:main` (a Typer callback
  that can't be the script entry) to `auditrum.cli:app`.

### Removed

- **`django-pgtrigger` dependency**. The Django integration used it
  briefly as a delivery mechanism for trigger installation, but we now
  own that layer end-to-end via `InstallTrigger` migration operations
  and `TriggerManager`. `pip install auditrum[django]` is lighter as a
  result, and we no longer track pgtrigger's release cadence.
- **Dead code in `integrations/django/utils.py`**: a leftover
  `get_changed_object()` helper referenced an undefined `apps` global
  and hard-coded an unrelated auction-domain model list. Gone.
- **Broken `from apps.audit.models import AuditLog`** at the top of
  `integrations/django/mixins.py` — a leftover from the original
  internal project. Fixed to import from our own models.

### Fixed

- **SQL injection in `revert.py`**: `record_id`, `log_id`, and
  identifier interpolations now go through `psycopg.sql.Identifier` /
  `sql.Literal`; user-supplied table names are validated against
  `^[A-Za-z_][A-Za-z0-9_]*$`. Test coverage prevents regressions.
- **SQL injection in the `status` CLI command**: identifier-building
  `f"SELECT COUNT(*) FROM {audit_table}"` replaced with
  `psycopg.sql.Identifier`.
- **Identifier validation in `generate_trigger_sql`**: all of
  `table_name`, `audit_table`, `track_only`, `exclude_fields`, and
  `extra_meta_fields` are now validated before being interpolated into
  the PL/pgSQL template.
- **Middleware never wrote context to the database** in 0.2.0: it only
  set Python `ContextVar`s. HTTP requests were effectively producing
  audit rows with empty `meta`. Fixed — the wrapper pipeline above
  guarantees every query inside a request carries its context.
- **`PgAuditSettings()` instantiated at module import time** made the
  CLI unusable without a `.env` file present. Moved to
  `@lru_cache`-backed lazy init.
- **CLI `--dry-run`** tried to open a database connection even when no
  DSN was needed (`init-schema --dry-run`, `generate-trigger --dry-run`,
  etc.). Now fully offline.
- **Stray `print()` in `generate_trigger_sql`** dumped the generated SQL
  to stdout every time the function was called from library code. Gone.
- **`mixins.py` SIM117**, various `ruff` cleanup, expired 2025-01-01
  initial partition bound.

### Security

- Initial hardening tooling: `auditrum harden` (REVOKE
  `UPDATE`/`DELETE`/`TRUNCATE` on the audit log), `enable-hash-chain`
  (SHA-256 row chain via `pgcrypto`), `verify-chain` for server-side
  chain verification. **Note:** the 0.3.0 implementation left
  `INSERT` intact and used `SECURITY INVOKER` trigger functions —
  see [0.3.1] for the full append-only model and a collision-resistant
  payload encoding. If you're shipping audit data anywhere it matters,
  upgrade to 0.3.1.
- Retention helpers (`auditrum purge`, partition drop) and a hardening
  guide at `docs/hardening.md`.

### Breaking changes

Things you have to do if you're upgrading from 0.2.0:

1. **Rerun migrations.** The audit log schema changed (context table,
   context_id FK, composite target index, dropped `meta` GIN, reconstruct
   functions, default partition). The `auditrum_django.0001_initial`
   migration handles all of it, but if you had custom migrations
   touching `auditlog` you'll need to reconcile manually.
2. **Trigger registration moved.** If you previously relied on the
   `post_migrate` signal that installed triggers, that's gone. Decorate
   your models with `@track(...)` (or call `register(Model, …)` in
   `audit.py`), run `./manage.py auditrum_makemigrations`, then the
   normal `./manage.py migrate`. Triggers are now proper Django
   migration operations.
3. **Middleware class name is the same** but the import path is the
   same; behaviour differs — it now requires `auditrum.integrations.django`
   to be in `INSTALLED_APPS` so the `AppConfig.ready()` hook installs
   the `DjangoExecutor`.
4. **`generate_trigger_sql(..., meta_fields=...)`** — the kwarg was
   renamed to `extra_meta_fields`. The old name is still accepted
   through the legacy `register()` facade, but direct callers of
   `generate_trigger_sql` need to rename.
5. **`requires-python` is now `>=3.11`**. If you were on 3.10 or older,
   you'll need to upgrade.

## [0.2.0] — 2025-05-16

Initial public release. Adds the Django integration, the Typer-based
CLI, and the first cut of trigger-based audit logging on partitioned
PostgreSQL tables.

[Unreleased]: https://github.com/tauvin/auditrum/compare/v0.3.1...HEAD
[0.3.1]: https://github.com/tauvin/auditrum/compare/v0.3.0...v0.3.1
[0.3.0]: https://github.com/tauvin/auditrum/compare/v0.2.0...v0.3.0
[0.2.0]: https://github.com/tauvin/auditrum/releases/tag/v0.2.0
