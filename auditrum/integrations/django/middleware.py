import uuid

from django.utils.deprecation import MiddlewareMixin

from auditrum.context import audit_context as ctx


class RequestIDMiddleware(MiddlewareMixin):
    def process_request(self, request):
        request.request_id = str(uuid.uuid4())
        ctx.set("request_id", request.request_id)


class AuditrumMiddleware(MiddlewareMixin):
    def process_request(self, request):
        user = getattr(request, "user", None)
        session = getattr(request, "session", None)

        ctx.set("user_id", getattr(user, "id", None))
        ctx.set("username", getattr(user, "username", None))
        ctx.set("client_ip", request.META.get("REMOTE_ADDR"))
        ctx.set("user_agent", request.META.get("HTTP_USER_AGENT"))
        ctx.set("session_key", getattr(session, "session_key", None))
        ctx.set("request_id", str(uuid.uuid4()))
        ctx.set("source", "http")
