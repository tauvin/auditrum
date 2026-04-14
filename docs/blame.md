# `auditrum blame`

Git-style history for a single audited row. The CLI command that makes
it easy to answer "who changed this, when, and why" without writing any
SQL.

## Quick reference

```bash
# Full history of one row
auditrum blame users 42

# History of one column only
auditrum blame users 42 email

# JSON output for scripts
auditrum blame users 42 --format json
```

## What it shows

For each event on the row, one line containing:

- operation (`INSERT` / `UPDATE` / `DELETE`, colored in `rich` mode)
- timestamp
- actor — resolved from `auditlog.user_id` and/or the context metadata
  username/source
- what changed — full field list for INSERT/DELETE, comma-separated
  column names for UPDATE, or value transition when a field filter is
  used
- optional change reason from context metadata
- short context id prefix so you can pivot to other events in the same
  request

Example output (rich mode):

```
Audit history for orders:42
──────────────────────────────
[INSERT] 2024-06-12 14:23:00  user=7 (alice)               inserted (4 fields)  ctx=abc12345
[UPDATE] 2024-09-03 09:11:22  user=7 (alice)               changed: status     reason="order confirmed"  ctx=def45678
[UPDATE] 2025-01-20 16:45:11  user=42 (admin_bob)          changed: status,note reason="compliance review"  ctx=ghi78901
[DELETE] 2025-03-01 11:02:00  source=cron                  deleted (4 fields)  reason="30-day retention"  ctx=jkl01234
```

## Field narrowing

Pass a column name as the third argument to get just the events that
touched that column:

```bash
auditrum blame users 42 email
```

```
Audit history for users:42 (field: email)
─────────────────────────────────────────
[INSERT] 2024-06-12 14:23:00  user=7 (alice)        → "a@example.com"
[UPDATE] 2024-09-03 09:11:22  user=7 (alice)        "a@example.com" → "a2@example.com"
[UPDATE] 2025-01-20 16:45:11  user=42 (admin_bob)   "a2@example.com" → "a3@example.com"  reason="email change request"
```

This is the single most useful mode for investigating "when did this
value last change". The query still uses the composite
`(table_name, object_id, changed_at DESC)` index — filtering by field
happens in Python after the row pull, which is cheap because the
pre-filter row count is already small.

## Output formats

### `rich` (default)

Terminal output with color and alignment. Uses the same `rich` library
that colorises the rest of the CLI. Good for interactive investigation.

### `text`

Plain ASCII output, no color markup. Suitable for piping into `less`,
logging to files, pasting into tickets, or any environment that can't
render ANSI.

```bash
auditrum blame users 42 --format text
```

### `json`

Machine-readable output for scripting. Each event becomes an object in
a top-level array:

```bash
auditrum blame users 42 --format json | jq '.[] | select(.operation == "DELETE")'
```

```json
[
  {
    "changed_at": "2024-06-12T14:23:00+00:00",
    "operation": "INSERT",
    "user_id": 7,
    "context_id": "abc12345-...",
    "context_metadata": {"username": "alice", "source": "http"},
    "old_value": null,
    "new_value": {"id": 42, "email": "a@example.com"},
    "change_reason": null,
    "diff": null
  },
  ...
]
```

## Common workflows

### Investigating a data corruption report

User reports their email is wrong. Check when it last changed:

```bash
auditrum blame users 42 email
```

Find the offending event, note the context id, then pull every event
from that request:

```bash
psql -c "SELECT table_name, object_id, operation, diff
         FROM auditlog
         WHERE context_id = 'def45678-...'
         ORDER BY changed_at"
```

That tells you everything else that happened in the same logical unit
of work — useful for spotting batched changes.

### "Who deleted this row?"

```bash
auditrum blame orders 42 --format json | jq '.[] | select(.operation == "DELETE")'
```

The `user_id` field is typed (populated by `_audit_current_user_id()`
from the context metadata), so it's indexed and the query is cheap
even across millions of audit rows.

### Feeding a GDPR subject-access request

A user asks for everything you know about them. Blame every tracked
row for that user:

```bash
auditrum blame users 42 --format json > subject_42_users.json

psql -c "SELECT object_id FROM orders WHERE user_id = 42" | \
  while read oid; do
    auditrum blame orders "$oid" --format json >> subject_42_orders.jsonl
  done
```

Combine with `jq` to strip out fields you don't want to disclose.

### Catching bulk updates

Bulk operations bypass Django signals but **never** bypass triggers —
`auditrum blame <table> <id>` will show `bulk_update` mass writes as
individual rows. If you see dozens of events with the same
`context_id` at the same timestamp, that's a bulk path.

## Behind the command

The CLI is a thin wrapper around `auditrum.blame.fetch_blame` and
`auditrum.blame.format_blame`. You can use them directly:

```python
from auditrum.blame import fetch_blame, format_blame

with psycopg.connect(dsn) as conn:
    entries = fetch_blame(
        conn,
        table="orders",
        object_id="42",
        field="status",
        limit=200,
    )
    print(format_blame(entries, field="status", fmt="text"))
```

`fetch_blame` returns a list of `BlameEntry` dataclasses — handy for
rendering in a web dashboard, exporting to CSV, or hooking into a
Slack bot that reports suspicious DELETEs.

Full field reference for `BlameEntry`:

| Field             | Type                      | Description                                                      |
|-------------------|---------------------------|------------------------------------------------------------------|
| `changed_at`      | `datetime`                | When the trigger fired                                           |
| `operation`       | `str`                     | `INSERT`, `UPDATE`, or `DELETE`                                  |
| `user_id`         | `int \| None`             | Typed, populated via `_audit_current_user_id()`                  |
| `context_id`      | `str \| None`             | UUID of the shared per-request context, if any                   |
| `context_metadata`| `dict \| None`            | Full metadata jsonb: username, source, url, method, trace_id, ...|
| `old_value`       | `Any`                     | Full row (dict) in normal mode, field value in field-narrow mode |
| `new_value`       | `Any`                     | Same, but after the event                                        |
| `change_reason`   | `str \| None`             | Extracted from `context_metadata["change_reason"]`               |
| `diff`            | `dict \| None`            | For UPDATEs: only the columns that actually changed              |

## What's next

- [Time travel](time-travel.md) — when you need the full row state at
  a moment, not just the event log
- [Django integration](django.md) — the `AuditLog.objects` manager and
  `AuditedModelMixin` expose similar queries in Python code
