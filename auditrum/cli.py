from typing import Callable, Optional, Tuple

import structlog
import typer
from dotenv import load_dotenv
from psycopg import connect
from rich import print as rich_print
from settings import PgAuditSettings

from auditrum.revert import generate_revert_sql_from_log
from auditrum.schema import (
    generate_auditlog_partitions_sql,
    generate_auditlog_table_sql,
)
from auditrum.triggers import generate_trigger_sql

load_dotenv()

# Helper function to get DB DSN from arguments or environment
def get_db_dsn(
    dsn: Optional[str] = None,
    host: Optional[str] = None,
    port: Optional[int] = None,
    user: Optional[str] = None,
    password: Optional[str] = None,
    dbname: Optional[str] = None,
) -> Optional[str]:
    if dsn:
        return dsn
    if all([host, port, user, password, dbname]):
        return f"postgresql://{user}:{password}@{host}:{port}/{dbname}"
    return settings.db_dsn


app = typer.Typer()


@app.callback()
def main(
    verbose: Optional[int] = typer.Option(
        1, "--verbose", "-v", help="Verbosity level (0=quiet, 1=default, 2=debug)"
    ),
):
    level = structlog.INFO
    if verbose == 0:
        level = structlog.NOTSET
    elif verbose == 2:
        level = structlog.DEBUG

    structlog.configure(
        wrapper_class=structlog.make_filtering_bound_logger(level),
        processors=[
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.dev.ConsoleRenderer(),
        ],
    )


log = structlog.get_logger()

settings = PgAuditSettings()


@app.command()
def generate_trigger(
    table: str,
    audit_table: str = settings.audit_table,
    dry_run: bool = typer.Option(False, "--dry-run", help="Print SQL instead of executing"),
    output: Optional[str] = typer.Option(
        None, "--output", "-o", help="Write SQL to file instead of printing"
    ),
    dsn: Optional[str] = typer.Option(None, "--db-dsn"),
    host: Optional[str] = typer.Option(None, "--db-host"),
    port: Optional[int] = typer.Option(None, "--db-port"),
    user: Optional[str] = typer.Option(None, "--db-user"),
    password: Optional[str] = typer.Option(None, "--db-password"),
    dbname: Optional[str] = typer.Option(None, "--db-name"),
):
    log.info("Generating trigger SQL")
    sql = generate_trigger_sql(table_name=table, audit_table=audit_table)
    db_dsn = get_db_dsn(dsn, host, port, user, password, dbname)
    execute_or_print_sql(lambda conn: sql, (), dry_run, output, db_dsn=db_dsn)


@app.command()
def init_schema(
    audit_table: str = settings.audit_table,
    dry_run: bool = typer.Option(False, "--dry-run", help="Print SQL instead of executing"),
    output: Optional[str] = typer.Option(
        None, "--output", "-o", help="Write SQL to file instead of printing"
    ),
    dsn: Optional[str] = typer.Option(None, "--db-dsn"),
    host: Optional[str] = typer.Option(None, "--db-host"),
    port: Optional[int] = typer.Option(None, "--db-port"),
    user: Optional[str] = typer.Option(None, "--db-user"),
    password: Optional[str] = typer.Option(None, "--db-password"),
    dbname: Optional[str] = typer.Option(None, "--db-name"),
):
    log.info("Generating auditlog schema SQL")
    sql = generate_auditlog_table_sql(audit_table)
    db_dsn = get_db_dsn(dsn, host, port, user, password, dbname)
    execute_or_print_sql(lambda conn: sql, (), dry_run, output, db_dsn=db_dsn)


@app.command()
def create_partitions(
    audit_table: str = settings.audit_table,
    months: int = 3,
    dry_run: bool = typer.Option(False, "--dry-run", help="Print SQL instead of executing"),
    output: Optional[str] = typer.Option(
        None, "--output", "-o", help="Write SQL to file instead of printing"
    ),
    dsn: Optional[str] = typer.Option(None, "--db-dsn"),
    host: Optional[str] = typer.Option(None, "--db-host"),
    port: Optional[int] = typer.Option(None, "--db-port"),
    user: Optional[str] = typer.Option(None, "--db-user"),
    password: Optional[str] = typer.Option(None, "--db-password"),
    dbname: Optional[str] = typer.Option(None, "--db-name"),
):
    log.info("Generating partition SQL", months=months)
    sql = generate_auditlog_partitions_sql(audit_table, months)
    db_dsn = get_db_dsn(dsn, host, port, user, password, dbname)
    execute_or_print_sql(lambda conn: sql, (), dry_run, output, db_dsn=db_dsn)


