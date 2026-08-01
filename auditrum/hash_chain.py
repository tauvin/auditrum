"""Optional hash-chain for tamper detection on the audit log.

When enabled via :func:`generate_hash_chain_sql`, every new row in the
audit log gets:

* a monotonically-increasing ``chain_seq`` assigned **inside** the
  per-table advisory lock — this is the chain order, distinct from the
  serial ``id`` which is assigned *before* the BEFORE INSERT trigger
  runs and can therefore drift from commit order under concurrency
* a ``row_hash`` computed as
  ``sha256(canonical_payload(id, changed_at, operation, table_name,
  old_data, new_data, user_id, object_id, diff, context_id, meta,
  prev_hash, prev_chain_seq))`` — the attribution columns are covered so
  WHO/why cannot be rewritten unnoticed, and ``prev_chain_seq`` (the
  previous surviving row's chain_seq) makes interior-row deletion
  detectable
* a ``prev_hash`` pointer to the previous row in chain order

``canonical_payload`` is a stable JSON encoding produced via
``jsonb_build_object`` cast to text. The canonical encoding is the
critical detail: a naive separator-joined concatenation
(``a || '|' || b``) allows collision attacks where an attacker inserts
a row whose own fields contain the separator and replicates a
legitimate row's hash. JSON encoding eliminates this because the field
names act as length-and-context-bearing delimiters that cannot be
embedded in the field values without changing the resulting JSON
structure.

**Why chain_seq instead of id?** Postgres assigns serial defaults
*before* BEFORE INSERT triggers fire. Two concurrent transactions can
therefore grab id=10 and id=11 in either order, but commit them in
either order. The advisory lock at the start of the trigger
serialises hash chaining, but the lookup for "previous row" was
ordered by ``id`` — under the racy id-assignment scenario, the lookup
for id=11 might miss id=10 entirely (if the id=11 transaction took the
lock first) or vice versa, producing a chain whose ``prev_hash``
pointers don't form a contiguous line. ``chain_seq`` is assigned from
a dedicated sequence **after** taking the lock, guaranteeing strict
monotonicity in lock-acquisition order.

Concurrency: the insert-side trigger takes an advisory lock on the
audit table name so hash chaining is serialised. This caps peak insert
throughput but is usually acceptable for compliance-driven deployments.
If you cannot afford serialisation, skip chaining and run
:func:`verify_chain` only against periodic snapshots.

Caveats:

* Row hashes detect tampering but do not prevent it. Pair with
  :func:`auditrum.hardening.generate_revoke_sql` to make the
  application role unable to write to ``auditlog`` directly in the
  first place.
* Deletion of an *interior* row IS detected: each row's hash binds the
  previous surviving row's ``chain_seq`` (``prev_chain_seq``), so removing
  a mid-chain row changes its successor's previous-surviving chain_seq and
  the successor's recomputed hash no longer matches its stored value. The
  chain is keyless (no HMAC), so an attacker with direct write access could
  still delete a row *and* recompute every following hash — the append-only
  revoke hardening above is what actually prevents that; ``prev_chain_seq``
  raises the bar for anyone who only edits/deletes without recomputing.
* :func:`verify_chain` still cannot detect deletion of the *last* rows in
  the chain by itself — there is no neighbour after the gap to
  mismatch against. Use ``expected_tip=`` together with
  :func:`get_chain_tip` snapshots stored externally.
* ``pgcrypto`` extension must be available.
"""

from typing import Any

from auditrum.tracking.spec import validate_identifier

__all__ = [
    "generate_hash_chain_sql",
    "get_chain_tip",
    "verify_chain",
]

