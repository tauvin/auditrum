"""Verify the integrity of the audit-log hash chain.

Runs :func:`auditrum.hash_chain.verify_chain` against the configured
database, prints whether the chain is intact, and **exits non-zero**
(raising ``CommandError``) if any row is broken — so it slots straight
into cron / monitoring as a tamper alarm.

Also reports the current chain tip (id / chain_seq / row_hash) from
:func:`auditrum.hash_chain.get_chain_tip`; capture that value
periodically and store it OUTSIDE the database so even a superuser
cannot silently rewrite the whole chain (see the django docs).
"""

from __future__ import annotations

import json
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError
from django.db import connection

from auditrum.hash_chain import get_chain_tip, verify_chain
from auditrum.integrations.django.settings import audit_settings


class Command(BaseCommand):
    help = (
        "Verify the audit-log hash chain. Prints whether the chain is "
        "intact and exits non-zero if it is broken (usable from cron / "
        "monitoring)."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--expected-tip-json",
            help=(
                "Anchor the chain tip to detect tail-row deletion / "
                "full-chain rewrite. Accepts either an inline JSON object "
                "or a path to a JSON file with the shape printed by this "
                "command / returned by get_chain_tip "
                "(keys: id, chain_seq, row_hash, changed_at). Capture it "
                "from a known-good run, store it outside the database, and "
                "feed it back here on the next run."
            ),
        )

    def _load_expected_tip(self, raw: str) -> dict:
        path = Path(raw)
        if path.is_file():
            raw = path.read_text()
        try:
            tip = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise CommandError(
                f"--expected-tip-json is not valid JSON nor an existing file: {exc}"
            ) from exc
        if not isinstance(tip, dict):
            raise CommandError("--expected-tip-json must decode to a JSON object.")
        return tip

    def handle(self, *args, **options):
        table_name = audit_settings.table_name

        expected_tip = None
        if options["expected_tip_json"]:
            expected_tip = self._load_expected_tip(options["expected_tip_json"])

        # Snapshot the head BEFORE verifying so the printed tip reflects the
        # chain state at the moment verification started.
        tip = get_chain_tip(connection, table_name)
        result = verify_chain(connection, table_name, expected_tip=expected_tip)

        if tip["row_hash"] is not None:
            self.stdout.write(
                f"chain tip: id={tip['id']} chain_seq={tip['chain_seq']} row_hash={tip['row_hash']}"
            )

        if result["ok"]:
            self.stdout.write(self.style.SUCCESS(f"chain OK: {result['checked']} rows verified."))
            return

        for rid, reason in result["broken"]:
            self.stderr.write(self.style.ERROR(f"  - row {rid}: {reason}"))
        raise CommandError(
            f"chain BROKEN: {len(result['broken'])} of {result['checked']} rows invalid."
        )
