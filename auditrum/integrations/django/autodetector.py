"""Detect drift between the ``@track`` registry and the trigger migrations.

Django's own autodetector knows nothing about audit triggers: changing
``@track(exclude=[...])`` leaves the model state untouched, so
``makemigrations`` reports "No changes detected" and ``migrate`` keeps the
old trigger body in the database.

This module is auditrum's equivalent of that autodetector. It replays
every :class:`InstallTrigger` / :class:`UninstallTrigger` operation found
in the migration graph (in dependency order) to reconstruct the trigger
state the migrations produce, then diffs it against the in-memory
registry. Used by ``auditrum_makemigrations`` to write only what changed,
and by the ``auditrum.W001`` system check to warn when a spec was edited
without a matching migration.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from django.db.migrations.loader import MigrationLoader

from auditrum.integrations.django.operations import InstallTrigger, UninstallTrigger
from auditrum.integrations.django.tracking import specs_by_app_label
from auditrum.tracking import TrackSpec

__all__ = [
    "ChangeKind",
    "MigratedTrigger",
    "TriggerChange",
    "detect_changes",
    "migrated_trigger_state",
]


@dataclass(frozen=True)
class MigratedTrigger:
    """A trigger as the migration history leaves it."""

    app_label: str
    spec: TrackSpec
    # ``None`` for migrations generated before InstallTrigger carried a checksum.
    checksum: str | None


class ChangeKind(Enum):
    INSTALL = "install"
    UPDATE = "update"
    UNINSTALL = "uninstall"


@dataclass(frozen=True)
class TriggerChange:
    app_label: str
    kind: ChangeKind
    spec: TrackSpec
    reason: str

    @property
    def trigger_name(self) -> str:
        return self.spec.effective_trigger_name


def migrated_trigger_state(loader: MigrationLoader | None = None) -> dict[str, MigratedTrigger]:
    """Replay trigger operations from every migration on disk.

    Returns ``{trigger_name: MigratedTrigger}`` for triggers that are
    installed once all migrations are applied. Does not touch the database.
    """
    if loader is None:
        loader = MigrationLoader(None, ignore_no_migrations=True)
    graph = loader.graph

    # Concatenating forwards plans of all leaves yields a valid topological
    # order of the whole graph (the same order ``migrate`` would use).
    plan: list[tuple[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for leaf in graph.leaf_nodes():
        for key in graph.forwards_plan(leaf):
            if key not in seen:
                seen.add(key)
                plan.append(key)

    state: dict[str, MigratedTrigger] = {}
    for key in plan:
        migration = graph.nodes[key]
        for op in migration.operations:
            if isinstance(op, InstallTrigger):
                state[op.spec.effective_trigger_name] = MigratedTrigger(
                    app_label=migration.app_label, spec=op.spec, checksum=op.checksum
                )
            elif isinstance(op, UninstallTrigger):
                state.pop(op.spec.effective_trigger_name, None)
    return state


def detect_changes(loader: MigrationLoader | None = None) -> dict[str, list[TriggerChange]]:
    """Diff the ``@track`` registry against :func:`migrated_trigger_state`.

    Returns changes grouped by the app label whose ``migrations/``
    directory should receive them. Within an app, uninstalls come before
    installs so a renamed trigger is dropped before its replacement is
    created.
    """
    migrated = migrated_trigger_state(loader)

    current: dict[str, tuple[str, TrackSpec]] = {}
    for app_label, items in specs_by_app_label().items():
        for _, spec in items:
            current[spec.effective_trigger_name] = (app_label, spec)

    changes: dict[str, list[TriggerChange]] = {}

    for name, old in migrated.items():
        if name not in current:
            changes.setdefault(old.app_label, []).append(
                TriggerChange(old.app_label, ChangeKind.UNINSTALL, old.spec, "no longer tracked")
            )

    for name, (app_label, spec) in current.items():
        old = migrated.get(name)
        if old is None:
            change = TriggerChange(app_label, ChangeKind.INSTALL, spec, "new")
        elif old.spec != spec:
            change = TriggerChange(app_label, ChangeKind.UPDATE, spec, "spec changed")
        elif old.checksum is None:
            # Pre-checksum migration: we cannot tell which template revision
            # it installed, so refresh once and record the checksum from now on.
            change = TriggerChange(app_label, ChangeKind.UPDATE, spec, "no checksum recorded")
        elif old.checksum != spec.build().checksum:
            change = TriggerChange(app_label, ChangeKind.UPDATE, spec, "trigger body changed")
        else:
            continue
        changes.setdefault(app_label, []).append(change)

    return changes
