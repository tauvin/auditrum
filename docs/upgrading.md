# Upgrade guide (0.3.x → 1.0)

This covers upgrading an existing auditrum install across versions. It is
distinct from the [migration cookbook](migration-cookbook.md), which is
about moving *to* auditrum from another audit system.

Good news: the public API has been stable since 0.4, so most upgrades are
"bump the version and migrate." The breaking changes are concentrated in
the **0.3 → 0.4** transition; see
[What's new in 1.0](whats-new-1.0.md#breaking-changes-since-03x) for the
overview and the [Changelog](changelog.md) for per-release detail.

## Standard upgrade procedure

Run as the admin role (see [deployment](deployment.md)), ideally on
staging first:

```bash
pip install -U "auditrum[django]"

# 1. Core schema migrations (drop content_type, refresh helpers, GIN opt-in)
python manage.py migrate auditrum_django

# 2. Regenerate per-app trigger migrations if a trigger body changed,
#    then apply them
python manage.py auditrum_makemigrations
python manage.py migrate

# 3. Make sure the DB-side PL/pgSQL helpers match the installed version
python manage.py auditrum_refresh_schema   # --dry-run to preview
```

Then verify (below). Most upgrades need nothing beyond this. The
sections that follow only apply if you are crossing the version where a
given breaking change landed.

## Breaking changes and what to do

### `content_type_id` removed — from ≤ 0.3.x (landed 0.4.0)

The `auditlog.content_type_id` column and `AuditLog.content_type` /
`content_object` GenericForeignKey were removed; identity is keyed on
`table_name`. Migration `0002_drop_content_type_id` drops the column for
you, and the admin History view now routes through `for_object`.

**Action:** none, unless your own code filtered on `content_type` —
switch those queries to `AuditLog.objects.for_object(obj)` /
`for_model(Model)`.

### Frozen schema helpers — from ≤ 0.3.x (fixed 0.4.2)

Before 0.4.2, the PL/pgSQL helper bodies (`jsonb_diff`, the reconstruct
functions, …) were emitted once and never refreshed on upgrade, so a
`pip install -U` left them at the old revision.

**Action:** run `auditrum_refresh_schema` (step 3 above) — or rely on
migration `0003_refresh_schema_04`. Without it, new rows may still be
written in the old diff shape.

### Paired `diff` shape — from ≤ 0.4.0 (landed 0.4.1)

`auditlog.diff` changed from `{field: new_value}` (UPDATE only) to the
paired `{field: {old, new}}` shape for **every** operation, and the
`jsonb_strip_nulls` wrapper was removed (so `UPDATE … SET x = NULL`
events are no longer dropped).

**New rows** use the new shape automatically once you've re-run
`auditrum_makemigrations` + `migrate`. **Existing rows** stay in the old
format unless you back-fill them. Run this once, in a transaction, on a
quiet window (it's idempotent — the UPDATE guard makes a partial run
safe to retry):

```sql
-- UPDATE rows: {field: new_val} → {field: {old: old_val, new: new_val}}
UPDATE auditlog
SET diff = (
    SELECT jsonb_object_agg(d.key, jsonb_build_object('old', old_data -> d.key, 'new', d.value))
    FROM jsonb_each(diff) d
)
WHERE operation = 'UPDATE' AND diff IS NOT NULL
  AND NOT EXISTS (
      SELECT 1 FROM jsonb_each(diff) d
      WHERE jsonb_typeof(d.value) = 'object' AND d.value ? 'old' AND d.value ? 'new'
  );

-- INSERT rows: historically diff = NULL → rebuild from new_data
UPDATE auditlog
SET diff = (
    SELECT jsonb_object_agg(d.key, jsonb_build_object('old', NULL, 'new', d.value))
    FROM jsonb_each(new_data) d
)
WHERE operation = 'INSERT' AND diff IS NULL AND new_data IS NOT NULL;

-- DELETE rows: same, from old_data
UPDATE auditlog
SET diff = (
    SELECT jsonb_object_agg(d.key, jsonb_build_object('old', d.value, 'new', NULL))
    FROM jsonb_each(old_data) d
)
WHERE operation = 'DELETE' AND diff IS NULL AND old_data IS NOT NULL;
```

The fully-annotated version (with notes on partitioned tables) is in the
[0.4.1 changelog entry](changelog.md). If the table is large, partition
the back-fill by `changed_at` range.

### `_validate_ident` removed — (landed 0.3.2)

The underscore-prefixed alias is gone.

**Action:** if you imported it, switch to
`from auditrum.tracking.spec import validate_identifier`.

### `diff` GIN index is opt-in — from ≤ 0.4.x (landed 0.5)

The GIN index on `auditlog.diff` is no longer created by default —
production data showed it unused (0 scans) but costing hundreds of MB per
partition and taxing every write. Migration `0004_diff_gin_index_optional`
**drops it** unless you opt back in.

**Action:** only if you run `WHERE diff @> '{...}'` jsonb-containment
queries — set `PGAUDIT_DIFF_GIN_INDEX = True` in settings *before*
migrating to keep the index. Otherwise do nothing; dropping it reclaims
disk and speeds up writes. See
[performance tuning](performance-tuning.md#8-drop-the-unused-gin-on-diff-index).

## New settings worth reviewing

Added across the 0.4–0.5 cycle (all have safe defaults):

* `PGAUDIT_HASH_CHAIN` — opt into the tamper-evidence chain (default off).
* `PGAUDIT_DIFF_GIN_INDEX` — keep the diff GIN index (default off).

See the [Django guide](django.md) and [API stability](api-stability.md)
for the full settings surface.

## Verifying the upgrade

* Run your test suite.
* Make a change to a tracked model and confirm a fresh `auditlog` row
  lands with the paired diff shape.
* If the hash chain is enabled, run `python manage.py auditrum_verify_chain`.
* Confirm `auditrum_refresh_schema --dry-run` reports nothing left to
  change.
