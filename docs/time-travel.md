# Time travel

Reconstruct the state of any row — or any whole table — at any past
timestamp, without PostgreSQL's `temporal_tables` extension and without
per-model shadow tables. Because every audit event stores the full row
snapshot in `old_data` / `new_data`, the "latest event at or before
`t`" gives us back the entire state.

## How it works

Two PL/SQL helper functions are installed alongside the audit log:

```sql
_audit_reconstruct_row(p_table text, p_object_id text, p_at timestamptz) RETURNS jsonb
_audit_reconstruct_table(p_table text, p_at timestamptz) RETURNS TABLE(object_id text, row_data jsonb)
```

Both are `STABLE` SQL functions that walk the
`(table_name, object_id, changed_at DESC)` composite index for an
O(log n) lookup per row. `DELETE` events map to `NULL` (the row didn't
exist at that time); a row that was inserted, deleted, and re-inserted
returns whatever state the most recent event before the target
timestamp describes.

You can call these directly from SQL, or through the Python wrappers
in `auditrum.timetravel`, or via the Django/SA helpers that ship on
top.

## Python API

```python
from datetime import datetime, UTC
from auditrum.timetravel import (
    reconstruct_row,
    reconstruct_table,
    reconstruct_field_history,
    HistoricalRow,
)
```

### `reconstruct_row` — one row at one moment

```python
row = reconstruct_row(
    conn,
    table="orders",
    object_id="42",
    at=datetime(2024, 6, 1, 14, 23, tzinfo=UTC),
)
if row is None:
    print("row didn't exist at that time")
else:
    print(row)           # dict shaped like the original row
    print(row["status"])
```

Returns `None` when:

- No events exist for that `object_id` before `at` (the row was never
  created yet).
- The most recent event at or before `at` was a `DELETE` (the row was
  gone at that moment).

### `reconstruct_table` — every surviving row at one moment

```python
from datetime import datetime, UTC

snapshot = dict(reconstruct_table(
    conn, table="orders", at=datetime(2024, 1, 1, tzinfo=UTC)
))
for obj_id, row_data in snapshot.items():
    print(obj_id, row_data["status"], row_data["total"])
```

Streams `(object_id, row_data)` tuples via a server-side `DISTINCT ON
(object_id)` query. Deleted rows are filtered out automatically. Under
the hood this is a single round trip — if the table has a million
historical events but only a few thousand distinct `object_id`s, you
get back a few thousand rows.

### `reconstruct_field_history` — timeline of one column

```python
from auditrum.timetravel import reconstruct_field_history

for ts, value in reconstruct_field_history(
    conn, table="users", object_id="42", field="email"
):
    print(ts.isoformat(), value)
# 2024-01-15 10:00:00+00  a@x.com
# 2024-03-02 18:22:00+00  a2@x.com
# 2024-09-11 09:05:00+00  a3@x.com
# 2025-04-01 12:00:00+00  None     <- row deleted
```

Only events that **actually changed** the field are included. The
`INSERT` at the start produces the initial value; each subsequent
`UPDATE` that touches the field emits a new pair; a `DELETE` appends a
final `(timestamp, None)` entry.

Unlike `reconstruct_row`, which asks "what was the full row", this
asks "show me the lifecycle of one column". Useful for compliance
reports ("when did this email address change?") and for debugging
auth/security questions.

## `HistoricalRow`

The Django mixin and the framework-agnostic `timetravel` module both
expose a lightweight `HistoricalRow` wrapper over the raw jsonb.

```python
from auditrum.timetravel import HistoricalRow

row = HistoricalRow(
    table="orders",
    object_id="42",
    at=datetime(2024, 6, 1, tzinfo=UTC),
    data={"id": 42, "status": "paid", "total": 99.0},
)

row["status"]       # "paid" — dict-style access
row.status          # "paid" — attribute access
row.get("missing")  # None — safe .get fallback
"total" in row      # True
row.data            # {"id": 42, ...} — the underlying dict

# Build an unsaved model instance filtered to currently-existing fields
from myapp.models import Order
order = row.to_model(Order)
```

`to_model(model_cls)` copies only fields that are still on the model
class — if you dropped `total` from `Order` last month, `to_model`
silently omits it from the constructed instance but preserves it in
`row.data` for inspection. This is the escape hatch that makes the
single-table design tolerate model refactors without losing history.

`HistoricalRow` is frozen (`@dataclass(frozen=True)`), so you can't
accidentally mutate historical state.

## Django shortcut methods

If your model uses `AuditedModelMixin`:

```python
from datetime import datetime, UTC
from myapp.models import Order

order = Order.objects.get(pk=42)

# Row state at a past moment
snap = order.audit_at(datetime(2024, 6, 1, tzinfo=UTC))
# -> HistoricalRow | None

# Field timeline
for ts, status in order.audit_field_history("status"):
    print(ts, status)

# Whole-table snapshot (classmethod)
for row in Order.audit_state_as_of(datetime(2024, 1, 1, tzinfo=UTC)):
    print(row.object_id, row.data)
```

These all route through `reconstruct_*` with the current Django
connection and return the same `HistoricalRow` instances as the raw
API.

## CLI

```bash
# Single row as JSON
auditrum as-of orders "2024-06-01T14:23:00+00:00" --id 42

# Whole table as JSON Lines (streamable)
auditrum as-of orders "2024-06-01T00:00:00+00:00" --format jsonl

# Limit the stream for exploration
auditrum as-of orders "2024-06-01T00:00:00+00:00" --format jsonl --limit 100
```

Output examples:

```json
{
  "id": 42,
  "status": "paid",
  "total": 99.0,
  "tenant_id": 1
}
```

Or one-object-per-line with `--format jsonl` when streaming the whole
table, so you can pipe it into `jq`, Parquet tools, or a data lake
loader.

## Edge cases and caveats

- **Tracked fields only**. `reconstruct_row` returns the full row as
  captured in `new_data`, which is the full `to_jsonb(NEW)` snapshot.
  `track_only` / `exclude` settings affect which columns are in the
  `diff`, not in `old_data` / `new_data`. So time travel works even
  for fields you said you didn't want to diff.
- **Log conditions change the picture**. If the trigger has a
  `log_condition` that suppresses certain updates, time travel only
  reflects events that were actually recorded. A row whose `is_active`
  field flipped under a `log_condition="NEW.is_active"` gate might
  reconstruct to a stale state.
- **Partition pruning helps**. Because `auditlog` is partitioned by
  `changed_at`, a time-travel query for `at = 2024-06-01` only reads
  the partitions up to June 2024. The composite index further narrows
  the scan to a single row per object_id.
- **Timestamp at or before**. The semantics are "most recent event with
  `changed_at <= at`". If you pass a timestamp between two updates,
  you get the state after the earlier one.
- **No PII masking on historical queries**. Time travel gives you
  back whatever was in the row at the time. If you need to comply with
  erasure requests, see the pseudonymization notes in
  [hardening](hardening.md#gdpr).

## What's next

- [`auditrum blame`](blame.md) — git-style readable timeline for
  interactive queries
- [Hardening](hardening.md) — make the log itself tamper-evident so
  time travel is defensible
- [Architecture](architecture.md) — how the composite index makes all
  of this cheap
