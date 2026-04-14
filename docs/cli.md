# CLI reference

The `auditrum` command line. Every subcommand ends up in this doc with
its arguments, typical usage, and the Python API it's a thin wrapper
around.

Entry point:

```bash
auditrum --help
```

Global options (apply to every subcommand):

| Option            | Default | Description                                            |
|-------------------|---------|--------------------------------------------------------|
| `-v, --verbose N` | `1`     | `0` quiet, `1` default, `2` debug (log level passthru) |
| `--help`          |         | Show help and exit                                     |

Every DB-touching subcommand accepts the same connection options:

| Option             | Fallback env var  | Description                              |
|--------------------|-------------------|------------------------------------------|
| `--db-dsn`         | —                 | Full libpq DSN, takes precedence         |
| `--db-host`        | `PGHOST`          |                                          |
| `--db-port`        | `PGPORT`          |                                          |
| `--db-user`        | `PGUSER`          |                                          |
| `--db-password`    | `PGPASSWORD`      |                                          |
| `--db-name`        | `PGDATABASE`      |                                          |
| `--audit-table`    | project setting   | Override destination audit table name    |

Settings are loaded from `.env` via `python-dotenv` if present in the
working directory.

---

## Schema management

### `init-schema`

Emit the `CREATE TABLE` + indexes + default partition for the audit
log.

```bash
auditrum init-schema --dry-run                     # print to stdout
auditrum init-schema --output schema.sql           # write to file
auditrum init-schema                               # apply to DB via DSN
```

Python equivalent: `auditrum.schema.generate_auditlog_table_sql(...)`.

### `create-partitions`

Create monthly partitions going forward N months. Idempotent.

```bash
auditrum create-partitions --months 6
auditrum create-partitions --months 3 --dry-run
```

Run this from cron so future writes always land in a real partition
even if cron hiccups. The default partition (`auditlog_default`)
catches anything outside the rolling window, so this is a performance
hint, not a crash prevention.

Python equivalent:
`auditrum.schema.generate_auditlog_partitions_sql(months_ahead=6)`.

### `generate-trigger`

Emit `CREATE OR REPLACE FUNCTION` + `CREATE TRIGGER` for a single
tracked table. Useful for non-Django / non-SA setups where you
maintain trigger SQL manually.

```bash
auditrum generate-trigger orders --dry-run
auditrum generate-trigger orders --output orders_trigger.sql
auditrum generate-trigger orders                  # apply to DB
```

Python equivalent:
```python
from auditrum.triggers import generate_trigger_sql
sql = generate_trigger_sql("orders", track_only=["status", "total"])
```

---

## Status and diagnostics

### `status`

Report what's currently installed: audit triggers, partitions, and
row count.

```bash
auditrum status
# INFO  Audit triggers found      count=3
#  - orders: audit_orders_trigger
#  - users: audit_users_trigger
#  - invoices: audit_invoices_trigger
# INFO  Auditlog partitions       count=6
#  - auditlog_p2026_04
#  - ...
# INFO  Auditlog row count        rows=1284817
```

Useful for a post-deploy smoke test or to diagnose "why isn't my
audit log growing".

### `blame`

Git-style history for one row or one column. Full guide in
[`blame.md`](blame.md).

```bash
auditrum blame <table> <object_id> [field]
auditrum blame users 42
auditrum blame users 42 email
auditrum blame orders 42 --format json --limit 50
```

Options:

| Option           | Default | Description                               |
|------------------|---------|-------------------------------------------|
| `-n, --limit N`  | `200`   | Maximum events to fetch                   |
| `-f, --format X` | `rich`  | `rich` (colored), `text` (plain), `json`  |

### `as-of`

Reconstruct the state of a row, or every surviving row in a table, at
a past timestamp. Full guide in [`time-travel.md`](time-travel.md).

```bash
auditrum as-of orders "2024-06-01T14:23:00+00:00" --id 42
auditrum as-of orders "2024-06-01T00:00:00+00:00" --format jsonl
auditrum as-of orders "2024-06-01T00:00:00+00:00" --format jsonl --limit 100
```

Options:

