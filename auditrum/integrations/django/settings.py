import re

from django.conf import settings

__all__ = [
    "AuditSettings",
    "audit_settings",
]

# Postgres custom GUCs are required to have the form `<class>.<name>` where
# both halves are simple identifiers. We validate user-supplied GUC names
# against this regex before they are interpolated into PL/pgSQL function
# bodies — defence in depth even though the trust boundary is Django settings.
_GUC_NAME_RE = re.compile(r"^[a-z_][a-z0-9_]*\.[a-z_][a-z0-9_]*$")


def _validate_guc_name(value: str, label: str) -> str:
    if not isinstance(value, str) or not _GUC_NAME_RE.match(value):
        raise ValueError(f"Invalid {label}: {value!r} (must match {_GUC_NAME_RE.pattern})")
    return value


class AuditSettings:
    @property
    def table_name(self) -> str:
        return getattr(settings, "PGAUDIT_TABLE_NAME", "auditlog")

    @property
    def context_table_name(self) -> str:
        return getattr(settings, "PGAUDIT_CONTEXT_TABLE_NAME", "audit_context")

    @property
    def enabled(self) -> bool:
        return getattr(settings, "PGAUDIT_ENABLED", True)

    @property
    def guc_id(self) -> str:
        value = getattr(settings, "PGAUDIT_GUC_ID", "auditrum.context_id")
        return _validate_guc_name(value, "PGAUDIT_GUC_ID")

    @property
    def guc_metadata(self) -> str:
        value = getattr(settings, "PGAUDIT_GUC_METADATA", "auditrum.context_metadata")
        return _validate_guc_name(value, "PGAUDIT_GUC_METADATA")

    @property
    def middleware_methods(self) -> tuple:
        return tuple(
            getattr(
                settings, "PGAUDIT_MIDDLEWARE_METHODS", ("GET", "POST", "PUT", "PATCH", "DELETE")
            )
        )

    @property
    def hash_session_key(self) -> bool:
        """Whether to hash session_key before storing it in audit_context.

        Default ``True``: storing the raw session token forever in the
        audit log is a credential-disclosure risk (PCI-DSS, GDPR Art.32,
        SOC2). Hashing with the project ``SECRET_KEY`` lets you still
        correlate events back to a session without persisting the
        bearer token. Set to ``False`` only if you have a hard
        compliance reason to keep the raw value.
        """
        return getattr(settings, "PGAUDIT_HASH_SESSION_KEY", True)

    @property
    def hash_chain(self) -> bool:
        """Whether the tamper-evidence hash chain is in use on the audit log.

        Default ``False``. This flag is advisory — it documents intent and
        is consulted by the ``auditrum_enable_hash_chain`` command (which
        warns if you enable the DDL while the flag is off). It does **not**
        auto-run any DDL: the chain adds write-serialisation overhead (a
        per-table advisory lock on insert), so enabling it is an explicit
        operator action via ``python manage.py auditrum_enable_hash_chain``.
        """
        return getattr(settings, "PGAUDIT_HASH_CHAIN", False)

    @property
    def diff_gin_index(self) -> bool:
        """Whether to create a GIN index on the audit log ``diff`` column.

        Default ``False`` (issue #6): production measurements on two
        independent deployments found this index gets 0 scans yet costs
        700-860 MB per monthly partition and is re-maintained on every
        audited write. It only pays off if the app runs ``WHERE diff @>
        '{...}'`` jsonb-containment queries against the audit log. Set to
        ``True`` only if you rely on such queries.
        """
        return getattr(settings, "PGAUDIT_DIFF_GIN_INDEX", False)

    @property
    def redact_user_agent(self) -> bool:
        """Whether to drop user_agent from audit_context metadata.

        Default ``False`` — most projects find user_agent useful for
        debugging. Set to ``True`` under strict GDPR / PII regimes
        where browser fingerprints count as personal data.
        """
        return getattr(settings, "PGAUDIT_REDACT_USER_AGENT", False)


audit_settings = AuditSettings()
