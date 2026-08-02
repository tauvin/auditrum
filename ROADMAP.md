# Roadmap

This document tracks what auditrum needs to reach a stable 1.0 release.
It is **not** a wishlist — every item has an explicit definition of done
and a milestone it belongs to. Items get checked off as they ship.

## Strategic principle: smallest stable surface

The path to 1.0 is **not** about adding features. It is about
**locking down what already exists** so users can rely on it never
breaking. Every item in the pre-1.0 cycle either:

1. **Stabilises** an existing API (clarifies private vs public, types
   it strictly, locks it in ``__all__``), or
2. **Validates** that the existing API works under real production load
   (benchmarks, multi-version matrix, real users), or
3. **Documents** how to use the existing API in real deployments.

New features — SQLAlchemy parity, async support, FastAPI integration,
web UI, per-model table mode, GDPR pseudonymization — are explicitly
**post-1.0 work**. They become 1.x minor releases after the Django path
is rock solid. Adding them pre-1.0 expands the surface we have to lock
and delays the stability promise that 1.0 is supposed to give.

## What "1.0" means

A 1.0 release is a public commitment that the API surface in the
``__all__`` of every module will not break in any subsequent ``1.x``
release. Breaking changes get pushed to a hypothetical 2.0 with a
deprecation period. Before we make that promise, four things have to
be true:

1. **The public/private boundary is clearly drawn.** No more "private
   helper imported from seven files" situations. Every internal name
   is either firmly public (in ``__all__``, type-annotated, documented)
   or firmly private (underscore-prefixed, no external imports, may
   change without notice).
2. **The performance characteristics are measured and published.** We
   call ourselves "compliance-grade" — that's marketing until there are
   trigger-overhead, hash-chain throughput, and time-travel-latency
   numbers in the README, with the methodology to reproduce them.
3. **The version matrix is real.** CI tests the cartesian product of
   supported PostgreSQL × Python × Django versions, not one cell.
4. **Multiple production deployments exist.** Catalog + sister projects
   are committed; we want them shipping real audit data through 0.4–0.7
   and feeding bug reports back into the cycle.

## First production users — already onboarding

The library is being adopted at the same company that owns
**catalog** (an auction catalog with high-churn ``item`` and ``sale``
tables — ~7M audit events in pre-prod) plus a
few sister projects. Integration starts on **0.3.1** — they are the
real-world feedback loop for the entire pre-1.0 cycle, not a
"find users" milestone we worry about at the end.

Concrete consequences for the roadmap:

* **All early testing is Django.** Every project at the company runs
  on Django, so 0.4–0.7 development is exclusively Django-focused.
  SQLAlchemy / FastAPI / async support move out of the pre-1.0
  cycle entirely (see "Post-1.0 features" below).
