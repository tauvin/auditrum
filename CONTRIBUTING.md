# Contributing to auditrum

Thank you for taking the time to look. auditrum is a small project —
one maintainer plus a couple of sister-project integrators — and
every issue, PR, and bug report genuinely helps.

## What we happily accept

* **Bug reports** with a minimal reproducer. Integration-test-shaped
  reports (trigger installed, mutation run, queryset returned the
  wrong thing) are gold — if you can submit a failing test alongside
  your report, that's the fastest path to a fix.
* **Typo and docs fixes**, any size, no issue required — just open a PR.
* **Regression fixes** for things that used to work. These are
  always welcome.
* **New tests** that cover existing behaviour. Always welcome.
* **Compatibility fixes** for newer PostgreSQL / Python / Django
  versions. File a bug first so we can agree on scope; the CI
  matrix in ``.github/workflows/ci.yml`` is the reference.
* **Performance improvements** with before/after numbers from
  ``benchmarks/``. Don't try to squeeze out percentages without
  evidence — the whole point of that suite is to avoid speculation.
* **Framework integrations** (FastAPI, Starlette, …) — discuss in
  an issue first. See ``ROADMAP.md`` "Post-1.0 features" for the
  planned-vs-opportunistic split; anything marked post-1.0 there is
  welcome as a standalone experimental integration, with the
  understanding that it lives under ``integrations/`` as
  ``public-experimental``.

## What needs discussion before you code

* **New public API surface** — a new module, a new top-level
  ``__all__`` entry, a new option on a stable function. Pre-``1.0``
  we're locking the shape of the stable surface; additions need
  alignment with ``docs/api-stability.md``.
* **Changes that break backwards compatibility** on anything marked
  ``public-stable`` in ``docs/api-stability.md``. Pre-``1.0`` this
  is possible but always deliberate; open an issue.
* **Schema changes** to ``auditlog`` / ``audit_context`` / trigger
  SQL templates. Every deployment pays the migration cost, so
  schema changes need a concrete reason and a documented back-fill
  path.

## What we probably don't want

* **Style refactors without a goal** — switching to a different
  formatter, renaming variables for taste, reshuffling imports.
  These create review churn without changing behaviour.
* **New dependencies** without a clearly-justified need. Each
  dependency is a supply-chain surface we have to track.
* **Feature-flag additions**. Pre-1.0 we prefer breaking changes
  over feature flags — the library is small enough that flag creep
  hurts more than it helps.
* **Compliance certification work** (SOC2 / ISO27001 / etc.).
  See ``ROADMAP.md`` — we provide building blocks; certification
  is between users and their auditors.

## Running the test suite

```bash
# One-time setup
uv sync --extra django --extra sqlalchemy --extra observability --extra test

# Unit tests (fast — no Postgres needed; uses sqlite + mocks)
uv run pytest tests/ -q --ignore=tests/integration

# Integration tests (requires Docker — uses testcontainers[postgres])
uv run pytest tests/integration/ -q

# Type gate
uv run ty check

# Lint
uvx ruff@0.15 check .
```

The CI matrix in ``.github/workflows/ci.yml`` is the reference for
what has to be green before a PR is mergeable. Locally you only
need the cell that matches your machine.

## Running benchmarks

```bash
uv sync --extra benchmark
uv run pytest benchmarks/ --benchmark-only
```

See ``benchmarks/README.md`` for saved-baseline comparison and the
context around what the numbers do and don't tell you.

## Filing a good bug report

The best reports answer four questions:

1. **Environment.** ``pip show auditrum``, Python version,
   PostgreSQL version, Django version if relevant.
2. **Steps to reproduce.** The exact sequence of API calls / SQL /
   manage.py commands. ``@track(...)`` decorator, migration flow,
   data inserted, query run.
3. **What you expected.** One sentence.
4. **What actually happened.** Full traceback, relevant SQL, or a
   minimal assertion that fails.

If you're reporting a performance regression, include numbers from
``benchmarks/`` (before and after). If you're reporting a
security-relevant issue, email the address in
``SECURITY.md`` instead of opening a public issue.

## Commit and PR conventions

* **Commit messages** are free-form. Focus on the *why*, not the
  *what* — the diff is already the what.
* **One logical change per PR**, but "logical change" can include
  a test + fix + CHANGELOG entry for the same bug. Don't split a
  one-bug PR into three PRs just for neatness.
* **Always update the CHANGELOG** under ``[Unreleased]`` for
  anything user-visible. ``Added`` / ``Changed`` / ``Removed`` /
  ``Fixed`` / ``Security`` sections following Keep a Changelog.
* **Breaking changes** go under ``### Changed`` with the word
  ``Breaking:`` leading the entry, and with an upgrade path
  documented in the same entry.

## Code style quick reference

* Python ≥ 3.11, typed strictly — ``ty`` checks ``auditrum/`` core
  and ``auditrum/integrations/django/``. Any new code should be
  type-clean.
* Prefer module-level imports; function-scoped imports only as a
  workaround for circular imports or heavy optional deps.
* No ``# noqa: …`` / ``# type: ignore[…]`` / ``# ty: ignore[…]``
  without a comment explaining *why* (future contributors need to
  know whether to keep it).
* No new prints / debug log statements left in a PR.
* Prefer single-line comments that explain *why*. If you're tempted
  to document *what* the code does, reach for clearer identifiers
  first.

## Code of conduct

Be decent. Disagreements about design are fine; personal attacks are
not. The maintainer reserves the right to close and lock threads that
go sideways.

## License

By contributing, you agree that your contributions will be licensed
under the project's MIT license. See ``LICENSE``.
