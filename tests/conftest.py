import pytest


@pytest.fixture(autouse=True)
def _clean_audit_context():
    """Reset AuditContext ContextVars between tests to avoid cross-test leakage."""
    from auditrum.context import audit_context

    audit_context._data.set({})
    audit_context._reason_stack.set([])
    yield
    audit_context._data.set({})
    audit_context._reason_stack.set([])
