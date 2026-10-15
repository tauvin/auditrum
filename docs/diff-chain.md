# Diff chain check

`verify-diff-chain` finds rows that were changed without going through
the audit trigger, so you know which objects
[time travel](time-travel.md) cannot be trusted for.

## Why

Reconstruction (and diff storage in particular) trusts that every write
to a tracked row went through the trigger. These paths skip it silently:

- `SET session_replication_role = replica` (used by restores and some
  loaders)
- `ALTER TABLE … DISABLE TRIGGER`
- `TRUNCATE`
- bulk loads that run with triggers off

After such a write the audit log no longer describes the row, and
nothing complains until someone reconstructs it.

The check is a **consistency check, not tamper evidence**: it compares
the audit log with itself and with the live rows. For tamper detection
see the [hash chain](hardening.md#hash-chain-for-tamper-detection).

## What is checked

Per object, and per column that is not excluded by the field filter
(excluded columns are never compared). It works for full and diff
tables alike. Each finding has one of three kinds, and only the first
finding per object (the one with the lowest event id) is reported.

**`chain`** — an event's `old` value differs from the `new` value of the
previous event that touched the same column. Something changed the
column between the two events without being audited. Example: a bypassing
`UPDATE orders SET status = 'paid'`, followed by an audited
`UPDATE orders SET status = 'shipped'`. The second event records
`old = 'paid'`, but the previous event left `status` at `'new'`; the
finding points at the second event.

**`live`** — the last event touching a column does not match the live
row. Example: the same bypassing `UPDATE` with no audited write after
it. The log says `status = 'new'`, the row says `'paid'`.

**`lifecycle`** — INSERT and DELETE do not match the row's existence.
Example: a bypassing `DELETE` leaves a last event that is not a DELETE
while the row is missing. The reverse is reported too: a last event
DELETE while the row exists, an INSERT for a row that already existed,
or an UPDATE after a DELETE without an INSERT.

## Windowed vs full

```bash
# Windowed: objects with events in the last day (default)
auditrum verify-diff-chain

# Everything
auditrum verify-diff-chain --full
```

A windowed run (`--since`, default `1 day`; same interval format as
`purge --older-than`, or an ISO-8601 timestamp) checks the objects that
have events in the window. It does not report a `chain` break or an
inside-history `lifecycle` finding older than the window, but it always
reports `live` mismatches and end-of-history `lifecycle` findings,
because they describe the current state.

A bypass after which the object has no audited write is invisible to a
windowed run until something touches that object. Only `--full` catches
it. `--full` ignores `--since`.

Recommended schedule: windowed daily, full weekly.

```
# daily, last day
0 3 * * * auditrum verify-diff-chain
# weekly, everything
0 4 * * 0 auditrum verify-diff-chain --full
```

With Django:

```
0 3 * * * cd /srv/app && python manage.py auditrum_verify_diff_chain
0 4 * * 0 cd /srv/app && python manage.py auditrum_verify_diff_chain --full
```

Without `--table` every table registered for the audit table is checked;
`--object-id` then applies to every one of them. If no table is
registered, the commands fail (exit 2, or `CommandError` in Django) with
"no tracked tables registered for audit table … — nothing checked",
so a misconfigured audit table does not pass as clean.

## Python API

```python
from datetime import UTC, datetime, timedelta

from auditrum.diff_chain import verify_diff_chain

findings = verify_diff_chain(
    conn,
    table="orders",
    since=datetime.now(UTC) - timedelta(days=1),
    # or full=True, or object_id=42
    limit=100,
)
for f in findings:
    print(f.table, f.object_id, f.audit_id, f.kind, f.column, f.detail)
```

At least one of `since`, `object_id` and `full` is required, otherwise
`ValueError`, so a full scan never happens by accident. `full=True`
ignores `since`; `object_id` narrows either mode to one object. The
table must be registered in `auditrum_applied_triggers` (tracked through
`TriggerManager`), otherwise `ValueError`.

Each `DiffChainMismatch` has:

| Field | Meaning |
|---|---|
| `table`, `object_id` | the affected object |
| `audit_id` | the event where the inconsistency shows |
| `kind` | `"chain"`, `"live"` or `"lifecycle"` |
| `column` | the column, for `chain` and `live`; `None` for `lifecycle` |
| `expected`, `actual` | decoded JSON values, or `None` |
| `detail` | human-readable description |

The CLI prints one line per finding (`--json` prints an array of these
fields) and exits 0 when clean, 1 with findings, 2 on error. `--limit`
caps the total across tables. A table that cannot be checked (for
example, it has no `id` column) is reported and skipped; the other
tables are still checked and their findings printed, and the run exits
2 naming the failed tables. The Django command does the same and raises
`CommandError` when anything is found or a table failed. Details: [CLI reference](cli.md#verify-diff-chain),
[Django integration](django.md#diff-chain-check).

## What to do with a finding

Reconstruction of that object is unreliable for times before the
finding. Find and close the bypass path first, so it does not happen
again. Then either restore the row from a backup, or accept the live
state and treat the finding as historical.

A historical finding keeps showing in `--full` runs. A windowed run
stops reporting a `chain` break once it is older than the window, while
a `live` or end-of-history `lifecycle` finding persists until the row
and its last event agree again, for example after the next audited
write of the affected columns.

## Limits

- A windowed run reads the whole history of every object it touches,
  because the previous event for a column can be arbitrarily old. This
  is the main cost on hot objects.
- If the field filter changed over time, a column added to the filter
  starts its chain at its first event after the change; a column
  removed from the filter is no longer compared with the live row.
- Tables tracked with a `log_condition` cannot be verified: events the
  condition skips are gaps by design. `verify_diff_chain` raises
  `ValueError` for them, and runs without `--table` leave them out.
- The tracked table needs a primary key column named `id`, the same
  assumption as time travel: the live row is looked up by it.
- Rows created before tracking, with no events at all, are not findings.
- Pre-0.4 rows whose `diff` holds only the new values are used as the
  new value of each column they list. The 0.3 trigger stripped changes
  that set a column to NULL from those diffs, so such a change is
  missing from the log and can still produce a finding.
- `TRUNCATE` shows up as `lifecycle` findings for every object whose
  last event is not a DELETE.
- The caller needs `SELECT` on the audit table, the trigger registry
  and the tracked table. No migration is required.
