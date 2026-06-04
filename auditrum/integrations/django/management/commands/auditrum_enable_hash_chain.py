"""Enable the optional tamper-evidence hash chain on the audit log.

Runs :func:`auditrum.hash_chain.generate_hash_chain_sql` against the
configured database. This adds ``row_hash`` / ``prev_hash`` /
``chain_seq`` columns plus a BEFORE INSERT trigger that hashes every new
row into an append-only chain so post-hoc edits become detectable via
``auditrum_verify_chain``.

This is **not** wired into a migration on purpose: the chain serialises
inserts behind a per-table advisory lock, which caps insert throughput.
Enabling it is therefore an explicit operator action.

Idempotent. The generated SQL uses ``IF NOT EXISTS`` /
``CREATE OR REPLACE`` everywhere, so re-running on an already-chained log
is safe.
"""

from __future__ import annotations

from django.core.management.base import BaseCommand
from django.db import connection

from auditrum.hash_chain import generate_hash_chain_sql
from auditrum.integrations.django.settings import audit_settings


class Command(BaseCommand):
    help = (
        "Enable the tamper-evidence hash chain on the audit log by "
        "running generate_hash_chain_sql against the configured "
        "database. Adds row_hash/prev_hash/chain_seq columns and a "
        "BEFORE INSERT trigger. Idempotent. This is a deliberate opt-in "
        "(not a migration) because chaining serialises inserts."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Print the SQL that would be executed and exit.",
        )

    def handle(self, *args, **options):
        table_name = audit_settings.table_name
        sql = generate_hash_chain_sql(table_name)

        if options["dry_run"]:
            self.stdout.write(f"-- hash chain for {table_name}")
            self.stdout.write(sql)
            return

        if not audit_settings.hash_chain:
            self.stderr.write(
                self.style.WARNING(
                    "PGAUDIT_HASH_CHAIN is False but you are enabling the "
                    "hash-chain DDL. Set PGAUDIT_HASH_CHAIN = True so the "
                    "setting reflects reality. Proceeding anyway."
                )
            )

        self.stdout.write(f"Enabling hash chain on {table_name}...")
        with connection.cursor() as cur:
            # generate_hash_chain_sql returns multiple ;-separated statements in
            # one string; psycopg (3 and 2) runs them in a single round-trip via
            # the simple query protocol when params is None.
            cur.execute(sql)

        self.stdout.write(
            self.style.SUCCESS(
                f"Hash chain enabled on {table_name}. New rows are now "
                "chained (row_hash/prev_hash/chain_seq). Verify integrity "
                "with `./manage.py auditrum_verify_chain`, and periodically "
                "anchor the chain tip outside the database."
            )
        )
