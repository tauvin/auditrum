"""Clear snapshots of diff-mode tables, keeping the diffs.

Runs :func:`auditrum.compaction.compact_snapshots` for each ``--table``
through Django's default connection. Batches commit separately (Django
runs in autocommit outside ``atomic()``). A failing table does not stop
the others; the command raises ``CommandError`` at the end if any failed.
"""

from __future__ import annotations

from typing import Any

from django.core.management.base import BaseCommand, CommandError, CommandParser
from django.db import connection

from auditrum.compaction import compact_snapshots
from auditrum.diff_chain import parse_since
from auditrum.integrations.django.settings import audit_settings

_MAX_FINDINGS_SHOWN = 10


class Command(BaseCommand):
    help = (
        "Clear snapshots (old_data/new_data) of diff-mode tables, keeping the "
        "diffs. Rows covered by the hash chain and objects with diff-chain "
        "findings are kept."
    )

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument(
            "--table",
            action="append",
            required=True,
            help="Diff-mode table to compact (repeatable).",
        )
        parser.add_argument(
            "--older-than",
            help="Only rows older than this: '6 months', '2 weeks', … or ISO-8601.",
        )
        parser.add_argument(
            "--batch-size", type=int, default=1000, help="Objects per committed batch."
        )
        parser.add_argument("--dry-run", action="store_true", help="Count rows without changing.")
        parser.add_argument("--audit-table", help="Audit table (default: from settings).")

    def handle(self, *args: Any, **options: Any) -> None:
        audit_table = options["audit_table"] or audit_settings.table_name
        try:
            before = (
                parse_since(options["older_than"], option="--older-than")
                if options["older_than"]
                else None
            )
        except ValueError as exc:
            raise CommandError(str(exc)) from exc

        failed: list[str] = []
        cleared = 0
        for table in options["table"]:
            try:
                result = compact_snapshots(
                    connection,
                    table=table,
                    before=before,
                    batch_size=options["batch_size"],
                    dry_run=options["dry_run"],
                    audit_table=audit_table,
                )
            except Exception as exc:
                self.stderr.write(self.style.ERROR(f"{table}: {exc}"))
                failed.append(table)
                continue
            cleared += result.compacted
            verb = "would clear" if result.dry_run else "cleared"
            self.stdout.write(f"{result.table}: {verb} snapshots of {result.compacted} row(s)")
            if result.skipped_hashed:
                self.stdout.write(
                    f"{result.table}: kept {result.skipped_hashed} row(s) covered by the hash chain"
                )
            if result.excluded:
                self.stdout.write(
                    self.style.WARNING(
                        f"{result.table}: kept {len(result.excluded)} object(s) "
                        "with diff-chain findings"
                    )
                )
                for f in result.excluded[:_MAX_FINDINGS_SHOWN]:
                    column = f" {f.column}" if f.column else ""
                    self.stdout.write(
                        self.style.WARNING(
                            f"{f.table}:{f.object_id} event {f.audit_id} {f.kind}{column}: "
                            f"{f.detail}"
                        )
                    )
                if len(result.excluded) > _MAX_FINDINGS_SHOWN:
                    self.stdout.write(
                        self.style.WARNING(
                            f"… and {len(result.excluded) - _MAX_FINDINGS_SHOWN} more"
                        )
                    )
        if not options["dry_run"] and cleared:
            self.stdout.write(f"Run VACUUM (ANALYZE) {audit_table} so the freed space is reused.")
        if failed:
            raise CommandError(f"compact snapshots: failed table(s): {', '.join(failed)}")
