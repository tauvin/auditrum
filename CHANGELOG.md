# Changelog

All notable changes to **auditrum** are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project
aims at [Semantic Versioning](https://semver.org/spec/v2.0.0.html) — with
the usual caveat that 0.x minors may still carry breaking changes while
the API stabilises.

## [Unreleased]

Nothing yet. Open an issue or PR if there's something you'd like to see.

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

This release went through a three-pass independent review (senior dev,
security auditor, QA) before tagging. Every finding tagged CRITICAL or
HIGH was fixed; the rest are tracked for 0.3.1 / 0.4 in the project
issue tracker.

#### Hardening model

- **`SECURITY DEFINER` audit trigger functions.** Every audit trigger
  function — and the helper functions `_audit_attach_context`,
  `_audit_current_user_id`, `_audit_reconstruct_row`,
  `_audit_reconstruct_table` — now declare `SECURITY DEFINER` with
  `SET search_path = pg_catalog, public`. They run with the
  privileges of their owner (the migration role), not the calling
  app role. This is the prerequisite that lets the application role
  have direct `INSERT` on `auditlog` revoked entirely.
- **`auditrum harden` now revokes `INSERT` too**, and covers both
  `auditlog` and `audit_context`. In 0.2 the command only revoked
  `UPDATE` / `DELETE` / `TRUNCATE`, which left a forgery hole — a
  compromised app role could write its own audit rows directly.
  Together with the SECURITY DEFINER functions this closes the
  forgery path completely.
- **Two-role deployment model documented in `docs/hardening.md`**:
  separate `myapp_admin` (migrations, retention, function owner) and
  `myapp_runtime` (app traffic, SELECT-only on the audit tables,
  INSERT only via triggers). Includes a snippet for transferring
  function ownership after the fact.
- **Append-only verification in integration tests**. New tests in
  `tests/integration/test_hardening_pg.py` actually create a limited
  Postgres role, apply the hardening, and prove that
  `INSERT INTO auditlog` / `INSERT INTO audit_context` /
  `UPDATE auditlog` / `DELETE FROM auditlog` all raise
  `InsufficientPrivilege` from the app role, while
  `INSERT INTO widgets` still produces an audit row through the
  trigger path. Smoke tests for the admin role round-trip too.

#### Hash chain integrity

- **Canonical JSON encoding for the hash payload.** The chain now
  hashes `jsonb_build_object('id', NEW.id, 'changed_at',
  NEW.changed_at, …)::text` instead of the old `field1 || '|' ||
  field2 || …` concatenation. The naive separator-join allowed
  trivial collision attacks where a forged row whose `operation`
  field contained `|` could replicate a legitimate row's hash.
  The new encoding uses the JSON object structure as an
  unforgeable delimiter and is shared between the trigger and the
  server-side `verify_chain` query via a single
  `_CANONICAL_PAYLOAD_EXPR` constant.
- **Tail-row deletion detection via tip anchors.** A new
  `auditrum.hash_chain.get_chain_tip(conn)` function returns the
  current `(id, row_hash)` of the most recent row in the chain.
  `verify_chain(conn, expected_tip=...)` then accepts that anchor
  back, verifies the tip row is still present and unmodified, and
  reports `tip row missing`, `tip row_hash mismatch`, or
  `tip id missing` when an attacker truncates the tail. Without an
  anchor the LAG-based check is blind to deletion of the most recent
  rows. Document recommends storing the anchor periodically in a
  tamper-evident external store (S3 with Object Lock, separate WORM
  database, paper printout for the truly paranoid).
- **`SECURITY DEFINER`** on the chain trigger function too, matching
  the audit trigger model.

#### Context propagation safety

- **`_Context` is now genuinely immutable.** The previous
  implementation was a `namedtuple` whose `metadata` slot held a
  plain `dict` that nested `__init__` would mutate in place. Two
  async tasks sharing the same outermost context could lose-write
  each other's metadata. The dataclass is now `frozen=True` with
  `metadata` wrapped in `types.MappingProxyType`, and nested entries
  push a fresh `_Context` onto the `ContextVar` (with the outer id
  preserved and metadata merged copy-on-push) instead of mutating.
  Outer state is restored verbatim on inner `__exit__` — inner
  metadata cannot leak into the outer block.
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
  schema editor's connection.** The 0.3.0 alpha briefly wrapped
  `schema_editor.connection` (a Django `DatabaseWrapper`) in
  `PsycopgExecutor` (which expects a raw psycopg connection). Worked
  by accident on `psycopg` 3 for the default alias but was
  undocumented and fragile on multi-DB setups. `DjangoExecutor` now
  optionally accepts a `connection=` argument and goes through
  Django's cursor protocol explicitly. Tests cover both the default
  (lazy global connection) and explicit-connection modes.

#### Generator output validation

- **`auditrum_makemigrations` output is now verified to be
  loadable** by a new `TestAuditrumMakemigrationsLoadability` class.
  The previous tests only `--dry-run`'d and substring-matched the
  output. The new tests write the file to a tmp app dir, parse it
  with `compile()`, and re-import it via `importlib` to confirm
  `Migration.operations[0]` is a real `InstallTrigger` instance
  with the right `TrackSpec`. Includes a round-trip test for
  `log_condition` containing single quotes — caught no bugs but
  closes the door on future string-concat regressions.

#### Other security touches

- **Identifier validation tested at every public entry point.**
  Every `generate_*_sql` function, `TrackSpec`, `FieldFilter`,
  `fetch_blame`, `reconstruct_*`, and `verify_chain` now has a
  parameterised "rejects injection" test. The validation regex was
  always there; the test coverage closes the chance of a future
  refactor accidentally bypassing it.
- **Concurrency tests for `TriggerManager.sync()`**. New
  `tests/integration/test_sync_concurrency_pg.py` runs N parallel
  syncs against the same spec via `ThreadPoolExecutor` against a
  shared testcontainer, asserts the tracking table converges to
  exactly one row, and verifies the trigger function is actually
  installed in `pg_proc`. Confirms the per-trigger advisory lock
  story under load.

#### Compliance-grade story

Hash chain, REVOKE-on-audit, retention tooling, and tamper
detection together give the project a credible compliance-grade
story. See [`docs/hardening.md`](docs/hardening.md) for the
deployment guide and the role split.

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

[Unreleased]: https://github.com/tauvin/auditrum/compare/v0.3.0...HEAD
[0.3.0]: https://github.com/tauvin/auditrum/compare/v0.2.0...v0.3.0
[0.2.0]: https://github.com/tauvin/auditrum/releases/tag/v0.2.0
