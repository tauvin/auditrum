"""Unit tests for the Celery/RQ task-context helpers."""

import asyncio
import sys
import types

import pytest

django = pytest.importorskip("django")

from django.conf import settings as django_settings  # noqa: E402

if not django_settings.configured:
    django_settings.configure(
        SECRET_KEY="test-secret-key",
        INSTALLED_APPS=[
            "django.contrib.contenttypes",
            "django.contrib.auth",
            "django.contrib.admin",
            "django.contrib.sessions",
            "django.contrib.messages",
            "auditrum.integrations.django",
        ],
        DATABASES={
            "default": {"ENGINE": "django.db.backends.sqlite3", "NAME": ":memory:"}
        },
        ROOT_URLCONF="django.contrib.contenttypes.urls",
        TEMPLATES=[
            {
                "BACKEND": "django.template.backends.django.DjangoTemplates",
                "DIRS": [],
                "APP_DIRS": True,
                "OPTIONS": {"context_processors": []},
            }
        ],
        USE_TZ=True,
        TIME_ZONE="UTC",
    )
    django.setup()

from auditrum.integrations.django.runtime import current_context  # noqa: E402
from auditrum.integrations.django.tasks import audit_task  # noqa: E402


class TestAuditTaskDecorator:
    def test_sync_task_gets_context(self):
        @audit_task(source="celery", queue="default")
        def task():
            ctx = current_context()
            assert ctx is not None
            return dict(ctx.metadata)

        result = task()
        assert result["source"] == "celery"
        assert result["queue"] == "default"

    def test_context_is_popped_after_return(self):
        @audit_task(source="rq")
        def task():
            return None

        assert current_context() is None
        task()
        assert current_context() is None

    def test_async_task_keeps_context_across_await(self):
        @audit_task(source="dramatiq", priority="high")
        async def task():
            await asyncio.sleep(0)
            ctx = current_context()
            assert ctx is not None
            return dict(ctx.metadata)

        result = asyncio.run(task())
        assert result["source"] == "dramatiq"
        assert result["priority"] == "high"

    def test_async_wrapper_is_coroutine_function(self):
        @audit_task(source="x")
        async def task():
            return 1

        assert asyncio.iscoroutinefunction(task)

    def test_raises_from_inner_function_still_pops_context(self):
        @audit_task(source="celery")
        def task():
            raise ValueError("boom")

        with pytest.raises(ValueError, match="boom"):
            task()
        assert current_context() is None


class _FakeSignal:
    """Minimal stand-in for ``celery.Signal`` — collects ``.connect``
    handlers and replays them on ``.send``. Lets us drive the signal
    lifecycle without requiring a real Celery install in the test
    environment."""

    def __init__(self):
        self.handlers: list = []

    def connect(self, handler, weak=False):
        self.handlers.append(handler)
        return handler

    def send(self, **kwargs):
        for h in self.handlers:
            h(**kwargs)


@pytest.fixture
def fake_celery_signals(monkeypatch):
    signals_mod = types.ModuleType("celery.signals")
    signals_mod.task_prerun = _FakeSignal()
    signals_mod.task_postrun = _FakeSignal()
    signals_mod.task_failure = _FakeSignal()

    celery_mod = types.ModuleType("celery")

    monkeypatch.setitem(sys.modules, "celery", celery_mod)
    monkeypatch.setitem(sys.modules, "celery.signals", signals_mod)
    return signals_mod


class TestInstallCelerySignals:
    def test_prerun_pushes_context(self, fake_celery_signals):
        from auditrum.integrations.django.tasks import install_celery_signals

        install_celery_signals(source="celery-app")

        class FakeTask:
            name = "myapp.tasks.send_email"

        fake_celery_signals.task_prerun.send(
            task_id="tid-001", task=FakeTask(), sender=FakeTask
        )
        try:
            ctx = current_context()
            assert ctx is not None
            assert ctx.metadata["source"] == "celery-app"
            assert ctx.metadata["task_name"] == "myapp.tasks.send_email"
            assert ctx.metadata["task_id"] == "tid-001"
        finally:
            fake_celery_signals.task_postrun.send(
                task_id="tid-001", sender=FakeTask
            )

    def test_postrun_pops_context(self, fake_celery_signals):
        from auditrum.integrations.django.tasks import install_celery_signals

        install_celery_signals()

        class FakeTask:
            name = "t"

        fake_celery_signals.task_prerun.send(
            task_id="tid-002", task=FakeTask(), sender=FakeTask
        )
        assert current_context() is not None

        fake_celery_signals.task_postrun.send(
            task_id="tid-002", sender=FakeTask
        )
        assert current_context() is None

    def test_missing_task_id_is_ignored(self, fake_celery_signals):
        # If Celery ever dispatches without a task_id (shouldn't happen
        # per the signal contract, but cheap insurance), we skip the
        # prerun wrap instead of raising.
        from auditrum.integrations.django.tasks import install_celery_signals

        install_celery_signals()

        class FakeTask:
            name = "t"

        fake_celery_signals.task_prerun.send(task_id=None, task=FakeTask())
        assert current_context() is None

    def test_raises_without_celery_installed(self, monkeypatch):
        # Simulate the "Celery not installed" path by forcing the import
        # to fail. importlib-level isolation via monkeypatched sys.modules.
        import importlib

        monkeypatch.setitem(sys.modules, "celery", None)
        monkeypatch.setitem(sys.modules, "celery.signals", None)

        # Ensure the module is re-imported so the inner import fires.
        import auditrum.integrations.django.tasks as tasks_mod

        importlib.reload(tasks_mod)

        with pytest.raises((ImportError, TypeError)):
            tasks_mod.install_celery_signals()