* **Bug reports from catalog become regression tests.** Each issue
  catalog hits in production lands as a test in ``tests/`` before the
  fix ships. The 0.3.x / 0.4 reactive cycles exist specifically to
  catch the things that only surface under real production traffic —
  every bug caught so far (``content_type`` NULL rendering, paired
  diff format, admin search crash, template references to fields that
  don't exist) came from catalog integration, not internal QA.
* **Performance benchmarks are tied to catalog's real workload, not
  synthetic ones.** Numbers in the 0.5 README come from instrumented
  catalog pre-prod, not from a fake test schema. This makes the
  "compliance-grade" claim defensible with a concrete reference.
* **Security and compliance features mature against real catalog data.**
  If catalog's roadmap includes GDPR-relevant fields, pseudonymization
  timing shifts with their regulatory deadline — see "Open questions".

---

## Versions

### 0.3.x — Production hardening (shipped; reactive cycle now runs inside 0.4)

**Theme:** the catalog integration happened on 0.3.x. Everything that
broke in production became a 0.3.x bugfix release. Stays a "reactive"
cycle — no new features, only fixes for things catalog hits. The
fixes are now rolling up into 0.4 rather than a separate patch line,
since 0.4 itself has not shipped yet.

- [x] Workflow + CI green (0.3.1)
- [x] First catalog integration on the dev/staging cluster
- [x] Catalog rolls out to production
- [x] Bug fixes from production reports — each becomes a regression test
      (``content_type`` / admin template / paired diff / admin search
      ForeignKey crash are in ``[Unreleased]``)
- [ ] Sister project #1 starts integration
- [ ] Sister project #2 starts integration

**Done when:** Catalog is running in production for at least 2 weeks
without auditrum-related incidents and we've stopped receiving new
bug reports for a release cycle.

**Effort:** was 2-4 weeks reactive work; effectively folded into the
0.4 [Unreleased] diff.

---

### 0.4 — API stabilization & cleanup

**Theme:** draw the public/private boundary explicitly. Strict typing,
locked ``__all__`` lists, deprecation warnings for de-facto-private
names. The goal is that 0.4's public surface is **the same shape** as
1.0's public surface — only documentation and validation gets added
later.

- [x] **Walk every module, classify every name** as one of:
  - ``public-stable`` — in ``__all__``, will not break until 2.0
  - ``public-experimental`` — in ``__all__`` but explicitly marked
    unstable (e.g. ``observability/`` helpers)
  - ``private`` — underscore-prefixed, may change without notice
  - Documented in ``docs/api-stability.md``.
- [x] **Lock ``__all__`` lists** in every public module. Anything not in
  ``__all__`` is private. Anything in ``__all__`` is the contract.
- [x] **Resolve the ``_validate_ident`` situation.** Kept
  ``validate_identifier`` as the permanent public name and removed
  the underscore-prefixed alias outright (breaking; pre-1.0 is fine).
- [x] **Strict type checker in CI.** Originally ``ty`` (Astral,
  pinned ``0.0.31``) with ``error-on-warning = true``, covering
  ``auditrum/`` core and ``auditrum/integrations/django/`` only.
  **Superseded in 0.5.x:** the gate is ``pyrefly`` (pinned
  ``1.2.0``) run with ``--min-severity warn`` — same
  warnings-are-fatal contract — and it now covers the *whole*
  package, SQLAlchemy and observability helpers included, so the
  shipped ``py.typed`` marker is backed end to end.
- [x] **Hypothesis property-based tests** for the trigger generator:
  identifier fuzz, ``FieldFilter`` combinatorics, ``TrackSpec.build``
  checksum stability, render → parse round-trip via ``pglast``.
- [x] **Coverage gate in CI** at 80% floor (``.coveragerc`` +
  ``ci.yml``). Target 90% stays for 0.7 RC cleanup.
- [x] **Audit error messages** in the public API — five sites
  upgraded to produce remediation hints, not just complaints.
- [x] **Remove all dead code** found during the audit — ``vulture``
  + ``ruff F401/F811/F841`` pass; the 13 candidates were all
  protocol-required parameters or ``TYPE_CHECKING``-only imports.
- [x] **Reactive bug fixes from catalog integration** (folded into
  0.4 rather than a separate 0.3.x patch line):
  - ``content_type_id`` column + ``AuditLog.content_type``
    GenericForeignKey removed; admin history view routes through
    ``for_object`` instead of the NULL ``content_type`` filter.
  - ``auditlog.diff`` migrated to paired ``{field: {old, new}}``
    shape; ``jsonb_strip_nulls`` removed (was silently dropping
    ``UPDATE … SET x = NULL`` events).
  - ``object_history.html`` renders a real ``old → new`` diff and
    reads ``source`` / ``change_reason`` from ``log.context.metadata``.
  - ``@audit_task`` + ``install_celery_signals`` for Celery / RQ.
  - ``@with_context`` / ``@with_change_reason`` / ``@audit_task``
    became async-aware (``iscoroutinefunction`` autodetect).
  - ``AuditContextAdmin`` surfaces source / user / change_reason as
    columns + links to the pre-filtered ``AuditLog`` changelist.
  - ``AuditLogAdmin.search_fields`` no longer crashes on the
    ``context_id`` FK lookup.
  - First E2E integration test covering the "install trigger →
    mutate row → admin history view" path (catches the whole class
    of bug that slipped through unit tests).

