from unittest.mock import MagicMock

import pytest

from auditrum.compaction import compact_snapshots


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
