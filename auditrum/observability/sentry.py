"""Sentry breadcrumb helpers for audit context.

Adds a Sentry breadcrumb whenever an audit context is entered, so
errors captured downstream include the audit metadata (user, source,
request_id, change_reason) as contextual trail. No-op without
``sentry_sdk``.
"""

from __future__ import annotations

import contextlib
from typing import Any

__all__ = ["add_breadcrumb_for_context"]


def add_breadcrumb_for_context(
    metadata: dict[str, Any], *, category: str = "auditrum"
) -> None:
    """Add a breadcrumb to the current Sentry scope.

    Silent no-op if ``sentry_sdk`` is not installed. The metadata is
    copied into the breadcrumb ``data`` payload verbatim — filter it
    yourself before calling if it contains PII you don't want in Sentry.
    """
    try:
        import sentry_sdk
    except ImportError:
        return

    # Never let observability break the app
    with contextlib.suppress(Exception):
        sentry_sdk.add_breadcrumb(
            category=category,
            message=f"audit context {metadata.get('source', '?')}",
            level="info",
            data=dict(metadata),
        )