**Done when:** the type gate and the coverage gate pass in CI;
``api-stability.md`` is published; catalog runs on 0.4 without new
bug reports caused by the cleanup.

**Effort:** ~3 weeks. This is the most important pre-1.0 milestone —
everything after it depends on a stable surface.

**Note on ``pyright``:** dropped from 0.4 and from the roadmap
entirely — a single type gate covers our needs, and running two type
checkers in CI doubles the failure modes without doubling the signal.
Decision locked in 2026-04-23; see Open questions below.

---

### 0.5 — Performance baseline & multi-version matrix

**Theme:** prove the claims hold under real production load. Benchmarks
driven by catalog's actual workload. Multi-version CI to lock the
support matrix before 1.0 makes a public commitment to it.

- [x] **Benchmark suite in ``benchmarks/``** using ``pytest-benchmark``:
  trigger overhead (INSERT/UPDATE/DELETE, ``track_only`` / ``log_condition``
  variants), time-travel ``reconstruct_row`` latency, hash-chain insert
  cost, and ``reconstruct_table`` streaming-vs-materialised memory
  (``benchmarks/test_stream_memory.py``). The **hash-chain throughput
  ceiling under concurrency** and the **10M-row** profile are deferred —
  see "Deferred measurements" below.
- [x] **Numbers published in docs.** ``docs/performance.md`` now carries
  real numbers from **two production deployments** (catalog pre-prod and
  bidwise prod, both PG 17): trigger overhead ~0.3–1.4 ms/write,
  time-travel ~5–11 ms at 570–2207 revisions, 25 GB / 10 GB footprints,
  ~49 events/s real prod peak — plus a reproducible reference
  microbenchmark and the methodology. The GIN-on-diff finding (0 scans on
  both deployments) drove making that index opt-in (#6).
- [x] **Multi-version CI matrix.** A 6-cell diagonal in ``ci.yml``
  covering the cartesian space without paying for all 30 combinations:
  - PostgreSQL **13, 14, 15, 16, 17** (one per cell, via the
    ``AUDITRUM_TEST_PG_IMAGE`` env the conftests read)
  - Python **3.11, 3.12, 3.13, 3.14**
  - Django **4.2 LTS, 5.2 LTS, 6.0** — support policy is "current LTS
    lines + latest feature release"; Django pinned per cell with
    ``uv pip install`` since ``uv.lock`` carries one version.
  Diagonal respects compatibility (Django 4.2 ≤ Py 3.12; Django 6.0 ≥
  Py 3.12). ``fail-fast: false`` and a per-cell uv cache suffix. Budget
  target ~10 min wall (cells run in parallel).
- [x] **Memory profiling for ``reconstruct_table``.** The ``stream=True``
  server-side cursor is measured (``benchmarks/test_stream_memory.py``):
  peak ~1.4 MB streaming vs ~38.6 MB materialised at 50k rows — bounded by
  the cursor batch, independent of row count. A run at true 10M-row volume
  is deferred (below). The benchmark also surfaced and fixed a real bug:
  streaming on an autocommit connection raised ``NoActiveSqlTransaction``
  (#13).
- [x] **Production metrics dashboard.** Grafana JSON in
  ``examples/grafana/`` — audit events/sec, trigger latency p50/p95/p99,
  hash-chain verify status, partition disk usage, retention lag.
- [x] **PyPI Trusted Publishing.** OIDC via
  ``pypa/gh-action-pypi-publish`` — confirmed working by the 0.4.x
  releases; ``secrets.PYPI`` is gone.

**Deferred measurements (run closer to 1.0, against real load):**
These need a quiet production window or a restored prod dump, so they are
intentionally postponed to the pre-release validation pass rather than
blocking 0.5:

- **Hash-chain throughput ceiling under concurrency.** Both production
  deployments run with the chain *disabled*, so there are no real-load
  numbers for the advisory-lock serialisation. A controlled load-test
  runbook (restore a bidwise dump locally, ``pgbench`` sweep chain-on vs
  -off across writer counts) is ready to run in a quiet window.
- **Connection-pooling smoke tests** — pgbouncer transaction mode + Django
  ``CONN_MAX_AGE > 0``, hunting ``is_local`` GUC leaks against a real
  pooled setup.
- **10M-row ``reconstruct_table`` memory profile** at true volume (the
  streaming bound is already measured at 50k).

**Done when:** ✅ benchmark suite landed; ``docs/performance.md`` carries
real numbers from two production deployments; CI matrix is green across
the support surface; auditrum is running in production (catalog + bidwise)
with measured overhead documented. The deferred load/concurrency
measurements move to the pre-release (RC) validation pass.

**Effort:** delivered. The deferred items are RC-window work; their risk
(if chain/concurrency numbers are poor) is carried into the RC, not 0.5.

---

### 0.6 — Documentation & production guide (shipped 2026-08-02)

**Theme:** turn ``docs/`` from "files written during development" into
a real production deployment manual that a new user can follow without
asking questions.

- [x] **API reference autogeneration** via ``mkdocs-material`` +
  ``mkdocstrings`` — ``docs/reference/api.md`` renders the public
  surface (tiered per ``api-stability.md``); published to GitHub Pages
  by ``docs.yml`` on merge to main. (PR #18)
- [x] **Production deployment guide** — ``docs/deployment.md``: the
  two-role split, retention/partition cron, Grafana monitoring,
  backup/restore (incl. ``auditrum_refresh_schema`` + chain
  re-anchoring), upgrades, and a rollback playbook. (PR #19)
- [x] **Migration cookbook** — greenfield / django-pghistory /
  django-simple-history / custom triggers, plus a real
  "remove a legacy in-house audit trigger" recipe from bidwise. (PR #11)
- [x] **Performance tuning guide** — track_only / log_condition /
  partition pruning / GIN-on-diff drop, cross-referencing the 0.5
  numbers. (PR #11)
- [x] **Catalog case study** — ``docs/case-studies/catalog.md`` with
  real pre-prod numbers (7.2M events, overhead, time-travel, the
  GIN finding). (PR #16)
- [x] **Sister-project case study** — ``docs/case-studies/bidwise.md``
  (production: ~49 events/s peak, deep histories, second GIN
  confirmation, the incomplete-migration lesson). (PR #16)
- [x] **First-time setup walkthrough** — ``docs/quickstart.md``, ~5
  minutes from ``pip install`` to the first audit row + admin History
  view. (PR #20)
- [x] **``SECURITY.md``** — disclosure + response SLAs.
- [x] **``CONTRIBUTING.md``** — accepted contributions, test suite, bug
  reports.

**Done when:** ✅ API ref + all guides publish to GitHub Pages
(``tauvin.github.io/auditrum`` via ``docs.yml``); the deployment guide
covers running auditrum in production end to end; both the catalog and
bidwise case studies are published with real numbers; a new user goes
from zero to first audit row in ~5 minutes via the Quickstart.

**Effort:** delivered. All docs landed against the real numbers from the
0.5 cycle, so the guides cite measured figures rather than estimates.

---

### 0.7 — Release candidate

**Theme:** API freeze, last security review, public RC period. No new
features. Only fixes for blockers.

- [ ] **Public API freeze.** Whatever is in 0.7 is what 1.0 will have.
  Breaking changes only if they fix a discovered blocker.
- [ ] **Public release candidates** on PyPI (``0.7.0rc1``,
  ``0.7.0rc2``, ...). Aim for 2-4 weeks of community testing before
  cutting 1.0.
- [ ] **Final security review** — repeat the three-pass review pattern
  from 0.3.1 (senior dev, security auditor, QA). Any new HIGH or
  CRITICAL findings block 1.0 and reset the RC clock.
- [ ] **External pre-1.0 user** beyond the original company. Catalog
  + sister projects are great real-world feedback but they share an
  organisational context. We want at least one independent user
  (someone outside the original company) running an RC before 1.0
  to validate the docs.
- [ ] **Full CHANGELOG for 1.0** with the cumulative diff against
  0.3.1.
- [ ] **Migration guide for 0.x → 1.0** users. Mostly boilerplate
  since the API surface should already match between 0.4 and 0.7.

**Done when:** 2+ weeks of RC period have passed without blocker bugs;
external user has confirmed RC works in their environment; security
review is clean; catalog + sister projects have all upgraded to the
RC and reported back.

**Effort:** ~1-2 weeks of work plus 2-4 weeks of waiting.

---

### 1.0.0 — Stable Django release

**Scope:** the same shape as 0.7, with the API stability promise made
public. Django-focused. Single-table audit log. Time travel, hash
chain, hardening, retention, blame CLI, observability hooks. SQLAlchemy
support exists but is marked experimental in ``__all__``.

**The promise:**
- Public API stability for the full 1.x series. Everything in
  ``__all__`` of every module changes only in backwards-compatible
  ways until 2.0.
- Schema migrations have a documented forward path. Auditrum 0.3 →
  1.0 has migration scripts; 1.x → 1.x is automatic.
- LTS minor releases for at least 12 months — bug fixes backported to
  the latest 1.x line even after a new minor ships.
- Public commitment to a support window for PostgreSQL / Python /
  Django versions. We document which versions are supported, tested,
  and what the EOL plan is.
- Security policy in ``SECURITY.md`` with a disclosure email and
  response SLAs.

**What we explicitly don't promise:**
- Perfect performance characteristics across every workload. We
  document benchmark numbers and the conditions under which they
  hold; users measure on their own infrastructure.
- Support for every Python ORM. Django is first-class; SQLAlchemy is
  experimental; community contributions for the rest.
- Compliance certifications. We provide building blocks; certification
  is between users and their auditors.

---

## Post-1.0 features (1.x line)

These were explicitly **moved out of the pre-1.0 cycle** to keep 1.0
focused. They ship as 1.x minor releases after the stable Django
foundation is locked. Each adds API surface, but in a backwards-
compatible way (new modules, new optional extras, new flags) without
touching the locked 1.0 surface.

### 1.1 — SQLAlchemy first-class

- Promote ``auditrum.integrations.sqlalchemy`` from experimental to
  fully supported.
- **Alembic autogenerate integration.** The pain point is
  ``auditrum_makemigrations`` is Django-only; SA users have to write
  migrations by hand or rely on ``sync(engine)`` at startup. Build
  ``alembic_utils``-style operations that Alembic's autogenerate can
  produce automatically.
- Documentation page parallel to ``docs/django.md`` showing the SA
  flow end-to-end.
- SA integration tests added to the multi-version matrix.

### 1.2 — Async + FastAPI

- ``AsyncPsycopgExecutor`` — async cursor protocol parallel to
  ``PsycopgExecutor``.
- Proper ``AsyncSession`` integration for SQLAlchemy.
- ``auditrum.integrations.fastapi`` extras with middleware and
  dependency-injection helpers.
- ``auditrum.integrations.starlette`` thin wrapper.
- Working FastAPI example in ``examples/fastapi/``.

### 1.3 — Compliance and privacy features

- **GDPR pseudonymization** via ``auditrum.privacy``: rewrite historic
  audit rows with a deterministic salted hash for a given subject,
  recompute the hash chain from the break point forward, append-only
  meta-audit table for the operation. ``verify_chain`` learns to
  bridge pseudonymization break points cleanly.
- **Per-model table opt-in mode** for users who specifically want
  typed Django querysets. Single-table stays the recommended default.
- **Compliance reporting templates**: SOC2-friendly export, GDPR
  subject access export, ISO27001 evidence pack.

### 1.4 and beyond — Nice-to-haves

- Web UI for browsing audit events (separate sibling repo
  ``auditrum-web``)
- Multi-database federation
- Audit-data-in-data-lake (Parquet export)
- Per-row column-level encryption helpers

---

## What we are explicitly NOT doing before 1.0

These are not deferred to 1.x — they are out of scope for this
project. They may come back as separate sibling projects or
community contributions, but they won't be in the auditrum tree.

- **Compliance certifications** (SOC2 / ISO27001 / PCI templates as
  business work). We provide building blocks; certification is
  between users and their auditors.
- **Logo and branding work.** After 1.0.
- **Conference talks / blog posts.** After 1.0 and after we have
  2-3 external users.

---

## Realistic timing

At a part-time pace (evenings + weekends, 1-2 phases per month):
**4-6 months** from current 0.3.1 to 1.0. Faster than the previous
estimate because we pulled SQLAlchemy / async / FastAPI out of the
pre-1.0 cycle entirely.

The phases most likely to slip:

- **0.3.x reactive cycle.** Depends on what catalog hits in
  production. Could be quick (no surprises) or could surface
  architectural issues that bump 0.4.
- **0.5 (performance benchmarks).** Depends on what the catalog
  numbers actually show. If trigger overhead is high, we have to
  optimise, which is open-ended work.
- **0.7 (RC + external user).** Depends on external feedback that
  we don't control.

---

## Tracking

- Issues in the GitHub issue tracker carry a ``milestone:`` label
  matching the version above (``milestone:0.4``, ``milestone:0.5``,
  etc.). Anything not labelled is unscheduled.
- This file is updated as items ship. Checked-off items stay in the
  document as a record of what was actually done in each version.
- If a milestone slips, the slip + reason gets noted here, not buried
  in commit messages.

---

## Open questions

These are decisions that affect roadmap shape but haven't been made:

1. ~~**Where does ``_validate_ident`` legacy alias go in 1.0?**~~
   **Resolved 2026-04-16:** removed outright in 0.4 (breaking;
   pre-1.0 is fine). Canonical name is
   ``auditrum.tracking.spec.validate_identifier``.
2. ~~**Second type checker (``pyright``) as an opinion in CI?**~~
   **Resolved 2026-04-23:** no. There is exactly one type gate
   (``ty`` then, ``pyrefly`` from 0.5.x). Running two catches
   marginally more bugs while doubling the failure modes and the
   maintenance cost of per-checker ``ignore`` pragma drift.
3. ~~**Documentation hosting.**~~ **Resolved 2026-04-23:** GitHub
   Pages. Same repo, no external service to manage, deploy via
   ``mkdocs gh-deploy`` in a release workflow. Revisit only if the
   docs outgrow what GH Pages can comfortably serve.
4. ~~**LTS line cadence.**~~ **Resolved 2026-06-04:** 12 months. Bug
   fixes get backported to the latest ``1.x`` line for 12 months after
   a minor ships. Concrete — unlike "as needed", which would hollow out
   the LTS promise — and sustainable at a part-time solo pace (18 months
   is too heavy to hold). Matches the "at least 12 months" wording
   already in the 1.0.0 section.
5. **GDPR pseudonymization timing — pre-1.0 or 1.3?** Currently in
   1.3. If catalog has a regulatory deadline that needs it sooner,
   it could move into 0.5 or 0.6. **Need catalog's compliance team
   to confirm whether they need it before 1.0.**
6. ~~**SQLAlchemy "experimental" status in 1.0.**~~ **Resolved
   2026-06-04:** ship it as ``experimental`` in ``__all__`` (API may
   change), with 1.1 promoting it to first-class + Alembic-autogenerate
   parity. Removing it from 1.0 and re-adding in 1.1 would be more churn
   and read as abandonment; the experimental label keeps the code in the
   tree while leaving 1.1 free to clean it up. Matches the 1.0 scope
   (Django first-class, SQLAlchemy experimental).
