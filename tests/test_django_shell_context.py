"""Coverage for the Django shell context helper.

``auditrum.integrations.django.shell_context`` executes
``audit_tracked(source="shell").__enter__()`` at import time so that
``manage.py shell`` sessions automatically get a populated audit
context. The module is tiny and has no public surface to test
directly; importing it is the test.
"""

from unittest.mock import MagicMock

import pytest

django = pytest.importorskip("django")

from django.conf import settings as django_settings  # noqa: E402

if not django_settings.configured:
    django_settings.configure(
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
    )
    django.setup()

from auditrum.context import audit_context  # noqa: E402


class TestShellContextImport:
    def test_import_installs_shell_source(self):
        # Make sure the module hasn't been imported by an earlier test —
        # we need the top-level statement to fire in this test's process
        # to cover the import-time code path. If another test imported
        # it first, reimport is a no-op and coverage is attributed to
        # that other test. Either way the shell source ends up on the
        # audit context.
        rec = MagicMock()

        @staticmethod
        def _cursor():
            class _CM:
                def __enter__(_self):
                    return rec

                def __exit__(_self, *a):
                    return False

            return _CM()

        rec_executor = MagicMock()
        rec_executor.cursor = _cursor
        prev_executor = audit_context.get_executor()
        audit_context.set_executor(rec_executor)
        try:
            import auditrum.integrations.django.shell_context  # noqa: F401
        finally:
            audit_context.set_executor(prev_executor)

        assert audit_context.get("source") == "shell"