# The shared SQL fragment that produces the canonical bytes hashed by both
# the insert trigger and the server-side verify query. Keeping this in one
# template guarantees that the two SQL paths cannot drift — they format the
# *same* string with context-specific substitutions only.
#
# Substitutions:
#   ``{col}``      — column-reference prefix: ``NEW.`` in the trigger,
#                    empty in the verify query (bare columns from the SELECT).
#   ``{prev}``     — previous row's row_hash: ``last_hash`` (trigger) or the
#                    ``expected_prev`` LAG output (verify).
#   ``{prev_seq}`` — previous SURVIVING row's chain_seq: ``last_chain_seq``
#                    (trigger) or the ``expected_prev_chain_seq`` LAG output
#                    (verify). Binding this into the hash makes interior
#                    deletion detectable: removing a mid-chain row changes
#                    the successor's previous-surviving chain_seq, so its
#                    recomputed hash no longer matches the stored one.
#
# The attribution columns (``user_id``, ``object_id``, ``diff``,
# ``context_id``, ``meta``) are hashed too, so WHO/why cannot be rewritten
# without breaking verification. The row's own ``chain_seq`` stays out of
# the payload (it's chain-ordering metadata, not audited content); only the
# *previous* surviving row's chain_seq is bound, via ``{prev_seq}``.
_CANONICAL_PAYLOAD_EXPR = """jsonb_build_object(
            'id', {col}id,
            'changed_at', {col}changed_at,
            'operation', {col}operation,
            'table_name', {col}table_name,
            'old_data', {col}old_data,
            'new_data', {col}new_data,
            'user_id', {col}user_id,
            'object_id', {col}object_id,
            'diff', {col}diff,
            'context_id', {col}context_id,
            'meta', {col}meta,
            'prev_hash', {prev},
            'prev_chain_seq', {prev_seq}
        )::text"""


def generate_hash_chain_sql(table_name: str = "auditlog") -> str:
    """Generate SQL to enable append-side hash chaining on an existing audit log.

    Adds three columns (``row_hash``, ``prev_hash``, ``chain_seq``), a
    dedicated sequence (``<table>_chain_seq``), a SECURITY DEFINER
    BEFORE INSERT trigger that assigns ``chain_seq`` from the sequence
    inside an advisory lock and computes the canonical-JSON SHA-256.
    Idempotent — uses ``IF NOT EXISTS`` and ``CREATE OR REPLACE``
    everywhere, so re-running on an existing chained log is safe.
    """
    validate_identifier(table_name, "table_name")
    fn_name = f"{table_name}_hash_chain_trigger"
    trig_name = f"{table_name}_hash_chain"
    seq_name = f"{table_name}_chain_seq"

    # In the trigger context the column references are NEW.<col>, the
    # previous-hash variable is `last_hash`, and the previous surviving
    # row's chain_seq is `last_chain_seq`. Built from the shared template
    # so trigger and verify produce byte-identical canonical JSON.
    trigger_payload = _CANONICAL_PAYLOAD_EXPR.format(
        col="NEW.", prev="last_hash", prev_seq="last_chain_seq"
    )

    return f"""
CREATE EXTENSION IF NOT EXISTS pgcrypto;

ALTER TABLE {table_name} ADD COLUMN IF NOT EXISTS row_hash text;
ALTER TABLE {table_name} ADD COLUMN IF NOT EXISTS prev_hash text;
ALTER TABLE {table_name} ADD COLUMN IF NOT EXISTS chain_seq bigint;

CREATE SEQUENCE IF NOT EXISTS {seq_name};

CREATE OR REPLACE FUNCTION {fn_name}() RETURNS trigger AS $$
DECLARE
    last_hash text;
    last_chain_seq bigint;
    payload text;
BEGIN
    -- Serialise concurrent inserts so chain_seq matches lock-acquisition
    -- order. hashtextextended gives a 64-bit lock key (collision-safe
    -- vs. other advisory-lock users in the same database).
    PERFORM pg_advisory_xact_lock(hashtextextended('{table_name}', 0));

    -- Assign chain_seq INSIDE the lock. Sequences are not transactional,
    -- so concurrent inserts can never grab the same value, and the
    -- lock-then-nextval order guarantees later transactions get strictly
    -- larger chain_seq than earlier ones.
    NEW.chain_seq := nextval('{seq_name}');

    -- Look up the previous row by chain_seq order. Falls through to
    -- legacy rows that pre-date the chain_seq column (chain_seq IS NULL,
    -- ordered by id) so chain continuity is preserved across the
    -- migration boundary.
    --
    -- Ordering picks the *most recent* row to chain to:
    -- chain_seq DESC (newest first) with NULLs sorted last, then id
    -- DESC as the tiebreaker for legacy rows. LIMIT 1 takes the head.
    -- Capture the predecessor's chain_seq too; binding it into this row's
    -- hash is what makes an interior deletion detectable (the deleted
    -- row's chain_seq is recorded in its successor's hash).
    SELECT row_hash, chain_seq INTO last_hash, last_chain_seq
    FROM {table_name}
    WHERE row_hash IS NOT NULL
      AND (
          (chain_seq IS NOT NULL AND chain_seq < NEW.chain_seq)
          OR chain_seq IS NULL
      )
    ORDER BY chain_seq DESC NULLS LAST, id DESC
    LIMIT 1;

    NEW.prev_hash := last_hash;
    payload := {trigger_payload};
    NEW.row_hash := encode(digest(payload, 'sha256'), 'hex');
    RETURN NEW;
END;
$$ LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, public;

DROP TRIGGER IF EXISTS {trig_name} ON {table_name};
CREATE TRIGGER {trig_name}
BEFORE INSERT ON {table_name}
FOR EACH ROW EXECUTE FUNCTION {fn_name}();
""".strip()


