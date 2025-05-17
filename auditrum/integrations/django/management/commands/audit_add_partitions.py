

import calendar
from datetime import date
from django.core.management.base import BaseCommand
from django.conf import settings
from auditrum.schema import generate_auditlog_partitions_sql
import psycopg


class Command(BaseCommand):
    help = "Create partitions for the auditlog table N months ahead"

    def add_arguments(self, parser):
        parser.add_argument(
            "--months",
            type=int,
            default=1,
            help="Number of months ahead to create partitions for (default: 1)",
        )

    def handle(self, *args, **options):
        months = options["months"]
        sql = generate_auditlog_partitions_sql(months_ahead=months)
        self.stdout.write(f"Executing SQL to create partitions for {months} month(s)...")

        from psycopg.conninfo import make_conninfo

        db_config = settings.DATABASES["default"]
        db_dsn = make_conninfo(
            host=db_config.get("HOST") or "localhost",
            port=db_config.get("PORT") or "5432",
            dbname=db_config["NAME"],
            user=db_config["USER"],
            password=db_config["PASSWORD"],
        )

        with psycopg.connect(db_dsn) as conn, conn.cursor() as cur:
            cur.execute(sql)
        self.stdout.write(self.style.SUCCESS("Partitions created successfully."))
