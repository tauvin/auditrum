# auditrum documentation

Welcome. **auditrum** is a trigger-based audit system for PostgreSQL with
context propagation, time travel, hash-chained tamper detection, and
first-class integrations for Django and SQLAlchemy.

This directory is the full documentation. Every page is markdown so you
can read it directly on GitHub without building anything.

## Start here

- **[Getting started](getting-started.md)** — install, pick your stack,
  see an audit event appear in under five minutes.
- **[Core concepts](concepts.md)** — what `TrackSpec`, `TriggerManager`,
  and the audit context actually are, and why we chose a single-table
  design backed by partitioning.

## Framework guides

- **[Django integration](django.md)** — `@track` decorator,
  `auditrum_makemigrations`, middleware, ORM helpers, admin integration.
- **[SQLAlchemy integration](sqlalchemy.md)** — `track_table`,
  `bootstrap_schema`, `sync`, Alembic integration.
- **[Raw psycopg](getting-started.md#option-3-raw-psycopg--any-other-framework)**
  — plain psycopg connections with `TriggerManager` directly.

## Features

- **[Time travel](time-travel.md)** — reconstruct any row or whole table
  at any past timestamp without the `temporal_tables` extension.
- **[`auditrum blame`](blame.md)** — git-style per-row and per-field
  history from the command line.
- **[Hardening and compliance](hardening.md)** — tamper-evident hash
  chain, append-only roles, retention purging, GDPR considerations.
- **[Observability](observability.md)** — OpenTelemetry trace correlation,
  Prometheus metrics, Sentry breadcrumbs.

## Reference

- **[CLI reference](cli.md)** — every command with arguments and examples.
- **[Architecture](architecture.md)** — schema layout, trigger flow,
  context propagation pipeline, drift detection, concurrency model.

## Getting help

- Issues and feature requests: [github.com/tauvin/auditrum/issues](https://github.com/tauvin/auditrum/issues)
- Changelog: [CHANGELOG.md](../CHANGELOG.md)

## Conventions in this doc

Code blocks show the language (`python`, `sql`, `bash`, `toml`) so
GitHub highlights them. Paths in backticks point to files in the
repository; click them on GitHub to jump to source.

When a section is framework-specific, the heading usually says so.
When it's not, the example assumes you have a psycopg connection named
`conn` that's already open against the database.
