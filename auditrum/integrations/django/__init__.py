"""Django integration for auditrum.

This is an optional integration layer — the framework-agnostic
:mod:`auditrum.tracking` core is the source of truth for all trigger
generation, install, and lifecycle. This module re-exports Django-
specific conveniences so apps can write::

    from auditrum.integrations.django import track, register, AuditLog

    @track(fields=["status", "total"])
    class Order(models.Model):
        ...
"""

default_app_config = "auditrum.integrations.django.apps.PgAuditIntegrationConfig"


def __getattr__(name: str):
    # Lazy re-exports so importing this package does not require Django
    # to be configured yet (avoids AppRegistryNotReady at import time).
    if name == "track":
        from auditrum.integrations.django.tracking import track

        return track
    if name == "register":
        from auditrum.integrations.django.tracking import register

        return register
    if name == "AuditLog":
        from auditrum.integrations.django.models import AuditLog

        return AuditLog
    if name == "AuditContext":
        from auditrum.integrations.django.models import AuditContext

        return AuditContext
    raise AttributeError(name)
