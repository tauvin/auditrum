"""Re-execute every ``generate_*_sql`` helper whose body the
release currently ships. Used as a safety valve when a user
upgrades auditrum and somehow misses the corresponding
``auditrum_django`` migration — or when running outside the
migration graph entirely (raw psycopg deployments, SQLAlchemy,
emergency recovery).

Idempotent. Every helper is a ``CREATE OR REPLACE FUNCTION``, so
running repeatedly just overwrites with the current code's body.

Does **not** touch table DDL — column-level migrations like
dropping ``content_type_id`` ride their own explicit steps. Only
the PL/pgSQL helper bodies are refreshed here.
"""

from __future__ import annotations

from django.core.management.base import BaseCommand
from django.db import connection

from auditrum.integrations.django.settings import audit_settings
from auditrum.schema import (
    generate_audit_attach_context_sql,
    generate_audit_current_user_id_sql,
    generate_audit_reconstruct_sql,
    generate_jsonb_diff_function_sql,
)


class Command(BaseCommand):
    help = (
        "Re-execute schema-side PL/pgSQL helpers (jsonb_diff, "
        "_audit_attach_context, _audit_current_user_id, "
        "_audit_reconstruct_*). Use after upgrading auditrum if the "
        "corresponding migration did not run, or whenever you need to "
        "force the DB-side helper bodies to match the currently-"
        "installed Python release."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Print the SQL that would be executed and exit.",
        )

    def handle(self, *args, **options):
        sqls: list[tuple[str, str]] = [
            ("jsonb_diff", generate_jsonb_diff_function_sql()),
            (
                "_audit_attach_context",
                generate_audit_attach_context_sql(
                    audit_settings.context_table_name,
                    guc_id=audit_settings.guc_id,
                    guc_metadata=audit_settings.guc_metadata,
                ),
            ),
            (
                "_audit_current_user_id",
                generate_audit_current_user_id_sql(
                    guc_metadata=audit_settings.guc_metadata,
                ),
            ),
            (
                "_audit_reconstruct_row/_table",
                generate_audit_reconstruct_sql(audit_settings.table_name),
            ),
        ]

        if options["dry_run"]:
            for name, sql in sqls:
                self.stdout.write(f"-- {name}")
                self.stdout.write(sql)
                self.stdout.write("")
            return

        with connection.cursor() as cur:
            for name, sql in sqls:
                self.stdout.write(f"Refreshing {name}...")
                cur.execute(sql)

        self.stdout.write(
            self.style.SUCCESS(
                "Schema helpers refreshed. "
                "Trigger function bodies were not touched — run "
                "`./manage.py auditrum_makemigrations && migrate` "
                "to refresh per-table trigger functions if their "
                "checksum changed."
            )
        )
