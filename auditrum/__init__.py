"""PostgreSQL audit system with rich contextual logging."""

from auditrum.context import (
    AuditContext,
    audit_context,
    with_change_reason,
    with_context,
)
from auditrum.hardening import generate_grant_admin_sql, generate_revoke_sql
from auditrum.hash_chain import generate_hash_chain_sql, get_chain_tip, verify_chain
from auditrum.retention import drop_old_partitions, generate_purge_sql
from auditrum.revert import generate_revert_sql, generate_revert_sql_from_log
from auditrum.schema import (
    generate_audit_attach_context_sql,
    generate_audit_context_table_sql,
    generate_audit_current_user_id_sql,
    generate_audit_reconstruct_sql,
    generate_auditlog_partitions_sql,
    generate_auditlog_table_sql,
    generate_jsonb_diff_function_sql,
)
from auditrum.timetravel import (
    reconstruct_field_history,
    reconstruct_row,
    reconstruct_table,
)
from auditrum.triggers import generate_trigger_sql
from auditrum.utils import audit_tracked

__all__ = [
    "AuditContext",
    "audit_context",
    "audit_tracked",
    "with_change_reason",
    "with_context",
    "generate_trigger_sql",
    "generate_auditlog_table_sql",
    "generate_auditlog_partitions_sql",
    "generate_audit_context_table_sql",
    "generate_audit_attach_context_sql",
    "generate_audit_current_user_id_sql",
    "generate_audit_reconstruct_sql",
    "generate_jsonb_diff_function_sql",
    "reconstruct_row",
    "reconstruct_table",
    "reconstruct_field_history",
    "generate_revert_sql",
    "generate_revert_sql_from_log",
    "generate_revoke_sql",
    "generate_grant_admin_sql",
    "generate_hash_chain_sql",
    "verify_chain",
    "get_chain_tip",
    "generate_purge_sql",
    "drop_old_partitions",
]
