from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

__all__ = ["DjangoExecutor"]


class DjangoExecutor:
    """ConnectionExecutor backed by a Django database wrapper.

    Two usage modes:

    * **Default** — instantiate with no arguments. Each ``cursor()`` call
      fetches the current ``django.db.connection`` lazily, so the executor
      respects whatever connection alias is active in the current request.
      This is what :class:`PgAuditIntegrationConfig.ready` installs into
      ``auditrum.context.audit_context`` so middleware and the legacy
      ``audit_tracked`` helper just work.

    * **Explicit connection** — pass a Django ``DatabaseWrapper`` (e.g.
      ``schema_editor.connection``) for use inside migration operations
      that need to target a specific alias rather than the default. This
      is the path used by :class:`InstallTrigger` /
      :class:`UninstallTrigger`.

    The executor delegates to ``connection.cursor()`` directly rather
    than reaching into ``connection.connection`` (the underlying psycopg
    handle), so query routing, multi-DB setups, and connection wrappers
    installed by Django middleware all keep working.
    """

    def __init__(self, connection: Any | None = None) -> None:
        self._connection = connection

    @contextmanager
    def cursor(self) -> Iterator[Any]:
        if self._connection is None:
            from django.db import connection as default_connection

            with default_connection.cursor() as cur:
                yield cur
        else:
            with self._connection.cursor() as cur:
                yield cur
