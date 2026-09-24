"""System checks for the Django integration.

``auditrum.W001`` fires when the ``@track`` registry and the trigger
migrations disagree — e.g. ``exclude=[...]`` was edited but
``auditrum_makemigrations`` was not run. Django's ``makemigrations`` cannot
see such changes, so without this check ``migrate`` would silently keep
the old trigger body in the database.
"""

from __future__ import annotations

from typing import Any

from django.core.checks import CheckMessage, Warning, register


@register()
def check_trigger_migrations(app_configs: Any = None, **kwargs: Any) -> list[CheckMessage]:
    from auditrum.integrations.django.autodetector import detect_changes

    try:
        changes = detect_changes()
    except Exception as exc:  # a broken migration must not take runserver down
        return [
            Warning(
                f"auditrum could not compare @track specs with trigger migrations: {exc}",
                id="auditrum.W002",
            )
        ]

    names = sorted(c.trigger_name for app in changes.values() for c in app)
    if not names:
        return []
    return [
        Warning(
            f"Audit trigger specs have changes not reflected in migrations: {', '.join(names)}.",
            hint="Run 'python manage.py auditrum_makemigrations' and then 'migrate'.",
            id="auditrum.W001",
        )
    ]
