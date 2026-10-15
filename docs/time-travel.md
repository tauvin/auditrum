# Time travel

Reconstruct the state of any row — or any whole table — at any past
timestamp, without PostgreSQL's `temporal_tables` extension and without
per-model shadow tables. Reconstruction works for both storage modes
(see [Storage mode](concepts.md#storage-mode)): tables that keep full
`old_data` / `new_data` snapshots, and tables in `storage="diff"` mode
that keep only the paired `diff`. Columns outside the table's field
filter are never written to the audit log and are never returned.

## How it works

Two PL/pgSQL helper functions are installed alongside the audit log:

```sql
_audit_reconstruct_row(p_table text, p_object_id text, p_at timestamptz) RETURNS jsonb
_audit_reconstruct_table(p_table text, p_at timestamptz) RETURNS TABLE(object_id text, row_data jsonb)
```

Both are `STABLE SECURITY INVOKER`: they run with the caller's
privileges (see [Hardening](hardening.md#time-travel-privileges)). The
functions are `STABLE`, so every statement inside them sees the caller's
snapshot: the live row and the audit rows are consistent.

**Ordering and the suffix.** Events are ordered by `id`, not by
`changed_at`. `changed_at` is the transaction start time, so a
transaction that started first can write second; `id` follows the
order the audit rows were actually inserted, which is the order the row
changes were made. For a target time `at`, the *suffix* of an object is
every event from the first event (lowest `id`) with `changed_at > at`
onwards, including later events whose own `changed_at` happens to be
earlier.

**State at `at`**, decided per row:

1. If the suffix is non-empty, take its earliest event `E`.
    - `E` is an `INSERT`: the row did not exist, the result is `NULL`.
    - `E` carries an `old_data` snapshot (a `full` row): return it. This
      is the snapshot short-cut and costs the same as a plain lookup.
    - Otherwise start from the live row and, for each column, replace
      its value with the `old` of the earliest suffix event that touched
      that column. Columns no suffix event touched keep their live
      value. If the row no longer exists, the base is empty and the
      `DELETE` in the suffix supplies every column.
2. If the suffix is empty, take the latest event with `changed_at <= at`.
   A `new_data` snapshot is returned as is; a `DELETE` gives `NULL`.
   If the latest event has no snapshot (it is diff-only), or there are
   no events and the registry says the table is in diff mode, the
   result is the live row, or `NULL` if there is none. A full table with
   no events returns `NULL`.

Backward replay never needs the original `INSERT`, so dropping old
partitions stays safe.

**The live row and the registry.** For tables that have diff-only rows
the live row is read from the tracked table by primary key (`id`), and
the field filter and storage mode come from `auditrum_applied_triggers`.
Such a table raises an error when the live row is needed and the
registry has no readable entry for it (the row is missing or the caller
has no `SELECT` on the registry). `full` tables never need the live row
for the snapshot path, so installs without a registry entry, and
callers without `SELECT` on the registry, keep working.

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

Streams `(object_id, row_data)` tuples from a single set-based query
that groups events per `object_id`. Deleted rows are filtered out
automatically. Under the hood this is a single round trip — if the
table has a million historical events but only a few thousand distinct
`object_id`s, you get back a few thousand rows. On a diff table it also
scans the live table; see [Semantics and limits](#semantics-and-limits)
for the cost.

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

## Semantics and limits

- **Excluded columns are never returned.** Columns outside the field
  filter are not in the audit log, and the live row is filtered with
  the same rule, so they cannot leak into a reconstructed row.
- **Schema drift.** A column added after `at` shows its current value
  (the audit log has nothing older); a dropped column disappears from
  the result.
- **`changed_at` is the transaction start.** A change written before
  `at` by a transaction that committed after `at` counts as applied at
  `at`. This is inherent to `now()`.
- **Log conditions change the picture.** A `log_condition` that
  suppresses updates leaves gaps in the history, so full tables may
  reconstruct a stale state. Diff storage cannot be combined with
  `log_condition` for exactly this reason.
- **Cost.** `reconstruct_row` is O(events after `at`) for the object.
  It reads the latest event with `ORDER BY id DESC`, which the
  `(table_name, object_id, changed_at DESC)` index cannot order, so a
  lookup reads the object's whole history. `reconstruct_table` filters
  only on `table_name`, so it reads every event of the table (and, on a
  diff table, scans the live table too), however recent `at` is.
  Reconstructing a table as of a year ago still costs a pass over its
  whole audit history. Only the probe for events after `at` uses a
  `changed_at > at` range.
- **Per-row rule after a mode switch.** A table that switches from diff
  back to full keeps reading the live row for objects whose latest
  event is diff-only. Untracked rows (no events at all) appear in
  `reconstruct_table` only while the table is in diff mode.
- **Privileges and errors.** `_audit_reconstruct_table` raises when
  diff-only rows exist but the table has no readable registry entry.
  `_audit_reconstruct_row` raises only when the requested row needs the
  live table.
- **The reconstructable window is the retained window.** Once a
  partition is dropped, states inside it can no longer be rebuilt.
- **Writes that bypass the trigger break diff reconstruction silently.**
  `session_replication_role = replica`, `ALTER TABLE ... DISABLE
  TRIGGER`, `TRUNCATE` and bulk loads that skip triggers leave no
  events, so a diff table reconstructs a wrong state around them.
- **No PII masking on historical queries.** Time travel gives you
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
