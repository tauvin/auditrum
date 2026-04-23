# API stability

The contract this document defines is what ``1.0`` will commit to.
Everything classified **public-stable** here will only change in
backwards-compatible ways until a hypothetical ``2.0`` release.
Everything **public-experimental** is in ``__all__`` but may change
shape between minors. Everything **private** is an implementation
detail — if you reach for it, expect breakage.

The rule for deciding "where is this name?" is simple:

* **In ``__all__`` + listed below as stable** → it's in the contract.
* **In ``__all__`` + listed below as experimental** → importable,
  documented, but explicitly marked "API may change". Usually lives
  under ``integrations/`` or ``observability/``.
* **Underscore-prefixed or not in any module's ``__all__``** →
  private. No stability guarantee; may change in any minor.

If you find a name that's used in real user code but isn't listed
here, that's a documentation bug — please file it.

## Versioning commitment

Starting with ``1.0``, the public-stable surface follows semver
strictly:

* **patch (1.x.y → 1.x.y+1):** bug fixes, stable surface unchanged.
* **minor (1.x → 1.x+1):** additive changes only. New names, new
  optional kwargs, new modules. No removals or semantic shifts
  on anything below.
* **major (1.x → 2.0):** anything goes, but only as a last resort,
  with a deprecation period in the preceding minor.

Pre-``1.0`` (``0.x``) releases **may** carry breaking changes in
minors. The upgrade notes in ``CHANGELOG.md`` under each version
document them.

## Public-stable surface

### Core (``auditrum``)

Top-level convenience re-exports for the most common workflows.

| Name                                    | Defined in                   | Purpose                                                              |
|-----------------------------------------|------------------------------|----------------------------------------------------------------------|
| ``AuditContext``                        | ``auditrum.context``         | Framework-agnostic context manager / ContextVar holder.              |
| ``audit_context``                       | ``auditrum.context``         | Global :class:`AuditContext` singleton.                              |
| ``audit_tracked``                       | ``auditrum.utils``           | ``with`` block for manual/cron/shell contexts.                       |
| ``with_context``                        | ``auditrum.context``         | Decorator to set context around a callable. Async-aware since 0.4.   |
| ``with_change_reason``                  | ``auditrum.context``         | Decorator to stamp ``change_reason``. Async-aware since 0.4.         |

### Schema generators (``auditrum.schema``)

Pure functions that render DDL for the audit log, context table,
helper functions, partitions, and JSON-diff utility. All validate
their identifier arguments at call time.

* ``generate_auditlog_table_sql``
* ``generate_auditlog_partitions_sql``
* ``generate_audit_context_table_sql``
* ``generate_audit_attach_context_sql``
* ``generate_audit_current_user_id_sql``
* ``generate_audit_reconstruct_sql``
* ``generate_jsonb_diff_function_sql``

### Trigger generation (``auditrum.triggers``)

The legacy function-level facade over ``auditrum.tracking``. Kept
stable for callers that only need to render one trigger's SQL.

* ``generate_trigger_sql(table_name, …) -> str``
* ``validate_identifier(name, label) -> str`` (also re-exported from
  :mod:`auditrum.tracking.spec`).

The dataclass-shaped ``TriggerSpec`` / ``build_trigger_spec`` pair in
the same module is **public-experimental** — see below.

### Tracking primitives (``auditrum.tracking``)

The canonical declarative API. New code should prefer these over
``auditrum.triggers``.

| Name                 | Purpose                                                                                             |
|----------------------|-----------------------------------------------------------------------------------------------------|
| ``TrackSpec``        | Frozen dataclass describing one trigger declaratively.                                              |
| ``FieldFilter``      | ``.all()`` / ``.only(*)`` / ``.exclude(*)`` — the "pick one" field filter for diffing.             |
| ``TriggerBundle``    | Rendered install/uninstall SQL + checksum for drift detection.                                     |
| ``TriggerManager``   | Runtime controller (``bootstrap``, ``install``, ``inspect``, ``sync``, ``diff``, ``uninstall``).   |
| ``TriggerStatus``    | ``INSTALLED`` / ``DRIFT`` / ``NOT_INSTALLED`` enum returned by ``inspect``.                        |
| ``TriggerAction``    | ``INSTALL`` / ``UPDATE`` / ``NOOP`` / ``REMOVE`` enum in ``SyncReport``.                           |
| ``SyncReport``       | Structured result of ``sync([...])``.                                                              |
| ``DiffEntry``        | Structured result of ``diff([...])``.                                                              |
| ``validate_identifier`` | Raises ``ValueError`` on non-``^[A-Za-z_][A-Za-z0-9_]*$`` names.                                |

### Executors (``auditrum.executor``)

The abstraction that decouples ``TriggerManager`` from any specific
database client.

* ``ConnectionExecutor`` — the ``Protocol``.
* ``CursorProtocol`` — the cursor shape we depend on.
* ``NullExecutor`` — safe no-op default.
* ``PsycopgExecutor`` — raw psycopg wrapper.

``DjangoExecutor`` lives in ``auditrum.integrations.django.executor``
and is also part of the stable surface.

