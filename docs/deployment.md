# Production deployment guide

This is the end-to-end runbook for running auditrum in production: the
deploy sequence, the cron jobs that keep it healthy, monitoring, backup,
and rollback. It **ties the pieces together** — the deep detail for each
piece lives in its own page, linked inline. Read
[Hardening](hardening.md) and [Observability](observability.md)
alongside this.

## Before you deploy

* **Supported versions:** PostgreSQL 13–17, Python 3.11–3.14, Django
  4.2 LTS / 5.2 LTS / 6.0. (See the CI matrix in
  [Performance](performance.md#ci-matrix).)
* **Two decisions up front:**
  * **Two DB roles** — an admin role for migrations/retention/CLI and a
    least-privilege runtime role for the app. This is the single most
    important hardening step; see [the role split](hardening.md#the-role-split).
  * **Hash chain on or off** — tamper-evidence vs. a small write cost.
    Off by default; turn it on for regulated/high-assurance audit trails.
    See [Hardening](hardening.md) and [Tamper evidence](hardening.md).

## 1. Initial install and schema

1. Install: `pip install "auditrum[django]"`; add the app and middleware
   and set the `PGAUDIT_*` settings — see
   [Django integration](django.md) and [Getting started](getting-started.md).
2. **Run migrations as the admin role.** They create the partitioned
   `auditlog` + `audit_context` tables, the PL/pgSQL helper functions,
   and the audit triggers on your tracked tables.
3. **Create partitions ahead of time.** `auditlog` is RANGE-partitioned
   by month; if no partition exists for the current month, rows fall to
   the `_default` partition (it works, but you lose the per-month
   pruning that makes retention cheap). Provision a few months ahead:

   ```bash
   python manage.py audit_add_partitions --months 3
   ```
   Schedule this as a recurring job (see below) so you never run out.
4. **(Optional) enable the hash chain:**

   ```bash
   python manage.py auditrum_enable_hash_chain
   ```
   It adds `chain_seq` / `prev_hash` / `row_hash` and a BEFORE-INSERT
   trigger. Idempotent. Note it serialises audit inserts behind a
   per-table advisory lock — opt in deliberately.

## 2. Roles and least privilege

Recap (full version in [the role split](hardening.md#the-role-split)):

* **`*_admin`** — owns the tables and trigger functions; runs
  migrations, partition management, retention, and the `auditrum` CLI.
* **`*_runtime`** — the app's role. It has **no direct write** to
  `auditlog`; the audit row is written by a `SECURITY DEFINER` trigger,
  so even a fully compromised app cannot forge or delete audit history.

## 3. Operational cron jobs

Run all of these as the **admin** role (cron, Celery beat, or a
Kubernetes `CronJob` — whatever you already operate):

| Job | Cadence | Command |
|-----|---------|---------|
| Provision future partitions | monthly | `python manage.py audit_add_partitions --months 3` |
| Prune old audit data | daily/weekly | `auditrum purge --older-than 18mo` (partition-aware drop — far cheaper than a row `DELETE`; see [Hardening](hardening.md) / [Performance tuning](performance-tuning.md)) |
| Verify the hash chain *(if enabled)* | daily | `python manage.py auditrum_verify_chain --expected-tip-json <anchor>` |

**Anchoring the chain tip.** If the chain is on, the verify job is only
meaningful against an externally-stored tip — otherwise a privileged
attacker could rewrite the whole chain. Capture the current tip on each
run and store it somewhere the DB admin can't silently edit (S3 Object
Lock, a separate WORM store, even a printout), then feed it back via
`--expected-tip-json` next time. See the hash-chain section of
[Django integration](django.md).

## 4. Monitoring

Wire up [Observability](observability.md) before you go live:

* **Prometheus** — `auditrum.observability.prometheus.AuditrumCollector`
  emits per-`(table, operation)` rates and per-table trigger-duration
  histograms.
* **Grafana** — import the dashboard from
  [`examples/grafana/`](https://github.com/tauvin/auditrum/tree/main/examples/grafana):
  events/sec, trigger latency p50/p95/p99, hash-chain verify status,
  partition disk usage, retention lag.

Alert on: hash-chain verify failing, partition disk growth outpacing
retention, and trigger-latency regressions. For real numbers to set
thresholds against, see [Performance](performance.md).

## 5. Backup and restore

* **The audit log is part of your compliance record — include it in
  backup scope.** It is large and partitioned; back it up the same way
  you back up any large partitioned table (physical base backup + WAL,
  or per-partition `pg_dump`).
* **After a restore or a version mismatch, refresh the schema helpers:**

  ```bash
  python manage.py auditrum_refresh_schema
  ```
  This re-emits the version-dependent PL/pgSQL helper bodies
  (`jsonb_diff`, the reconstruct functions, …) so the database side
  matches the installed library. `--dry-run` shows what would change.
* **Hash chain across a restore.** A restore that loses tail rows is
  *detectable* — that's the point of anchoring the tip externally. After
  any restore, re-run `auditrum_verify_chain` against your last anchor,
  then capture and store a fresh tip.

## 6. Upgrading auditrum

1. Bump the dependency and read the [Changelog](changelog.md) for
   breaking changes (e.g. 0.5 made the GIN-on-diff index opt-in —
   migration `0004` drops it unless you set `PGAUDIT_DIFF_GIN_INDEX=True`).
2. Run the new migrations as the admin role.
3. Run `auditrum_refresh_schema` to bring the PL/pgSQL helpers in sync
   (the migration graph also does this; the command is the ops escape
   hatch for deployments installed outside the migration graph).

## 7. Rollback playbook

* **App rollback** — redeploy the previous image. The audit trigger
  lives in the database and keeps working; no audit-side action needed.
* **Migration rollback** — `python manage.py migrate auditrum_django <previous>`.
  Check what each migration reverses before rolling back across a schema
  change.
* **Corrupted helper functions after a bad deploy** — `auditrum_refresh_schema`
  restores them to the installed version.
* **Emergency stop** — to halt auditing on a table, uninstall its
  trigger (the `UninstallTrigger` migration operation, or
  `DROP TRIGGER`). Auditing stops; existing history is untouched. Re-add
  the trigger to resume. See [Django integration](django.md).

## Command quick reference

| Command | Purpose |
|---------|---------|
| `python manage.py audit_add_partitions --months N` | Provision future monthly partitions |
| `auditrum purge --older-than <interval>` | Partition-aware retention |
| `python manage.py auditrum_enable_hash_chain` | Turn on tamper-evidence |
| `python manage.py auditrum_verify_chain [--expected-tip-json …]` | Check chain integrity (non-zero exit if broken) |
| `python manage.py auditrum_refresh_schema [--dry-run]` | Re-sync PL/pgSQL helpers after upgrade/restore |

For the full CLI, see the [CLI reference](cli.md); for the
programmatic API, the [API reference](reference/api.md).
