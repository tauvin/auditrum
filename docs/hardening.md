# Hardening and compliance

For compliance-sensitive deployments, the audit log has to be
**append-only** and **tamper-evident**. auditrum ships three building
blocks that compose:

1. **`auditrum harden`** — `REVOKE` all direct writes (INSERT/UPDATE/
   DELETE/TRUNCATE) on `auditlog` and `audit_context` from the
   application role, so even a fully compromised app can't forge
   or modify audit rows by writing to these tables directly.
2. **`auditrum enable-hash-chain`** — optional SHA-256 chain over
   every audit row, recomputable and verifiable server-side.
3. **`auditrum purge` / retention** — partition-aware deletion of old
   events for GDPR and storage management.

Each piece is independently useful; compliance-heavy projects tend to
enable all three.

## The role split

auditrum's hardening story relies on **two distinct database roles**:

- **`myapp_admin`** — runs migrations, owns the audit trigger functions,
  has full privileges on `auditlog` / `audit_context`. Used only by
  migration deploys, retention cron jobs, and the `auditrum` CLI.
  **Credentials must live outside the runtime app environment** —
  CI/CD secrets, a PAM vault, a separate deployment service.
- **`myapp`** — the runtime application role. Reads and writes the
  tracked business tables, reads `auditlog` for in-app history views,
  but has **no direct write access** to `auditlog` or `audit_context`.
  Audit rows only flow through the `SECURITY DEFINER` trigger functions.

The split is what makes "application can only produce truthful audit
rows" a provable property, not a marketing claim. A fully compromised
`myapp` role cannot forge rows because direct INSERT is revoked; it
cannot modify history because UPDATE is revoked; it cannot destroy
history because DELETE/TRUNCATE is revoked. The only write path it
has is through a trigger fired by a `INSERT`/`UPDATE`/`DELETE` on a
tracked business table — which `SECURITY DEFINER` causes to run under
`myapp_admin` privileges, producing a legitimate audit row with the
actual operation details.

## How `SECURITY DEFINER` powers this

The audit trigger functions installed by `auditrum.tracking` are
declared `SECURITY DEFINER` with `SET search_path = pg_catalog, public`.
What this means in Postgres:

- The function runs with the privileges of its **owner**, not the
  calling role.
- Because `myapp_admin` creates the functions during migration, they
  run as `myapp_admin` for the duration of each invocation.
- Calling `INSERT INTO widgets …` as `myapp` fires the trigger, which
  enters the `myapp_admin` privilege context, which can `INSERT INTO
  auditlog` — even though `myapp` itself cannot.
- `SET search_path = pg_catalog, public` prevents search-path attacks
  (a rogue schema earlier on the path shadowing built-in functions).

This is the standard Postgres pattern for "privileged helper triggered
by unprivileged code". It's the same mechanism Postgres's own
`pgcrypto`, `pgaudit`, and many RLS-based multi-tenant schemes use.

## Running `auditrum harden`

```bash
# Minimum: revoke direct writes on both tables from the named app role
auditrum harden --app-role myapp

# With a dedicated admin role that retains maintenance privileges
auditrum harden --app-role myapp --admin-role myapp_admin

# Preview the SQL without running it
auditrum harden --app-role myapp --admin-role myapp_admin --dry-run

# Custom context table name
auditrum harden --app-role myapp --context-table my_audit_context
```

Run `auditrum harden` as `myapp_admin` (or as a superuser) — the
command revokes privileges, which requires being the table owner or a
superuser.

Generated SQL (example with `--app-role myapp --admin-role myapp_admin`):

```sql
REVOKE INSERT, UPDATE, DELETE, TRUNCATE ON auditlog FROM PUBLIC;
REVOKE INSERT, UPDATE, DELETE, TRUNCATE ON audit_context FROM PUBLIC;
REVOKE INSERT, UPDATE, DELETE, TRUNCATE ON auditlog FROM myapp;
REVOKE INSERT, UPDATE, DELETE, TRUNCATE ON audit_context FROM myapp;
GRANT SELECT, INSERT, UPDATE, DELETE, TRUNCATE, REFERENCES, TRIGGER
    ON auditlog TO myapp_admin;
GRANT SELECT, INSERT, UPDATE, DELETE, TRUNCATE, REFERENCES, TRIGGER
    ON audit_context TO myapp_admin;
```

After running this:

