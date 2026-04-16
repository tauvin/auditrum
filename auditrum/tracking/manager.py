"""Runtime lifecycle for audit triggers.

:class:`TriggerManager` handles install / uninstall / drift detection /
idempotent sync for a collection of :class:`TrackSpec` instances against
a live database connection. Framework-agnostic — takes any
:class:`auditrum.executor.ConnectionExecutor`.

Installed state is tracked in an ``auditrum_applied_triggers`` table (one
row per installed trigger, with the body checksum) so ``sync()`` can
decide whether an existing trigger needs updating or is already current.
Concurrent installs are serialized via a Postgres advisory lock keyed by
``hashtext(trigger_name)`` so two simultaneous deploys don't race.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from auditrum.executor import ConnectionExecutor
from auditrum.tracking.spec import TrackSpec, TriggerBundle, validate_identifier

__all__ = [
    "DiffEntry",
    "SyncReport",
    "TriggerAction",
    "TriggerManager",
    "TriggerStatus",
]


class TriggerStatus(Enum):
    NOT_INSTALLED = "not_installed"
    INSTALLED = "installed"
    DRIFT = "drift"  # present in DB but checksum differs from current spec


class TriggerAction(Enum):
    INSTALL = "install"
    UPDATE = "update"
    UNINSTALL = "uninstall"
    SKIP = "skip"


@dataclass(frozen=True)
class DiffEntry:
    spec: TrackSpec | None  # None for rows present in DB but not in incoming specs
    action: TriggerAction
    status_before: TriggerStatus
    trigger_name: str


@dataclass
class SyncReport:
    installed: list[str] = field(default_factory=list)
    updated: list[str] = field(default_factory=list)
    uninstalled: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)

    @property
    def total_changes(self) -> int:
        return len(self.installed) + len(self.updated) + len(self.uninstalled)


_TRACKING_TABLE_DEFAULT = "auditrum_applied_triggers"


def _tracking_table_ddl(table_name: str) -> str:
    # Defense-in-depth: callers should already have validated, but the
    # identifier reaches an f-string so we enforce the boundary here too.
    validate_identifier(table_name, "table_name")
    return f"""
CREATE TABLE IF NOT EXISTS {table_name} (
    trigger_name text PRIMARY KEY,
    table_name text NOT NULL,
    checksum text NOT NULL,
    applied_at timestamptz NOT NULL DEFAULT now(),
    spec_fingerprint jsonb NOT NULL
);

CREATE INDEX IF NOT EXISTS {table_name}_table_name_idx
    ON {table_name} (table_name);