### Time travel (``auditrum.timetravel``)

Reconstruct row / table / field state at an arbitrary past
timestamp, no ``temporal_tables`` extension required.

* ``reconstruct_row``
* ``reconstruct_table`` (``stream=True`` uses a server-side named
  cursor for memory-bounded iteration)
* ``reconstruct_field_history``
* ``HistoricalRow`` value object (dict + attribute access, with
  ``.to_model(cls)`` convenience for Django / SA models)

### Tamper evidence (``auditrum.hash_chain``)

* ``generate_hash_chain_sql`` — installs the BEFORE INSERT trigger
  and the ``chain_seq`` sequence.
* ``verify_chain`` — full / windowed verification with an optional
  ``expected_tip=`` anchor for tail-row detection.
* ``get_chain_tip`` — capture a tamper-evident tip anchor.

### Hardening (``auditrum.hardening``)

* ``generate_revoke_sql`` — REVOKE ``INSERT`` / ``UPDATE`` /
  ``DELETE`` / ``TRUNCATE`` from ``PUBLIC``.
* ``generate_grant_admin_sql`` — grant the admin role the
  privileges needed to own the trigger functions.

### Retention (``auditrum.retention``)

* ``drop_old_partitions`` — detach + drop partitions older than a
  calendar interval.
* ``generate_purge_sql`` — safely parameterised ``DELETE`` for
  row-level retention.

### Revert (``auditrum.revert``)

* ``generate_revert_sql``
* ``generate_revert_sql_from_log``
* ``get_revert_columns_from_log``

### Blame (``auditrum.blame``)

* ``BlameEntry`` value object.
* ``fetch_blame(conn, *, table, object_id, field=None, …)``
* ``format_blame(entries, *, fmt='text|rich|json', …)``

### CLI (``auditrum.cli``)

* ``app`` — the Typer ``typer.Typer()`` application. Exposed as the
  ``auditrum`` script entry in ``pyproject.toml``. Commands are
  treated as part of the stable surface individually; see
  ``auditrum --help`` or :doc:`cli`.

### Settings (``auditrum.settings``)

* ``PgAuditSettings`` — ``pydantic-settings`` model for the CLI
  configuration. ``@lru_cache``-constructed; environment-backed.

### Django integration (``auditrum.integrations.django``)

Everything needed to run auditrum in a Django project.

| Name                             | Defined in                                     | Purpose                                                                 |
|----------------------------------|------------------------------------------------|-------------------------------------------------------------------------|
| ``track``, ``register``          | ``…django.tracking`` (re-exported at package)  | Decorator + function form of the ``@track`` API.                        |
| ``AuditLog``, ``AuditContext``   | ``…django.models`` (re-exported at package)    | Django ORM models; ``managed=False``.                                   |
| ``AuditLogManager``              | ``…django.models``                             | Manager exposing ``for_model`` / ``for_object`` / ``for_user`` / …      |
| ``AuditLogQuerySet``             | ``…django.models``                             | Chainable queryset with the same helpers.                               |
| ``AuditedModelMixin``            | ``…django.mixins``                             | Per-instance helpers (``audit_events``, ``audit_at``, …).               |
| ``AuditHistoryMixin``            | ``…django.mixins``                             | ``ModelAdmin`` mixin adding the per-object history view.                |
| ``AuditrumMiddleware``           | ``…django.middleware``                         | Per-request ``execute_wrapper``-based context propagation.              |
| ``RequestIDMiddleware``          | ``…django.middleware``                         | Stamps ``request.request_id`` for downstream middleware.                |
| ``auditrum_context``             | ``…django.runtime``                            | Async- and decorator-usable per-block context manager.                  |
| ``current_context``              | ``…django.runtime``                            | Read-only accessor for the active ``_Context``.                         |
| ``InstallTrigger``, ``UninstallTrigger`` | ``…django.operations``                   | Real ``migrations.Operation`` subclasses.                               |
| ``DjangoExecutor``               | ``…django.executor``                           | ``ConnectionExecutor`` bound to Django's default connection alias.      |
| ``AuditLogAdmin``, ``AuditContextAdmin`` | ``…django.admin``                      | Auto-registered ``ModelAdmin``s.                                        |
| ``audit_task``                   | ``…django.tasks``                              | Per-task decorator (Celery / RQ / Dramatiq / APScheduler).              |
| ``install_celery_signals``       | ``…django.tasks``                              | One-shot Celery auto-wrapping via signals.                              |
| ``PgAuditIntegrationConfig``     | ``…django.apps``                               | The ``AppConfig`` subclass. Rarely referenced directly.                 |
| ``AuditSettings``, ``audit_settings`` | ``…django.settings``                      | Proxy over ``django.conf.settings`` for ``PGAUDIT_*`` values.           |

### Django utility helpers (``auditrum.integrations.django.utils``)

Helpers used by ``AuditHistoryMixin`` templates and by the admin; kept
public so users can reuse them in their own templates / admin classes.

* ``model_for_table`` — resolve a Django model class from its
  ``_meta.db_table``. Returns ``None`` for cross-service tables.