@app.command()
def revert(
    table: str,
    record_id: str,
    log_id: int,
    audit_table: str = settings.audit_table,
    dry_run: bool = typer.Option(False, "--dry-run", help="Print SQL instead of executing"),
    output: Optional[str] = typer.Option(
        None, "--output", "-o", help="Write SQL to file instead of printing"
    ),
    dsn: Optional[str] = typer.Option(None, "--db-dsn"),
    host: Optional[str] = typer.Option(None, "--db-host"),
    port: Optional[int] = typer.Option(None, "--db-port"),
    user: Optional[str] = typer.Option(None, "--db-user"),
    password: Optional[str] = typer.Option(None, "--db-password"),
    dbname: Optional[str] = typer.Option(None, "--db-name"),
):
    log.info("Generating revert SQL", table=table, record_id=record_id, log_id=log_id)
    db_dsn = get_db_dsn(dsn, host, port, user, password, dbname)
    execute_or_print_sql(
        sql_fn=generate_revert_sql_from_log,
        sql_args=(audit_table, table, record_id, log_id),
        dry_run=dry_run,
        output=output,
        db_dsn=db_dsn,
    )


@app.command()
def status(
    audit_table: str = settings.audit_table,
    dsn: Optional[str] = typer.Option(None, "--db-dsn"),
    host: Optional[str] = typer.Option(None, "--db-host"),
    port: Optional[int] = typer.Option(None, "--db-port"),
    user: Optional[str] = typer.Option(None, "--db-user"),
    password: Optional[str] = typer.Option(None, "--db-password"),
    dbname: Optional[str] = typer.Option(None, "--db-name"),
):
    log.info("Checking audit trigger and table status")

    db_dsn = get_db_dsn(dsn, host, port, user, password, dbname)

    try:
        with connect(dsn=db_dsn) as conn, conn.cursor() as cur:
            # Check triggers
            cur.execute("""
                SELECT event_object_table AS "table", trigger_name
                FROM information_schema.triggers
                WHERE action_statement ILIKE '%audit_trigger_fn%'
            """)
            triggers = cur.fetchall()

            # Check partitions
            cur.execute(f"""
                SELECT inhrelid::regclass AS partition
                FROM pg_inherits
                WHERE inhparent = '{audit_table}'::regclass
            """)
            partitions = cur.fetchall()

            # Check auditlog size and row count
            cur.execute(f"SELECT COUNT(*) FROM {audit_table}")
            row_count = cur.fetchone()[0]

        log.info("Audit triggers found", count=len(triggers))
        for table, trig in triggers:
            rich_print(f"[yellow]- {table}: {trig}[/yellow]")

        log.info("Auditlog partitions", count=len(partitions))
        for p in partitions:
            rich_print(f"[blue]- {p[0]}[/blue]")

        log.info("Auditlog row count", rows=row_count)

    except Exception as e:
        log.error("Failed to check audit status", error=str(e))


def display_sql(sql: str, output: Optional[str] = None):
    if output:
        with open(output, "w") as f:
            f.write(sql + "\n")
        log.info("SQL written to file", path=output)
    else:
        rich_print(f"[bold green]{sql}[/bold green]")


def execute_or_print_sql(
    sql_fn: Callable[..., str],
    sql_args: Tuple,
    dry_run: bool,
    output: Optional[str] = None,
    db_dsn: Optional[str] = None,
):
    try:
        with connect(dsn=db_dsn) as conn:
            sql = sql_fn(conn, *sql_args)

            if dry_run:
                display_sql(sql, output)
                return

            with conn.cursor() as cur:
                cur.execute(sql)
            conn.commit()
            log.info("SQL successfully executed")
    except Exception as e:
        log.error("Failed to execute SQL", error=str(e))


if __name__ == "__main__":
    app()
