"""Integration: compact_snapshots against real Postgres."""

import pytest

from auditrum.compaction import compact_snapshots
from auditrum.executor import PsycopgExecutor
from auditrum.hash_chain import generate_hash_chain_sql
from auditrum.tracking import FieldFilter, TrackSpec, TriggerManager


def install(conn, storage):
    mgr = TriggerManager(PsycopgExecutor(conn))
    mgr.bootstrap()
    mgr.install(
        TrackSpec(table="cs_items", fields=FieldFilter.exclude("secret"), storage=storage),
        force=True,
    )


def now(conn):
    with conn.cursor() as cur:
        cur.execute("SELECT clock_timestamp()")
        return cur.fetchone()[0]


def bypass(conn, statement, params=()):
    """Run a write with row triggers disabled for this session."""
    with conn.cursor() as cur:
        cur.execute("SET session_replication_role = replica")
        try:
            cur.execute(statement, params)
        finally:
            cur.execute("SET session_replication_role = origin")


def scalar(conn, query, params=()):
    with conn.cursor() as cur:
        cur.execute(query, params)
        return cur.fetchone()[0]


def snapshot_count(conn, object_id=None):
    """Rows of cs_items that still hold a snapshot (optionally of one object)."""
    query = (
        "SELECT count(*) FROM auditlog WHERE table_name = 'cs_items' "
        "AND (old_data IS NOT NULL OR new_data IS NOT NULL)"
    )
    if object_id is None:
        return scalar(conn, query)
    return scalar(conn, query + " AND object_id = %s", (object_id,))


@pytest.fixture
def items(fresh_auditlog):
    conn = fresh_auditlog
    with conn.cursor() as cur:
        cur.execute("DROP TABLE IF EXISTS cs_items CASCADE")
        cur.execute("CREATE TABLE cs_items (id int PRIMARY KEY, name text, secret text, n int)")
    yield conn
    with conn.cursor() as cur:
        cur.execute("DROP TABLE IF EXISTS cs_items CASCADE")


def write_full_era(conn):
    """A history in full mode; returns a timestamp captured after each statement."""
    stamps = []
    statements = [
        *[f"INSERT INTO cs_items VALUES ({i}, 'a', 's', 0)" for i in (1, 2, 3, 4)],
        "UPDATE cs_items SET name = 'b' WHERE id IN (1, 2)",
        "UPDATE cs_items SET n = 5 WHERE id = 1",
        "UPDATE cs_items SET name = 'c', n = 7 WHERE id = 3",
        "DELETE FROM cs_items WHERE id = 3",
        "INSERT INTO cs_items VALUES (3, 'r', 's', 1)",
        "UPDATE cs_items SET name = 'd' WHERE id = 4",
        "UPDATE cs_items SET n = 9 WHERE id = 2",
        "UPDATE cs_items SET n = 4 WHERE id = 5",
    ]
    for statement in statements:
        with conn.cursor() as cur:
            cur.execute(statement)
        stamps.append(now(conn))
    return stamps


def write_diff_era(conn):
    stamps = []
    for statement in (
        "UPDATE cs_items SET name = 'e' WHERE id = 1",
        "UPDATE cs_items SET n = 11 WHERE id IN (2, 4)",
        "UPDATE cs_items SET name = 'f' WHERE id = 3",
    ):
        with conn.cursor() as cur:
            cur.execute(statement)
        stamps.append(now(conn))
    return stamps


def full_then_diff(conn):
    # Object 5 exists before tracking starts, so it never gets an INSERT event.
    with conn.cursor() as cur:
        cur.execute("INSERT INTO cs_items VALUES (5, 'p', 's', 0)")
    install(conn, "full")
    stamps = write_full_era(conn)
    install(conn, "diff")
    stamps += write_diff_era(conn)
    return stamps


def reconstruction(conn, stamps):
    rows = {}
    with conn.cursor() as cur:
        for object_id in "12345":
            for ts in stamps:
                cur.execute("SELECT _audit_reconstruct_row('cs_items', %s, %s)", (object_id, ts))
                rows[("row", object_id, ts)] = cur.fetchone()[0]
        for ts in (stamps[3], stamps[8], stamps[-1]):
            cur.execute("SELECT * FROM _audit_reconstruct_table('cs_items', %s)", (ts,))
            rows[("table", ts)] = sorted((r[0], str(r[1])) for r in cur.fetchall())
    return rows


