"""Find writes that bypassed the audit trigger.

Runs :func:`auditrum.diff_chain.verify_diff_chain` for the registered
tables (or ``--table``) over a window (``--since``, default one day) or
whole tables (``--full``) and raises ``CommandError`` when anything is
found, so it slots into cron / monitoring next to ``auditrum_verify_chain``.
"""

from __future__ import annotations

import dataclasses
import json
from typing import Any

from django.core.management.base import BaseCommand, CommandError, CommandParser
from django.db import connection

from auditrum.diff_chain import list_registered_tables, parse_since, verify_diff_chain
from auditrum.integrations.django.settings import audit_settings


class Command(BaseCommand):
    help = (
        "Check the audit chain for writes that bypassed the trigger. Exits "
        "non-zero when anything is found (usable from cron / monitoring)."
    )

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument("--table", action="append", help="Table to check (repeatable).")
        parser.add_argument(
            "--since", default="1 day", help="Window start: '1 day', '2 weeks', … or ISO-8601."
        )
        parser.add_argument("--full", action="store_true", help="Check whole tables.")
        parser.add_argument("--object-id", help="Check one object only.")
        parser.add_argument("--json", action="store_true", help="Print findings as JSON.")
        parser.add_argument("--limit", type=int, help="Report at most N findings.")

    def handle(self, *args: Any, **options: Any) -> None:
        audit_table = audit_settings.table_name
        full = options["full"]
        try:
            since = None if full else parse_since(options["since"])
        except ValueError as exc:
            raise CommandError(str(exc)) from exc

        tables = options["table"] or list_registered_tables(connection, audit_table)
        if not tables:
            raise CommandError(
                f"no tracked tables registered for audit table {audit_table} — nothing checked"
            )
        findings = []
        failed = []
        for table in tables:
            try:
                findings.extend(
                    verify_diff_chain(
                        connection,
                        table=table,
                        since=since,
                        object_id=options["object_id"],
                        full=full,
                        audit_table=audit_table,
                        limit=options["limit"],
                    )
                )
            except Exception as exc:
                self.stderr.write(self.style.ERROR(f"{table}: {exc}"))
                failed.append(table)
        if options["limit"] is not None:
            findings = findings[: options["limit"]]

        if options["json"]:
            self.stdout.write(json.dumps([dataclasses.asdict(f) for f in findings], default=str))
        for f in findings:
            column = f" {f.column}" if f.column else ""
            self.stderr.write(
                self.style.ERROR(
                    f"{f.table}:{f.object_id} event {f.audit_id} {f.kind}{column}: {f.detail}"
                )
            )
        if failed:
            raise CommandError(f"diff chain: could not check table(s): {', '.join(failed)}")
        if findings:
            raise CommandError(f"diff chain: {len(findings)} finding(s)")
        if not options["json"]:
            self.stdout.write(self.style.SUCCESS("diff chain OK"))