- `myapp` trying `INSERT INTO auditlog (...)` → `InsufficientPrivilege`
  (a compromised app can't forge rows)
- `myapp` trying `UPDATE auditlog SET operation = 'HACKED'` →
  `InsufficientPrivilege`
- `myapp` trying `INSERT INTO widgets (...)` → works; trigger fires;
  audit row appears in `auditlog` via the `SECURITY DEFINER` path
- `myapp` trying `SELECT * FROM auditlog WHERE user_id = 42` → works
  (SELECT is not revoked; in-app history views still function)
- `myapp_admin` trying any maintenance operation → works

### Verifying the setup

The integration test `tests/integration/test_hardening_pg.py` creates a
limited role, applies `generate_revoke_sql`, and verifies:

- `INSERT INTO auditlog` directly fails with `InsufficientPrivilege`
- `INSERT INTO audit_context` directly fails with `InsufficientPrivilege`
- `UPDATE auditlog` fails
- `DELETE FROM auditlog` fails
- `INSERT INTO widgets` still produces an audit row via the trigger
- `SELECT FROM auditlog` still works
- A dedicated admin role regains full write access after `generate_grant_admin_sql`

Copy that pattern into your own deployment smoke tests.

### Who owns the trigger functions?

Whoever created them — i.e. whoever ran the migration. In a
two-role setup, that should be `myapp_admin`:

```bash
# In your deploy script, run migrations as the admin role
DATABASE_URL=postgresql://myapp_admin:${MYAPP_ADMIN_PASSWORD}@db/myapp \
    python manage.py migrate

DATABASE_URL=postgresql://myapp_admin:${MYAPP_ADMIN_PASSWORD}@db/myapp \
    python manage.py auditrum_makemigrations

DATABASE_URL=postgresql://myapp_admin:${MYAPP_ADMIN_PASSWORD}@db/myapp \
    python manage.py migrate

# Harden right after migration
DATABASE_URL=postgresql://myapp_admin:${MYAPP_ADMIN_PASSWORD}@db/myapp \
    auditrum harden --app-role myapp --admin-role myapp_admin

# Then the runtime app starts as the lower-privilege role
DATABASE_URL=postgresql://myapp:${MYAPP_PASSWORD}@db/myapp gunicorn ...
```

If you already deployed with `myapp` running migrations and now want to
transfer ownership to an admin role, run this as superuser:

```sql
-- Transfer ownership of every audit trigger function to myapp_admin.
-- Run once after creating myapp_admin.
DO $$
DECLARE
    fn record;
BEGIN
    FOR fn IN
        SELECT oid::regprocedure AS signature
        FROM pg_proc
        WHERE proname LIKE 'audit\_%\_trigger' ESCAPE '\'
           OR proname IN ('_audit_attach_context', '_audit_current_user_id',
                          '_audit_reconstruct_row', '_audit_reconstruct_table')
    LOOP
        EXECUTE format('ALTER FUNCTION %s OWNER TO myapp_admin', fn.signature);
    END LOOP;
END $$;
```

## Hash chain for tamper detection

Revoking `UPDATE` from `PUBLIC` is good, but an attacker with the
database superuser password (or a compromised backup restore path)
can still edit rows directly. The hash chain gives you
**detection** when that happens.

### Enable

```bash
auditrum enable-hash-chain
```

This runs:

```sql
CREATE EXTENSION IF NOT EXISTS pgcrypto;

ALTER TABLE auditlog ADD COLUMN IF NOT EXISTS row_hash text;
ALTER TABLE auditlog ADD COLUMN IF NOT EXISTS prev_hash text;

CREATE OR REPLACE FUNCTION auditlog_hash_chain_trigger() RETURNS trigger AS $$
DECLARE
    last_hash text;
    payload text;
BEGIN
    PERFORM pg_advisory_xact_lock(hashtext('auditlog'));

    SELECT row_hash INTO last_hash
    FROM auditlog
    WHERE id < NEW.id AND row_hash IS NOT NULL
    ORDER BY id DESC
    LIMIT 1;

    NEW.prev_hash := last_hash;
    payload := COALESCE(NEW.id::text, '') || '|' ||
               COALESCE(NEW.changed_at::text, '') || '|' ||
               COALESCE(NEW.operation, '') || '|' ||
               COALESCE(NEW.table_name, '') || '|' ||
               COALESCE(NEW.old_data::text, '') || '|' ||
               COALESCE(NEW.new_data::text, '') || '|' ||
               COALESCE(last_hash, '');
    NEW.row_hash := encode(digest(payload, 'sha256'), 'hex');
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER auditlog_hash_chain
BEFORE INSERT ON auditlog
FOR EACH ROW EXECUTE FUNCTION auditlog_hash_chain_trigger();
```

Every new audit row gets a `row_hash` computed from its own content
plus the previous row's hash. The advisory xact-lock serialises
inserts against the hash chain trigger so two concurrent writers
cannot both compute hashes against the same "previous" row.

### Cost

The serialisation via advisory lock means hash-chained inserts are
**effectively single-writer** through the audit log. If your workload
writes 10k audit events/s you'll hit contention here. Measure before
enabling in production-scale apps. For compliance-heavy but lower-
throughput workloads (fintech, healthtech), the cost is negligible.

### Verify

```bash
auditrum verify-chain
```

Runs a single server-side query that recomputes the expected hash for
every row using a `LAG` window function, compares it to the stored
`row_hash`, and reports mismatches:

```
chain OK: 48271 rows verified
```

Or on tampered data:

```
chain BROKEN: 1 of 48271 rows invalid
  - row 12847: row_hash mismatch
```

The verification query is **server-side recomputation** (not Python
re-implementation), so it's byte-perfect with the trigger's hashing
behaviour — no drift between the Python verifier and the PL/pgSQL
trigger. Details in
[`auditrum/hash_chain.py`](../auditrum/hash_chain.py) and
[`tests/integration/test_hardening_pg.py`](../tests/integration/test_hardening_pg.py).

### Hash chain does **not** prevent tampering

Crucially: hash chain is **detection**, not prevention. An attacker
who edits row 12847 can also recompute the hash and rewrite every
subsequent `row_hash`/`prev_hash`. The chain protects you against
**casual** or **accidental** tampering and against an attacker who
doesn't know about the chain. Against a motivated attacker with
sustained DB access, pair with:

- Periodic export of the `row_hash` of the latest audit row to an
  external immutable store (S3 with Object Lock, an append-only
  Kafka topic, a printed paper log, etc.). A genuine tamper trace
  requires the attacker to also rewrite the external record.
- Cryptographic signing of batch exports.
- Real WORM storage for the log (tangential but relevant).

## Retention and purging

Compliance regimes like GDPR require you to delete personal data on
request and within policy deadlines. Audit logs are the hardest place
to do this because of the tension between tamper detection and
right-to-erasure.

### Simple age-based purge

```bash
# DELETE rows older than 2 years
auditrum purge --older-than "2 years"

# Preview the generated SQL
auditrum purge --older-than "30 days" --dry-run

# Drop whole month-partitions instead — WAL-friendly for big tables
auditrum purge --older-than "2 years" --drop-partitions
```

`--drop-partitions` uses `DROP TABLE ... IF EXISTS` on every monthly
partition whose upper bound is older than the cutoff. This is
dramatically cheaper than DELETE at scale — it's a metadata operation,
not a row-by-row WAL write.

Supported interval vocabulary: `N days`, `N weeks`, `N months`,
`N years`. Parsed on the Python side, converted to an absolute cutoff,
and bound as a literal — no injection risk.

### Retention schedule

For a typical production setup, put this in a nightly cron:

```bash
# /etc/cron.daily/auditrum-retention
auditrum purge --older-than "2 years" --drop-partitions >> /var/log/auditrum-retention.log 2>&1
```

Pair with `auditrum create-partitions --months 3` to stay ahead of the
current-month boundary:

```bash
# Monthly
auditrum create-partitions --months 6
```

(Running the partition cron on day 25 of each month gives you a 5-day
safety margin before month roll-over.)

### GDPR

auditrum does **not** currently ship a subject-level
pseudonymization/erasure tool — the tension between "tamper-evident
hash chain" and "GDPR erasure" is real and requires careful design
(see the discussion in [CHANGELOG.md](../CHANGELOG.md#unreleased)).

Practical short-term approaches:

- **Retention is your erasure tool for routine cases.** Set
  `--older-than "30 days"` or whatever your policy demands, cron it.
  Subject data naturally ages out.
- **For named erasure requests** on data inside the retention window,
  the current recommendation is to rebuild the affected partitions
  with PII replaced by deterministic hashes, recompute the hash chain
  starting from the break point, and store a meta-audit record of the
  operation in a separate tamper-evident store. Tooling for this is
  on the roadmap but not yet in the CLI — if you need it today, drop
  an issue with your exact requirements.
- **If you cannot erase**, the pseudonymization trick (replace
  identifying columns with salted hashes at insert time, keep the
  mapping in a separate ACL-controlled table) is compliant with GDPR
  Article 4(5) for many use cases but is a project-level decision,
  not a CLI operation.

## Putting it all together

A hardened production setup looks like this:

```bash
# One-time, at deploy:
auditrum harden --app-role app_user --admin-role app_admin
auditrum enable-hash-chain

# Recurring, via cron (run as app_admin):
auditrum create-partitions --months 6         # monthly
auditrum purge --older-than "2 years" --drop-partitions  # daily
auditrum verify-chain                           # daily, alert on failure
```

Combined with the other defences — append-only triggers, ORM mixin
for read-only querying, single-table design — you get:

- Application can **only append**.
- Admin role can do maintenance (partition drop, retention).
- Any tampering at the DB level is **detected** by the hash chain.
- Retention is a **single command** that scales to huge tables via
  partition drops.
- GDPR routine requests are satisfied through retention; edge cases
  have a known (if manual) pattern.

## What's next

- [Observability](observability.md) — wire `verify-chain` alerts to
  Prometheus / Sentry
- [Architecture](architecture.md) — why a single linear log is
  prerequisite for a meaningful hash chain
- [CLI reference](cli.md) — every option on `harden`, `purge`,
  `enable-hash-chain`, `verify-chain`