def test_reconstruction_unchanged_after_compaction(items):
    stamps = full_then_diff(items)
    before = reconstruction(items, stamps)
    full_era_rows = snapshot_count(items)
    assert full_era_rows > 0

    result = compact_snapshots(items, table="cs_items")

    assert result.compacted == full_era_rows
    assert result.excluded == ()
    assert result.skipped_hashed == 0
    assert snapshot_count(items) == 0
    assert reconstruction(items, stamps) == before


def test_dry_run_changes_nothing(items):
    full_then_diff(items)
    held = snapshot_count(items)

    dry = compact_snapshots(items, table="cs_items", dry_run=True)

    assert dry.dry_run is True
    assert snapshot_count(items) == held
    real = compact_snapshots(items, table="cs_items")
    assert real.dry_run is False
    assert real.compacted == dry.compacted == held


def test_full_table_is_refused(items):
    install(items, "full")
    with pytest.raises(ValueError, match="diff"):
        compact_snapshots(items, table="cs_items")


def test_before_limits_rows(items):
    install(items, "full")
    stamps = write_full_era(items)
    install(items, "diff")
    cutoff = stamps[4]

    compact_snapshots(items, table="cs_items", before=cutoff)

    assert (
        scalar(
            items,
            "SELECT count(*) FROM auditlog WHERE table_name = 'cs_items' AND changed_at < %s "
            "AND (old_data IS NOT NULL OR new_data IS NOT NULL)",
            (cutoff,),
        )
        == 0
    )
    assert (
        scalar(
            items,
            "SELECT count(*) FROM auditlog WHERE table_name = 'cs_items' AND changed_at >= %s "
            "AND (old_data IS NOT NULL OR new_data IS NOT NULL)",
            (cutoff,),
        )
        > 0
    )


def test_broken_object_is_excluded(items):
    full_then_diff(items)
    bypass(items, "UPDATE cs_items SET name = 'zz' WHERE id = 2")
    object_two = snapshot_count(items, "2")
    assert object_two > 0

    result = compact_snapshots(items, table="cs_items")

    assert snapshot_count(items, "2") == object_two
    for other in ("1", "3", "4"):
        assert snapshot_count(items, other) == 0
    assert [f.object_id for f in result.excluded] == ["2"]


def test_hashed_rows_are_kept(items):
    with items.cursor() as cur:
        cur.execute(generate_hash_chain_sql("auditlog"))
    full_then_diff(items)
    held = snapshot_count(items)
    assert held > 0

    result = compact_snapshots(items, table="cs_items")

    assert result.compacted == 0
    assert result.skipped_hashed == held
    assert snapshot_count(items) == held


def test_values_only_rows_are_kept(items):
    install(items, "diff")
    with items.cursor() as cur:
        cur.execute(
            "INSERT INTO auditlog (operation, changed_at, object_id, table_name, "
            "old_data, new_data, diff) VALUES ('UPDATE', now(), '1', 'cs_items', "
            """'{"name": "w"}', '{"name": "x"}', '{"name": "x"}'::jsonb)"""
        )

    result = compact_snapshots(items, table="cs_items")

    assert result.compacted == 0
    assert snapshot_count(items) == 1


def test_rows_without_object_id_are_kept_and_paging_ends(items):
    full_then_diff(items)
    held = snapshot_count(items)
    with items.cursor() as cur:
        cur.execute(
            "INSERT INTO auditlog (operation, changed_at, object_id, table_name, "
            "old_data, new_data, diff) VALUES ('UPDATE', now(), NULL, 'cs_items', "
            """'{"name": "w"}', '{"name": "x"}', '{"name": {"old": "w", "new": "x"}}'::jsonb)"""
        )

    result = compact_snapshots(items, table="cs_items", batch_size=2)

    assert result.compacted == held
    assert snapshot_count(items) == 1


def test_small_batches_process_everything(items):
    full_then_diff(items)
    held = snapshot_count(items)

    result = compact_snapshots(items, table="cs_items", batch_size=2)

    assert result.compacted == held
    assert snapshot_count(items) == 0


def test_second_run_is_noop(items):
    full_then_diff(items)
    assert compact_snapshots(items, table="cs_items").compacted > 0
    assert compact_snapshots(items, table="cs_items").compacted == 0


def test_bypass_on_a_column_no_diff_touched_excludes_the_object(items):
    with items.cursor() as cur:
        cur.execute("INSERT INTO cs_items VALUES (1, 'a', 's', 0)")
    install(items, "full")
    with items.cursor() as cur:
        cur.execute("UPDATE cs_items SET n = 5 WHERE id = 1")
    stamp = now(items)
    install(items, "diff")
    bypass(items, "UPDATE cs_items SET name = 'zz' WHERE id = 1")
    query = "SELECT _audit_reconstruct_row('cs_items', '1', %s)"
    before = scalar(items, query, (stamp,))

    result = compact_snapshots(items, table="cs_items")

    assert [f.object_id for f in result.excluded] == ["1"]
    assert result.compacted == 0
    assert snapshot_count(items, "1") == 1
    assert scalar(items, query, (stamp,)) == before


