"""SQLAlchemy integration for auditrum.

Thin bridge on top of the framework-agnostic :mod:`auditrum.tracking`
core. Exposes:

* :class:`SQLAlchemyExecutor` — a :class:`ConnectionExecutor` that wraps
  a ``sqlalchemy.engine.Connection`` so :class:`TriggerManager` can
  install / uninstall triggers against a SQLAlchemy-managed database
* :func:`track_table` — declarative helper that builds a
  :class:`TrackSpec` from a SQLAlchemy :class:`~sqlalchemy.Table`
* :func:`registered_specs` — introspection of the registry
* :func:`sync` — one-line sync of all registered specs
* :func:`bootstrap_schema` — install the audit log + context table +
  helper functions against an engine (idempotent)

Example::

    from sqlalchemy import create_engine, Table, Column, Integer, String, MetaData
    from auditrum.integrations.sqlalchemy import track_table, sync, bootstrap_schema

    meta = MetaData()
    users = Table(
        "users", meta,
        Column("id", Integer, primary_key=True),
        Column("email", String),
    )
    track_table(users, fields=["email"])

    engine = create_engine("postgresql+psycopg://user:pw@/db")
    meta.create_all(engine)
    bootstrap_schema(engine)
    sync(engine)

Pair this with Alembic's ``env.py`` ``run_migrations_online`` hook if
you want audit triggers to be part of your migration flow.
"""

from auditrum.integrations.sqlalchemy.core import (
    SQLAlchemyExecutor,
    bootstrap_schema,
    clear_registry,
    registered_specs,
    sync,
    track_table,
)

__all__ = [
    "SQLAlchemyExecutor",
    "track_table",
    "sync",
    "bootstrap_schema",
    "registered_specs",
    "clear_registry",
]
