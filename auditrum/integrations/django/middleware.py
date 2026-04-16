import hashlib
import hmac
import uuid
from typing import Any

from django.conf import settings as django_settings

from auditrum.integrations.django.runtime import auditrum_context
from auditrum.integrations.django.settings import audit_settings

__all__ = [
    "AuditrumMiddleware",
    "RequestIDMiddleware",
]


def _hash_session_key(session_key: str | None) -> str | None:
    """One-way HMAC of the session key keyed by the project ``SECRET_KEY``.

    Returns the first 16 hex chars of HMAC-SHA256 — short enough to be
    useful as a correlation id, long enough to be collision-resistant
    over a single deployment's session population. The original session
    token cannot be recovered.

    Returns ``None`` if ``session_key`` is ``None`` (anonymous request)
    or if Django ``SECRET_KEY`` is unavailable.
    """
    if not session_key:
        return None
    try:
        secret = django_settings.SECRET_KEY
    except Exception:
        # ImproperlyConfigured if SECRET_KEY isn't set, or any other lookup
        # error — degrade gracefully rather than crashing the request path.
        return None
    if not secret:
        return None
    secret_bytes = secret.encode("utf-8") if isinstance(secret, str) else bytes(secret)
    digest = hmac.new(secret_bytes, session_key.encode("utf-8"), hashlib.sha256)
    return digest.hexdigest()[:16]


class RequestIDMiddleware:
    """Attach a stable ``request_id`` to the request object."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        if not getattr(request, "request_id", None):
            request.request_id = str(uuid.uuid4())
        return self.get_response(request)


class AuditrumMiddleware:
    """Propagate per-request audit context into PostgreSQL session GUCs.

    For every incoming HTTP request (matching ``PGAUDIT_MIDDLEWARE_METHODS``)
    this middleware enters an :class:`auditrum_context` block. That block
    installs a ``connection.execute_wrapper`` which prepends
    ``SELECT set_config('auditrum.context_id', …, true), set_config(
    'auditrum.context_metadata', …, true); `` to every subsequent SQL
    statement. Audit triggers read these GUCs via ``_audit_attach_context()``
    and write exactly one row into ``audit_context`` per HTTP request that
    actually produces an audit event.

    The ``is_local=true`` semantics mean GUCs are scoped to the statement's
    transaction and cannot leak across requests sharing a pooled connection.

    PII handling:

    * ``session_key`` is hashed by default (``PGAUDIT_HASH_SESSION_KEY``)
      via HMAC-SHA256 keyed by Django's ``SECRET_KEY``. This avoids
      storing the live bearer token in the audit log forever.
    * ``user_agent`` can be dropped via ``PGAUDIT_REDACT_USER_AGENT``
      for strict GDPR setups.
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def get_context(self, request) -> dict[str, Any]:
        user = getattr(request, "user", None)
        session = getattr(request, "session", None)
        request_id = getattr(request, "request_id", None) or str(uuid.uuid4())
        request.request_id = request_id

        raw_session_key = getattr(session, "session_key", None)
        if audit_settings.hash_session_key:
            session_field = _hash_session_key(raw_session_key)
        else:
            session_field = raw_session_key

        ctx: dict[str, Any] = {
            "user_id": getattr(user, "id", None),
            "username": getattr(user, "username", None),
            "client_ip": request.META.get("REMOTE_ADDR"),
            "session_key": session_field,
            "request_id": request_id,
            "url": getattr(request, "path", None),
            "method": getattr(request, "method", None),
            "source": "http",
        }
        if not audit_settings.redact_user_agent:
            ctx["user_agent"] = request.META.get("HTTP_USER_AGENT")
        return ctx

    def __call__(self, request):
        if request.method not in audit_settings.middleware_methods:
            return self.get_response(request)

        with auditrum_context(**self.get_context(request)):
            return self.get_response(request)