def test_log_condition_table_is_refused(items):
    mgr = TriggerManager(PsycopgExecutor(items))
    mgr.bootstrap()
    mgr.install(TrackSpec(table="cs_items", storage="full", log_condition="NEW.n > 0"), force=True)
    with pytest.raises(ValueError):
        compact_snapshots(items, table="cs_items")


def raw_snapshot_history(conn, operation):
    """A pre-0.6.2 history under only("name"): the snapshots also hold n.

    The trigger of that time stored to_jsonb(OLD) / to_jsonb(NEW) unfiltered;
    the snapshots are patched to that form. The filter is then widened to
    (name, n) and the table switched to diff mode.
    """
    mgr = TriggerManager(PsycopgExecutor(conn))
    mgr.bootstrap()
    mgr.install(
        TrackSpec(table="cs_items", fields=FieldFilter.only("name"), storage="full"), force=True
    )
    with conn.cursor() as cur:
        cur.execute("INSERT INTO cs_items VALUES (1, 'a', 's', 0)")
    stamp = now(conn)
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE auditlog SET new_data = %s::jsonb WHERE operation = 'INSERT'",
            ('{"id": 1, "name": "a", "secret": "s", "n": 0}',),
        )
        if operation == "UPDATE":
            cur.execute("UPDATE cs_items SET name = 'b', n = 5 WHERE id = 1")
            new_data = '{"id": 1, "name": "b", "secret": "s", "n": 5}'
        else:
            cur.execute("DELETE FROM cs_items WHERE id = 1")
            new_data = None
        cur.execute(
            "UPDATE auditlog SET old_data = %s::jsonb, new_data = %s::jsonb "
            "WHERE operation = %s RETURNING id",
            ('{"id": 1, "name": "a", "secret": "s", "n": 0}', new_data, operation),
        )
        audit_id = cur.fetchone()[0]
    mgr.install(
        TrackSpec(table="cs_items", fields=FieldFilter.only("name", "n"), storage="diff"),
        force=True,
    )
    return stamp, audit_id


def test_snapshot_change_missing_from_the_diff_excludes_the_object(items):
    stamp, audit_id = raw_snapshot_history(items, "UPDATE")
    with items.cursor() as cur:
        cur.execute("UPDATE cs_items SET name = 'c' WHERE id = 1")
    query = "SELECT _audit_reconstruct_row('cs_items', '1', %s)"
    before = scalar(items, query, (stamp,))
    held = snapshot_count(items, "1")

    result = compact_snapshots(items, table="cs_items")

    [finding] = result.excluded
    assert (finding.object_id, finding.audit_id, finding.kind) == ("1", audit_id, "chain")
    assert (finding.column, finding.expected, finding.actual) == ("n", 0, 5)
    assert "diff does not" in finding.detail
    assert result.compacted == 0
    assert snapshot_count(items, "1") == held
    assert scalar(items, query, (stamp,)) == before


def test_deleted_snapshot_column_missing_from_the_diff_excludes_the_object(items):
    stamp, audit_id = raw_snapshot_history(items, "DELETE")
    query = "SELECT _audit_reconstruct_row('cs_items', '1', %s)"
    before = scalar(items, query, (stamp,))
    held = snapshot_count(items, "1")

    result = compact_snapshots(items, table="cs_items", dry_run=True)
    [finding] = result.excluded
    assert (finding.object_id, finding.audit_id, finding.column) == ("1", audit_id, "n")
    assert (finding.expected, finding.actual) == (0, None)
    assert result.compacted == 0

    compact_snapshots(items, table="cs_items")
    assert snapshot_count(items, "1") == held
    assert scalar(items, query, (stamp,)) == before


def test_findings_from_every_batch_are_reported(items):
    full_then_diff(items)
    bypass(items, "UPDATE cs_items SET name = 'zz' WHERE id IN (2, 4)")

    result = compact_snapshots(items, table="cs_items", batch_size=1)

    assert [f.object_id for f in result.excluded] == ["2", "4"]
    assert snapshot_count(items, "1") == snapshot_count(items, "3") == 0
    assert snapshot_count(items, "2") > 0
    assert snapshot_count(items, "4") > 0