| Option           | Default | Description                                  |
|------------------|---------|----------------------------------------------|
| `--id X`         | none    | Single row by object id                      |
| `-f, --format X` | `json`  | `json` (pretty single row), `jsonl` (stream) |
| `--limit N`      | none    | Cap the stream; ignored when `--id` is set   |

---

## Mutation and recovery

### `revert`

Generate the SQL `UPDATE` that would roll back a specific audit event.
This is an **inspection** tool — the command prints or executes the
SQL, it does not automatically create a compensating audit row, so
treat it as a "break glass" operation.

```bash
# Generate the revert SQL without running it
auditrum revert orders 42 <log_id> --dry-run

# Actually apply the revert
auditrum revert orders 42 <log_id>
```

The command refuses to run without a valid `--db-dsn` because it needs
to query the historical row to rebuild the `SET` clause.

Python equivalent: `auditrum.revert.generate_revert_sql_from_log(...)`.

---

## Hardening

### `harden`

Revoke mutating privileges on the audit log. Full guide in
[`hardening.md`](hardening.md).

```bash
auditrum harden                                    # REVOKE from PUBLIC
auditrum harden --app-role myapp                   # + from myapp
auditrum harden --admin-role myapp_admin           # + GRANT to myapp_admin
auditrum harden --dry-run                          # print SQL only
```

### `enable-hash-chain`

Enable SHA-256 tamper-evident hash chain on the audit log.

```bash
auditrum enable-hash-chain
auditrum enable-hash-chain --dry-run
```

Requires `pgcrypto`. One-time operation; safe to re-run
(`ALTER TABLE IF NOT EXISTS` + `CREATE OR REPLACE FUNCTION`).

### `verify-chain`

Verify the hash chain end-to-end via a server-side `LAG` window
query. Exits 0 on success, prints broken row ids on failure.

```bash
auditrum verify-chain
# chain OK: 48271 rows verified
```

Good candidate for a nightly cron that alerts on failure.

---

## Retention

### `purge`

Delete audit events older than a given interval. Full guide in
[`hardening.md`](hardening.md#retention-and-purging).

```bash
# DELETE-based
auditrum purge --older-than "2 years"
auditrum purge --older-than "30 days" --dry-run

# Partition drops (WAL-friendly for large tables)
auditrum purge --older-than "2 years" --drop-partitions
```

Supported interval grammar: `N days`, `N weeks`, `N months`, `N years`.

Python equivalents:
`auditrum.retention.generate_purge_sql(...)`,
`auditrum.retention.drop_old_partitions(...)`.

---

## Django-specific management commands

These are Django management commands registered by
`auditrum.integrations.django`, not subcommands of the `auditrum`
binary. Run them as `python manage.py <command>`.

### `auditrum_makemigrations`

Generate Django migration files for every `@track`-decorated model.
Full guide in [`django.md`](django.md#migrations).

```bash
python manage.py auditrum_makemigrations
python manage.py auditrum_makemigrations --dry-run
python manage.py auditrum_makemigrations --name release_v3
```

### `audit_add_partitions`

Create additional monthly partitions via Django's default connection.

```bash
python manage.py audit_add_partitions --months 6
```

Equivalent to the standalone `auditrum create-partitions` but uses
the Django connection so it respects `DATABASES['default']` and any
custom routing.

---

## Environment variables

Used by the standalone CLI (not the Django management commands):

| Variable         | Purpose                                        |
|------------------|------------------------------------------------|
| `PGHOST`         | DB host fallback when `--db-host` is omitted   |
| `PGPORT`         | DB port fallback                               |
| `PGUSER`         | DB user fallback                               |
| `PGPASSWORD`     | DB password fallback                           |
| `PGDATABASE`     | DB name fallback                               |
| `AUDIT_TABLE`    | Override default `auditlog` table name         |

All are loaded from a `.env` in the working directory via
`python-dotenv` if present.

## What's next

- [Getting started](getting-started.md) — first-time setup
- [Django](django.md) / [SQLAlchemy](sqlalchemy.md) — framework guides
- [Hardening](hardening.md) — pick which CLI commands belong in cron
