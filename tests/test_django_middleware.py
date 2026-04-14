"""Unit tests for AuditrumMiddleware — PII handling, context shape, hashing."""

from unittest.mock import MagicMock

import pytest

django = pytest.importorskip("django")

from django.conf import settings as django_settings  # noqa: E402

if not django_settings.configured:
    django_settings.configure(
        SECRET_KEY="test-secret-key-for-hashing",
        INSTALLED_APPS=[
            "django.contrib.contenttypes",
            "django.contrib.auth",
            "django.contrib.admin",
            "django.contrib.sessions",
            "django.contrib.messages",
            "auditrum.integrations.django",
        ],
        DATABASES={"default": {"ENGINE": "django.db.backends.sqlite3", "NAME": ":memory:"}},
        ROOT_URLCONF="django.contrib.contenttypes.urls",
        TEMPLATES=[
            {
                "BACKEND": "django.template.backends.django.DjangoTemplates",
                "DIRS": [],
                "APP_DIRS": True,
                "OPTIONS": {"context_processors": []},
            }
        ],
    )
    django.setup()

from auditrum.integrations.django.middleware import (  # noqa: E402
    AuditrumMiddleware,
    _hash_session_key,
)


@pytest.fixture(autouse=True)
def _ensure_secret_key():
    """Some other test files configure Django without a SECRET_KEY. The
    middleware tests need one for the HMAC. We bypass monkeypatch here
    because reading the original via getattr triggers ImproperlyConfigured."""
    import contextlib

    sentinel = object()
    original = django_settings._wrapped.__dict__.get("SECRET_KEY", sentinel)
    django_settings._wrapped.SECRET_KEY = "test-secret-key-for-hashing"
    yield
    if original is sentinel:
        with contextlib.suppress(AttributeError):
            delattr(django_settings._wrapped, "SECRET_KEY")
    else:
        django_settings._wrapped.SECRET_KEY = original


def _fake_request(*, session_key=None, user_id=42, ua="Mozilla/5.0", method="POST"):
    request = MagicMock()
    request.method = method
    request.path = "/api/orders"
    request.META = {"REMOTE_ADDR": "1.2.3.4", "HTTP_USER_AGENT": ua}
    request.user = MagicMock()
    request.user.id = user_id
    request.user.username = "alice"
    request.session = MagicMock()
    request.session.session_key = session_key
    request.request_id = None
    return request


class TestHashSessionKey:
    def test_returns_hex_string(self):
        result = _hash_session_key("abc123")
        assert result is not None
        assert len(result) == 16
        assert all(c in "0123456789abcdef" for c in result)

    def test_deterministic(self):
        a = _hash_session_key("abc123")
        b = _hash_session_key("abc123")
        assert a == b

    def test_different_inputs_different_hashes(self):
        a = _hash_session_key("session_a")
        b = _hash_session_key("session_b")
        assert a != b

    def test_none_returns_none(self):
        assert _hash_session_key(None) is None

    def test_empty_string_returns_none(self):
        assert _hash_session_key("") is None

    def test_does_not_leak_original_value(self):
        """The original session token must not appear anywhere in the hash."""
        token = "secret_session_token_xyz_42"
        result = _hash_session_key(token)
        assert token not in result
        assert "secret" not in result


class TestMiddlewareGetContext:
    def test_session_key_is_hashed_by_default(self):
        mw = AuditrumMiddleware(get_response=lambda r: None)
        request = _fake_request(session_key="raw_session_token_xyz")
        ctx = mw.get_context(request)
        # Hashed value must be present and must not equal the raw token
        assert ctx["session_key"] is not None
        assert ctx["session_key"] != "raw_session_token_xyz"
        assert "raw_session_token" not in str(ctx["session_key"])

    def test_session_key_is_short_correlation_id(self):
        mw = AuditrumMiddleware(get_response=lambda r: None)
        ctx = mw.get_context(_fake_request(session_key="xyz"))
        assert len(ctx["session_key"]) == 16

    def test_session_key_none_for_anonymous(self):
        mw = AuditrumMiddleware(get_response=lambda r: None)
        ctx = mw.get_context(_fake_request(session_key=None))
        assert ctx["session_key"] is None

    def test_hash_disabled_via_setting(self, monkeypatch):
        monkeypatch.setattr(
            django_settings, "PGAUDIT_HASH_SESSION_KEY", False, raising=False
        )
        mw = AuditrumMiddleware(get_response=lambda r: None)
        ctx = mw.get_context(_fake_request(session_key="raw_token"))
        assert ctx["session_key"] == "raw_token"

    def test_user_agent_present_by_default(self):
        mw = AuditrumMiddleware(get_response=lambda r: None)
        ctx = mw.get_context(_fake_request(ua="Mozilla/Test"))
        assert ctx["user_agent"] == "Mozilla/Test"

    def test_user_agent_redacted_via_setting(self, monkeypatch):
        monkeypatch.setattr(
            django_settings, "PGAUDIT_REDACT_USER_AGENT", True, raising=False
        )
        mw = AuditrumMiddleware(get_response=lambda r: None)
        ctx = mw.get_context(_fake_request(ua="Mozilla/Test"))
        assert "user_agent" not in ctx

    def test_request_id_assigned_when_missing(self):
        mw = AuditrumMiddleware(get_response=lambda r: None)
        request = _fake_request()
        assert request.request_id is None
        ctx = mw.get_context(request)
        assert ctx["request_id"] is not None
        assert request.request_id == ctx["request_id"]

    def test_full_context_shape(self):
        mw = AuditrumMiddleware(get_response=lambda r: None)
        ctx = mw.get_context(_fake_request())
        assert ctx["user_id"] == 42
        assert ctx["username"] == "alice"
        assert ctx["client_ip"] == "1.2.3.4"
        assert ctx["url"] == "/api/orders"
        assert ctx["method"] == "POST"
        assert ctx["source"] == "http"
        assert "session_key" in ctx
        assert "request_id" in ctx
        assert "user_agent" in ctx