def get_chain_tip(conn: Any, table_name: str = "auditlog") -> dict[str, Any]:
    """Return the current tip of the hash chain.

    ``conn`` accepts a psycopg ``Connection`` OR a Django connection —
    anything exposing ``.cursor()`` that returns a PEP-249 cursor.

    Returns a dict with ``id``, ``chain_seq``, ``row_hash``, and
    ``changed_at`` of the most recent row in chain-order. Used together
    with :func:`verify_chain`'s ``expected_tip`` parameter to detect
    tail-row deletion: capture the tip periodically (cron), store it in
    a tamper-evident external location (S3 with Object Lock, a separate
    WORM database, paper printout, etc.), and pass it back to
    ``verify_chain`` on the next verification run.

    Returns ``None`` for all fields if the chain is empty.

    Tip ordering: prefers ``chain_seq`` for new rows; falls back to
    ``id`` for legacy rows that pre-date the chain_seq column.
    """
    validate_identifier(table_name, "table_name")
    from psycopg import sql

    query = sql.SQL(
        "SELECT id, chain_seq, row_hash, changed_at FROM {tbl} "
        "WHERE row_hash IS NOT NULL "
        "ORDER BY chain_seq DESC NULLS LAST, id DESC LIMIT 1"
    ).format(tbl=sql.Identifier(table_name))

    with conn.cursor() as cur:
        cur.execute(query)
        row = cur.fetchone()

    if row is None:
        return {
            "id": None,
            "chain_seq": None,
            "row_hash": None,
            "changed_at": None,
        }
    return {
        "id": row[0],
        "chain_seq": row[1],
        "row_hash": row[2],
        "changed_at": row[3],
    }