* ``render_log_changes`` — template helper producing an HTML diff
  from an :class:`AuditLog` row.
* ``resolve_field_value`` — model-aware field value → label
  rendering.
* ``get_user_display`` — user label resolver.
* ``link``, ``link_to_related_object`` — small HTML helpers.
* ``set_var`` — parametrised ``set_config`` wrapper (used by
  :class:`auditrum_context` internally; exposed for users who need
  to call it from outside the execute_wrapper path).
* ``audit_tracked`` — re-export for import convenience.

### Django per-app ``audit.py`` registry (``auditrum.integrations.django.tracking``)

* ``register(Model, **kwargs)``
* ``track(**kwargs)`` (decorator form)
* ``get_registered_specs()``
* ``specs_by_app_label()``
* ``clear_registry()`` (mostly used in tests)

The legacy dict-proxy ``registry`` in
``auditrum.integrations.django.audit`` is **public-experimental** —
see below.

## Public-experimental surface

These names are exported, documented, and tested — but their shape
is allowed to change in pre-1.0 minors *and* in the 1.x line (with
a deprecation notice in ``CHANGELOG.md``) until they graduate to
stable.

### SQLAlchemy integration (``auditrum.integrations.sqlalchemy``)

The entire module is experimental for the ``1.0`` release. Reason:
the 0.3 SQLAlchemy path works, but lacks Alembic-autogenerate parity
with ``auditrum_makemigrations``. Until that gap closes, the
public surface may shift to accommodate the Alembic integration.

* ``SQLAlchemyExecutor``
* ``track_table``
* ``sync``
* ``bootstrap_schema``
* ``registered_specs``
* ``clear_registry``

Scheduled to graduate in ``1.1``; see ``ROADMAP.md`` "Post-1.0
features — 1.1 SQLAlchemy first-class".

### Observability helpers (``auditrum.observability.*``)

Soft dependencies on OpenTelemetry, ``prometheus-client``, and
Sentry SDK. Each helper is a thin bridge between the host library
and auditrum, and the host APIs themselves evolve.

* ``auditrum.observability.otel.enrich_metadata``
* ``auditrum.observability.prometheus.AuditrumCollector``
* ``auditrum.observability.sentry.add_breadcrumb_for_context``

The helpers will only break in response to upstream API changes.
That's out of our control; we track the upstream changelog and
version-pin the optional extras accordingly.

### Legacy registry (``auditrum.integrations.django.audit``)

The dict-proxy view ``registry`` and the ``register`` function
exposed from this module are a 0.2-era compatibility shim over
``auditrum.integrations.django.tracking``. The new
``@track`` / ``register`` API is the recommended form. This module
stays in-tree so pre-0.3 ``audit.py`` files keep working; it may be
hidden behind an explicit opt-in or removed outright in 1.x.

### Legacy trigger dataclass (``auditrum.triggers``)

The function-level ``generate_trigger_sql`` and ``validate_identifier``
are stable (see above). ``TriggerSpec`` and ``build_trigger_spec``
are the pre-0.3 dataclass shape — superseded by
:class:`auditrum.tracking.TrackSpec` / :meth:`TrackSpec.build`. They
remain for callers that read ``spec.declare`` / ``.sql`` / etc. in
historical shapes. May be removed in ``1.x`` with a deprecation period.

## Private surface

Anything not in any module's ``__all__`` is private. In particular:

* Everything under ``auditrum.tracking._template``.
* ``auditrum.integrations.django.shell_context`` — the helper
  module that keeps the shell-entrypoint context alive. Imported
  only from ``PgAuditIntegrationConfig.ready()``.
* Internal PL/pgSQL template files in
  ``auditrum/tracking/templates/*.sql`` — the string contents are
  part of the tested SQL contract, not the public Python API.
* Any name starting with an underscore in any module (``_Context``,
  ``_apply_ctx``, ``_tracker`` ContextVar, ``_inject_audit_context``
  execute_wrapper callback, ``_hash_session_key``, etc.).

Private names may be renamed, split, or deleted in any minor
release. If you import one, you are on your own.

## Compatibility across pre-1.0 minors

Until ``1.0`` ships, the above classification describes the
**intended** shape of the stable surface — but pre-1.0 minors are
allowed to carry breaking changes. Each ``CHANGELOG.md`` entry
flags them explicitly under the ``### Removed`` or
``### Breaking`` heading. Notable in-flight examples:

* ``0.4`` removed the ``content_type_id`` column + the
  ``AuditLog.content_type`` / ``content_object`` GenericForeignKey
  fields (see ``CHANGELOG.md`` for migration SQL).
* ``0.4`` changed ``auditlog.diff`` to the paired
  ``{field: {old, new}}`` shape.
* ``0.4`` removed the ``_validate_ident`` legacy alias.

Once ``1.0`` ships, none of the above surface changes in a
backwards-incompatible way until ``2.0``. The ``1.x`` line may
*add* new names to ``__all__``, add optional arguments to public
functions, or relax (never tighten) parameter types — but not
remove or rename.
