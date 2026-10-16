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

### `verify-diff-chain`

Find writes that bypassed the audit trigger: per object and column, each
event's `old` must match the previous event's `new`, the last event must
match the live row, and INSERT / DELETE must match the row's existence.
Full guide in [`diff-chain.md`](diff-chain.md).

```bash
# Objects with events in the last day (default window)
auditrum verify-diff-chain

# Whole tables, one table
auditrum verify-diff-chain --full --table orders

# One object, JSON output
auditrum verify-diff-chain --table orders --object-id 42 --json
```

| Option | Meaning |
|---|---|
| `--table T` | Table to check, repeatable. Default: every registered table. |
| `--since` | Window start: `'1 day'`, `'2 weeks'`, … or ISO-8601. Default `'1 day'`. |
| `--full` | Check whole tables. Ignores `--since`. |
| `--object-id ID` | Check one object. Without `--table` it applies to every registered table. |
| `--json` | Print findings as a JSON array. |
| `--limit N` | Report at most N findings in total. |
| `--audit-table`, `--db-*` | As for the other commands. |

Prints one finding per object (the first by event id). Exit 0 when
clean, 1 with findings, 2 on error. With no registered tables it exits
2 because nothing was checked. A table that cannot be checked is
reported and skipped; the others are still checked, and the run exits 2.

### `compact-snapshots`

Clear `old_data` / `new_data` of rows written before a table was switched
to `storage="diff"`, keeping the diffs, online and in batches of objects.
Each batch is checked first; objects with [diff chain](diff-chain.md)
findings or with snapshot changes their diff does not record, rows
covered by the hash chain and pre-0.4 rows are kept. Guide and limits:
[Clearing old snapshots](migration-cookbook.md#clearing-old-snapshots).

```bash
auditrum compact-snapshots --table orders --dry-run
auditrum compact-snapshots --table orders --table invoices --older-than '6 months'
```

| Option | Meaning |
|---|---|
| `--table T` | Diff-mode table, required, repeatable. There is no "all tables". |
| `--older-than` | Only rows older than this: `'6 months'`, … or ISO-8601. |
| `--batch-size N` | Objects per committed batch. Default 1000. |
| `--dry-run` | Count rows without changing them. |
| `--audit-table`, `--db-*` | As for the other commands. |

Prints, per table, the rows cleared (`would clear` on `--dry-run`), the
rows kept because of the hash chain, and the objects kept because of
findings (the first 10). After a run that cleared rows it reminds
you to run `VACUUM (ANALYZE)` on the audit table. Exit 0 on success
(findings are reported, not failures), 2 when a table failed; the other
tables are still processed. Needs `UPDATE` on the audit table.

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

Generate Django migration files for `@track`-decorated models whose
trigger changed since the last migration (new, edited, removed, or
re-rendered by an auditrum upgrade). Writes nothing when everything is
up to date. Full guide in [`django.md`](django.md#migrations).

```bash
python manage.py auditrum_makemigrations
python manage.py auditrum_makemigrations --dry-run
python manage.py auditrum_makemigrations --check   # CI: exit 1 if migrations are missing
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