def verify_chain(
    conn: Any,
    table_name: str = "auditlog",
    *,
    expected_tip: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Verify the hash chain on the audit log using server-side recomputation.

    ``conn`` accepts a psycopg ``Connection`` OR a Django connection —
    anything exposing ``.cursor()`` that returns a PEP-249 cursor.

    The check runs entirely in PostgreSQL to match the trigger's hashing
    behaviour exactly (canonical JSON encoding, timestamp formatting, etc.).
    It recomputes the expected ``row_hash`` for each row using a window
    function and returns the IDs of any rows whose stored hash does not
    match the expected value, plus rows whose ``prev_hash`` does not match
    the previous row's ``row_hash``.

    Rows are walked in chain order (``chain_seq NULLS FIRST, id``) so
    legacy rows from before the chain_seq migration sort first by id and
    new rows sort after them by chain_seq.

    **Tail-row deletion detection.** The window-function check alone
    cannot detect deletion of the most recent rows (there is no
    neighbour after the gap to mismatch against). To close that gap,
    pass ``expected_tip`` — a dict produced by an earlier call to
    :func:`get_chain_tip` and stored in a tamper-evident location. The
    function then verifies that the anchor row is still present and
    unmodified.

    Returns a dict with ``checked``, ``ok``, ``broken`` keys. ``broken`` is a
    list of ``(id, reason)`` pairs.
    """
    validate_identifier(table_name, "table_name")
    from psycopg import sql

    # In the verify context the column references are bare (the SELECT
    # comes straight from the audit table), the previous-hash variable is
    # the LAG(row_hash) window output `expected_prev`, and the previous
    # surviving row's chain_seq is the LAG(chain_seq) output
    # `expected_prev_chain_seq`. Built from the same template as the
    # trigger so the canonical JSON is byte-identical.
    verify_payload = _CANONICAL_PAYLOAD_EXPR.format(
        col="", prev="expected_prev", prev_seq="expected_prev_chain_seq"
    )

    query = sql.SQL(
        """
        WITH ordered AS (
            SELECT
                id, changed_at, operation, table_name, old_data, new_data,
                user_id, object_id, diff, context_id, meta,
                row_hash, prev_hash, chain_seq,
                LAG(row_hash) OVER (ORDER BY chain_seq NULLS FIRST, id)
                    AS expected_prev,
                LAG(chain_seq) OVER (ORDER BY chain_seq NULLS FIRST, id)
                    AS expected_prev_chain_seq
            FROM {tbl}
            WHERE row_hash IS NOT NULL
        )
        SELECT
            id,
            row_hash,
            encode(digest("""
        + verify_payload
        + """, 'sha256'), 'hex') AS expected_hash,
            expected_prev,
            prev_hash
        FROM ordered
        ORDER BY chain_seq NULLS FIRST, id
        """
    ).format(tbl=sql.Identifier(table_name))

    broken: list[tuple[int | None, str]] = []
    checked = 0
    with conn.cursor() as cur:
        cur.execute(query)
        for row_id, row_hash, expected_hash, expected_prev, prev_hash in cur:
            checked += 1
            if (expected_prev or None) != (prev_hash or None):
                broken.append((row_id, "prev_hash mismatch"))
                continue
            if expected_hash != row_hash:
                broken.append((row_id, "row_hash mismatch"))

    if expected_tip is not None:
        anchor_id = expected_tip.get("id")
        anchor_hash = expected_tip.get("row_hash")
        if anchor_id is not None and anchor_hash is not None:
            anchor_check = sql.SQL("SELECT row_hash FROM {tbl} WHERE id = %s").format(
                tbl=sql.Identifier(table_name)
            )
            with conn.cursor() as cur:
                cur.execute(anchor_check, (anchor_id,))
                row = cur.fetchone()
            if row is None:
                broken.append((anchor_id, "tip row missing — chain truncated"))
            elif row[0] != anchor_hash:
                broken.append((anchor_id, "tip row_hash mismatch — chain rewritten"))

            # Also verify that no rows have been deleted between the
            # last surviving row and the anchor.
            max_query = sql.SQL("SELECT COALESCE(MAX(id), 0) FROM {tbl}").format(
                tbl=sql.Identifier(table_name)
            )
            with conn.cursor() as cur:
                cur.execute(max_query)
                actual_max = cur.fetchone()[0]
            if actual_max < anchor_id:
                broken.append((anchor_id, f"tip id missing — actual max id is {actual_max}"))

    return {"checked": checked, "ok": len(broken) == 0, "broken": broken}
