from unittest.mock import MagicMock

import pytest

from auditrum.compaction import _has_row_hash, compact_snapshots


@pytest.mark.parametrize(
    "kwargs",
    [
        {"table": "t", "batch_size": 0},
        {"table": "bad name"},
        {"table": "t", "audit_table": "x;y"},
    ],
)
def test_invalid_arguments_raise_before_touching_the_connection(kwargs):
    conn = MagicMock()
    with pytest.raises(ValueError):
        compact_snapshots(conn, **kwargs)
    conn.cursor.assert_not_called()


def test_row_hash_lookup_quotes_the_audit_table():
    cur = MagicMock()
    cur.fetchone.return_value = None
    assert _has_row_hash(cur, "AuditLog") is False
    query, params = cur.execute.call_args.args
    assert "quote_ident(%s)::regclass" in query and params == ("AuditLog",)