""".strip()


class TriggerManager:
    """Install, uninstall, and reconcile audit triggers.

    Usage::

        mgr = TriggerManager(executor)
        mgr.bootstrap()              # idempotent: creates tracking table
        mgr.sync([spec1, spec2])     # install/update/skip based on checksums
        mgr.inspect(spec1)           # -> TriggerStatus
        mgr.uninstall(spec1)

    All operations go through the supplied :class:`ConnectionExecutor`, so
    the same code works for psycopg, Django, SQLAlchemy, or anything else
    that exposes a cursor.
    """

    def __init__(
        self,
        executor: ConnectionExecutor,
        *,
        tracking_table: str = _TRACKING_TABLE_DEFAULT,
    ) -> None:
        validate_identifier(tracking_table, "tracking_table")
        self.executor = executor
        self._tracking_table = tracking_table

    @property
    def tracking_table(self) -> str:
        # Read-only so the validated value from __init__ can't be
        # swapped out later and sneak an unchecked identifier into an
        # f-string in _fetch_stored / list_installed / _upsert_tracking.
        return self._tracking_table

    # ------------------------------------------------------------------
    # Bootstrap
    # ------------------------------------------------------------------

    def bootstrap(self) -> None:
        """Idempotently create the tracking table.

        Safe to call on every app startup — uses ``CREATE TABLE IF NOT
        EXISTS`` and tolerates the catalog race that occurs when two
        parallel bootstraps both pass the existence check before either
        has committed. ``CREATE TABLE IF NOT EXISTS`` is **not** atomic
        across concurrent sessions because the system catalog updates
        only become visible after commit; under load the second call
        can still hit ``pg_type_typname_nsp_index`` violations or
        equivalent ``DuplicateTable`` errors. We catch those and verify
        the table now exists — if it does, the race already resolved
        in our favour and there is nothing else to do.

        Does not touch the audit log or context tables; those come from
        ``auditrum.schema.generate_*_sql`` and are typically applied via
        the host framework's migration system.
        """
        try:
            with self.executor.cursor() as cur:
                cur.execute(_tracking_table_ddl(self.tracking_table))
            return
        except Exception as exc:
            # Re-raise unless the failure looks like a concurrent-create
            # collision. We avoid importing psycopg.errors at module
            # level so the framework-agnostic core stays driver-free.
            if not _looks_like_duplicate_table(exc):
                raise

        # A concurrent bootstrap may have just created the table.
        # Verify it actually exists now; if so, swallow the original
        # error. If not, re-raise so the caller sees the real problem.
        try:
            with self.executor.cursor() as cur:
                cur.execute(
                    "SELECT 1 FROM pg_catalog.pg_class WHERE relname = %s",
                    (self.tracking_table,),
                )
                if cur.fetchone() is None:
                    raise RuntimeError(
                        f"bootstrap failed and {self.tracking_table} still missing"
                    )
        except Exception:
            raise

    # ------------------------------------------------------------------
    # Inspection
    # ------------------------------------------------------------------

    def _fetch_stored(self, trigger_name: str) -> tuple[str, dict] | None:
        """Return ``(checksum, fingerprint)`` or ``None`` if not tracked."""
        with self.executor.cursor() as cur:
            cur.execute(
                f"SELECT checksum, spec_fingerprint FROM {self.tracking_table} "
                "WHERE trigger_name = %s",
                (trigger_name,),
            )
            row = cur.fetchone()
        if row is None:
            return None
        checksum, fingerprint = row
        if isinstance(fingerprint, str):
            fingerprint = json.loads(fingerprint)
        return checksum, fingerprint

    def inspect(self, spec: TrackSpec) -> TriggerStatus:
        """Compare the spec to stored state in the tracking table."""
        bundle = spec.build()
        stored = self._fetch_stored(bundle.trigger_name)
        if stored is None:
            return TriggerStatus.NOT_INSTALLED
        stored_checksum, _ = stored
        if stored_checksum == bundle.checksum:
            return TriggerStatus.INSTALLED
        return TriggerStatus.DRIFT

    def list_installed(self) -> list[dict]:
        """Return all rows from the tracking table as dicts (for CLI output)."""
        with self.executor.cursor() as cur:
            cur.execute(
                f"SELECT trigger_name, table_name, checksum, applied_at, spec_fingerprint "
                f"FROM {self.tracking_table} ORDER BY trigger_name"
            )
            rows = cur.fetchall()
        result: list[dict] = []
        for name, tbl, checksum, applied_at, fp in rows:
            if isinstance(fp, str):
                fp = json.loads(fp)
            result.append(
                {
                    "trigger_name": name,
                    "table_name": tbl,
                    "checksum": checksum,
                    "applied_at": applied_at,
                    "spec_fingerprint": fp,
                }
            )
        return result

    # ------------------------------------------------------------------
    # Install / Uninstall
    # ------------------------------------------------------------------

    def _acquire_lock(self, cur, trigger_name: str) -> None:
        """Acquire a session-level advisory lock keyed by trigger name.

        Serialises concurrent installs of the same trigger so two racing
        deploys don't leave the tracking table inconsistent with the
        actual trigger state in ``pg_catalog``. We use a session lock
        (``pg_advisory_lock``) rather than a transaction lock so the
        protection holds even when the cursor's connection is in
        autocommit mode — each ``cur.execute`` would otherwise commit
        and release a transaction-scoped lock between statements.

        Always pair with :meth:`_release_lock` in a ``try``/``finally``.

        ``hashtextextended`` gives a 64-bit lock key so collisions with
        other advisory-lock users in the same database are negligible.
        """
        cur.execute(
            "SELECT pg_advisory_lock(hashtextextended(%s, 0))",
            (trigger_name,),
        )

    def _release_lock(self, cur, trigger_name: str) -> None:
        """Release the session-level advisory lock acquired by :meth:`_acquire_lock`."""
        cur.execute(
            "SELECT pg_advisory_unlock(hashtextextended(%s, 0))",
            (trigger_name,),
        )

    def _upsert_tracking(
        self, cur, bundle: TriggerBundle, fingerprint: dict[str, Any]
    ) -> None:
        cur.execute(
            f"INSERT INTO {self.tracking_table} "
            "(trigger_name, table_name, checksum, spec_fingerprint, applied_at) "
            "VALUES (%s, %s, %s, %s::jsonb, now()) "
            "ON CONFLICT (trigger_name) DO UPDATE "
            "SET checksum = EXCLUDED.checksum, "
            "spec_fingerprint = EXCLUDED.spec_fingerprint, "
            "applied_at = now()",
            (
                bundle.trigger_name,
                bundle.spec.table,
                bundle.checksum,
                json.dumps(fingerprint),
            ),
        )

    def _delete_tracking(self, cur, trigger_name: str) -> None:
        cur.execute(
            f"DELETE FROM {self.tracking_table} WHERE trigger_name = %s",
            (trigger_name,),
        )

    def install(self, spec: TrackSpec, *, force: bool = False) -> bool:
        """Install or update the trigger for ``spec``.

        Returns ``True`` if any DDL was run, ``False`` if the current
        state already matched (``force=False`` only).
        """
        bundle = spec.build()
        with self.executor.cursor() as cur:
            self._acquire_lock(cur, bundle.trigger_name)
            try:
                if not force:
                    stored = self._fetch_stored(bundle.trigger_name)
                    if stored is not None and stored[0] == bundle.checksum:
                        return False

                cur.execute(bundle.install_sql)
                self._upsert_tracking(cur, bundle, spec.to_fingerprint())
            finally:
                self._release_lock(cur, bundle.trigger_name)
        return True

    def uninstall(self, spec: TrackSpec) -> bool:
        """Drop the trigger + function for ``spec`` and remove its tracking row.

        Returns ``True`` if the trigger was tracked and dropped, ``False``
        if it was already absent.
        """
        bundle = spec.build()
        with self.executor.cursor() as cur:
            self._acquire_lock(cur, bundle.trigger_name)
            try:
                stored = self._fetch_stored(bundle.trigger_name)
                cur.execute(bundle.uninstall_sql)
                self._delete_tracking(cur, bundle.trigger_name)
            finally:
                self._release_lock(cur, bundle.trigger_name)
        return stored is not None

    def uninstall_by_name(self, trigger_name: str, table_name: str) -> bool:
        """Drop a tracked trigger without having the original spec.

        Used by ``sync()`` when the caller wants to prune tracked triggers
        that are no longer in the incoming spec list.

        Both ``trigger_name`` and ``table_name`` are re-validated through
        :func:`validate_identifier` even though they normally come from
        the tracking table (which only accepts validated values on
        insert). Defence in depth: if an attacker can write to
        ``auditrum_applied_triggers`` directly, they should not also get
        DDL execution via ``DROP TRIGGER`` from a maintenance call.
        """
        validate_identifier(trigger_name, "trigger_name")
        validate_identifier(table_name, "table_name")
        uninstall_sql = (
            f"DROP TRIGGER IF EXISTS {trigger_name} ON {table_name};\n"
            f"DROP FUNCTION IF EXISTS {trigger_name}() CASCADE;"
        )
        with self.executor.cursor() as cur:
            self._acquire_lock(cur, trigger_name)
            try:
                cur.execute(uninstall_sql)
                self._delete_tracking(cur, trigger_name)
            finally:
                self._release_lock(cur, trigger_name)
        return True

    # ------------------------------------------------------------------
    # Batch ops
    # ------------------------------------------------------------------

    def diff(
        self, specs: list[TrackSpec], *, prune: bool = False
    ) -> list[DiffEntry]:
        """Return the list of changes ``sync()`` would apply.

        If ``prune=True``, triggers present in the tracking table but not
        in ``specs`` are emitted as :class:`TriggerAction.UNINSTALL`.
        """
        entries: list[DiffEntry] = []
        incoming_names = set()

        for spec in specs:
            bundle = spec.build()
            incoming_names.add(bundle.trigger_name)
            stored = self._fetch_stored(bundle.trigger_name)
            if stored is None:
                entries.append(
                    DiffEntry(
                        spec=spec,
                        action=TriggerAction.INSTALL,
                        status_before=TriggerStatus.NOT_INSTALLED,
                        trigger_name=bundle.trigger_name,
                    )
                )
            elif stored[0] != bundle.checksum:
                entries.append(
                    DiffEntry(
                        spec=spec,
                        action=TriggerAction.UPDATE,
                        status_before=TriggerStatus.DRIFT,
                        trigger_name=bundle.trigger_name,
                    )
                )
            else:
                entries.append(
                    DiffEntry(
                        spec=spec,
                        action=TriggerAction.SKIP,
                        status_before=TriggerStatus.INSTALLED,
                        trigger_name=bundle.trigger_name,
                    )
                )

        if prune:
            for row in self.list_installed():
                name = row["trigger_name"]
                if name not in incoming_names:
                    entries.append(
                        DiffEntry(
                            spec=None,
                            action=TriggerAction.UNINSTALL,
                            status_before=TriggerStatus.INSTALLED,
                            trigger_name=name,
                        )
                    )

        return entries

    def sync(
        self, specs: list[TrackSpec], *, prune: bool = False
    ) -> SyncReport:
        """Idempotent batch install / update / optional prune.

        Returns a :class:`SyncReport` summarizing what was done. Drift is
        automatically repaired by re-installing. Triggers not present in
        ``specs`` are only removed when ``prune=True`` — default is
        safety-first (additive sync).
        """
        report = SyncReport()
        installed_rows = {row["trigger_name"]: row for row in self.list_installed()}
        incoming_names = set()

        for spec in specs:
            bundle = spec.build()
            incoming_names.add(bundle.trigger_name)
            stored = installed_rows.get(bundle.trigger_name)
            if stored is None:
                self.install(spec, force=True)
                report.installed.append(bundle.trigger_name)
            elif stored["checksum"] != bundle.checksum:
                self.install(spec, force=True)
                report.updated.append(bundle.trigger_name)
            else:
                report.skipped.append(bundle.trigger_name)

        if prune:
            for name, row in installed_rows.items():
                if name not in incoming_names:
                    self.uninstall_by_name(name, row["table_name"])
                    report.uninstalled.append(name)

        return report


# Postgres SQLSTATEs / message fragments for "this table is being created
# right now by a concurrent transaction". Different drivers wrap them
# differently — psycopg surfaces ``DuplicateTable`` (42P07) and
# ``UniqueViolation`` (23505) on the implicit pg_type row. We match by
# substring so the driver-agnostic core doesn't need a hard dependency.
_DUPLICATE_TABLE_HINTS = (
    "duplicatetable",
    "duplicate table",
    "already exists",
    "pg_type_typname_nsp_index",
    "pg_class_relname_nsp_index",
    "duplicateobject",
    "duplicate object",
    "uniqueviolation",
)


def _looks_like_duplicate_table(exc: BaseException) -> bool:
    text = (str(exc) + " " + type(exc).__name__).lower()
    return any(hint in text for hint in _DUPLICATE_TABLE_HINTS)
