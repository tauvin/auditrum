from collections.abc import Callable
from functools import lru_cache

import structlog
import typer
from dotenv import load_dotenv
from psycopg import connect
from psycopg import sql as psycopg_sql
from rich import print as rich_print

from auditrum.blame import fetch_blame, format_blame
from auditrum.hardening import generate_grant_admin_sql, generate_revoke_sql
from auditrum.hash_chain import generate_hash_chain_sql, verify_chain
from auditrum.retention import drop_old_partitions, generate_purge_sql
from auditrum.revert import generate_revert_sql_from_log
from auditrum.schema import (
    generate_auditlog_partitions_sql,
    generate_auditlog_table_sql,
)
from auditrum.settings import PgAuditSettings
from auditrum.timetravel import reconstruct_row, reconstruct_table
from auditrum.triggers import generate_trigger_sql

load_dotenv()

log = structlog.get_logger()

app = typer.Typer()


@lru_cache(maxsize=1)
def get_settings() -> PgAuditSettings:
    return PgAuditSettings()


def resolve_audit_table(audit_table: str | None) -> str:
    if audit_table is not None:
        return audit_table
    return get_settings().audit_table


def get_db_dsn(
    dsn: str | None = None,
    host: str | None = None,
    port: int | None = None,
    user: str | None = None,
    password: str | None = None,
    dbname: str | None = None,
) -> str | None:
    if dsn:
        return dsn
    if all([host, port, user, password, dbname]):
        return f"postgresql://{user}:{password}@{host}:{port}/{dbname}"
    return get_settings().db_dsn


@app.callback()
def main(
    verbose: int | None = typer.Option(
        1, "--verbose", "-v", help="Verbosity level (0=quiet, 1=default, 2=debug)"
    ),
) -> None:
    import logging

    level = logging.INFO
    if verbose == 0:
        level = logging.CRITICAL
    elif verbose == 2:
        level = logging.DEBUG

    structlog.configure(
        wrapper_class=structlog.make_filtering_bound_logger(level),
        processors=[
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.dev.ConsoleRenderer(),
        ],
    )


@app.command()
def generate_trigger(
    table: str,
    audit_table: str | None = None,
    dry_run: bool = typer.Option(False, "--dry-run", help="Print SQL instead of executing"),
    output: str | None = typer.Option(
        None, "--output", "-o", help="Write SQL to file instead of printing"
    ),
    dsn: str | None = typer.Option(None, "--db-dsn"),
    host: str | None = typer.Option(None, "--db-host"),
    port: int | None = typer.Option(None, "--db-port"),
    user: str | None = typer.Option(None, "--db-user"),
    password: str | None = typer.Option(None, "--db-password"),
    dbname: str | None = typer.Option(None, "--db-name"),
) -> None:
    log.info("Generating trigger SQL")
    sql = generate_trigger_sql(table_name=table, audit_table=resolve_audit_table(audit_table))
    run_static_sql(sql, dry_run, output, get_db_dsn(dsn, host, port, user, password, dbname))


@app.command()
def init_schema(
    audit_table: str | None = None,
    dry_run: bool = typer.Option(False, "--dry-run", help="Print SQL instead of executing"),
    output: str | None = typer.Option(
        None, "--output", "-o", help="Write SQL to file instead of printing"
    ),
    dsn: str | None = typer.Option(None, "--db-dsn"),
    host: str | None = typer.Option(None, "--db-host"),
    port: int | None = typer.Option(None, "--db-port"),
    user: str | None = typer.Option(None, "--db-user"),
    password: str | None = typer.Option(None, "--db-password"),
    dbname: str | None = typer.Option(None, "--db-name"),
) -> None:
    log.info("Generating auditlog schema SQL")
    sql = generate_auditlog_table_sql(resolve_audit_table(audit_table))
    run_static_sql(sql, dry_run, output, get_db_dsn(dsn, host, port, user, password, dbname))


@app.command()
def create_partitions(
    audit_table: str | None = None,
    months: int = 3,
    dry_run: bool = typer.Option(False, "--dry-run", help="Print SQL instead of executing"),
    output: str | None = typer.Option(
        None, "--output", "-o", help="Write SQL to file instead of printing"
    ),
    dsn: str | None = typer.Option(None, "--db-dsn"),
    host: str | None = typer.Option(None, "--db-host"),
    port: int | None = typer.Option(None, "--db-port"),
    user: str | None = typer.Option(None, "--db-user"),
    password: str | None = typer.Option(None, "--db-password"),
    dbname: str | None = typer.Option(None, "--db-name"),
) -> None:
    log.info("Generating partition SQL", months=months)
    sql = generate_auditlog_partitions_sql(resolve_audit_table(audit_table), months)
    run_static_sql(sql, dry_run, output, get_db_dsn(dsn, host, port, user, password, dbname))


@app.command()
def revert(
    table: str,
    record_id: str,
    log_id: int,
    audit_table: str | None = None,
    dry_run: bool = typer.Option(False, "--dry-run", help="Print SQL instead of executing"),
    output: str | None = typer.Option(
        None, "--output", "-o", help="Write SQL to file instead of printing"
    ),
    dsn: str | None = typer.Option(None, "--db-dsn"),
    host: str | None = typer.Option(None, "--db-host"),
    port: int | None = typer.Option(None, "--db-port"),
    user: str | None = typer.Option(None, "--db-user"),
    password: str | None = typer.Option(None, "--db-password"),
    dbname: str | None = typer.Option(None, "--db-name"),
) -> None:
    log.info("Generating revert SQL", table=table, record_id=record_id, log_id=log_id)
    db_dsn = get_db_dsn(dsn, host, port, user, password, dbname)
    execute_or_print_sql(
        sql_fn=generate_revert_sql_from_log,
        sql_args=(resolve_audit_table(audit_table), table, record_id, log_id),
        dry_run=dry_run,
        output=output,
        db_dsn=db_dsn,
    )


@app.command()
def status(
    audit_table: str | None = None,
    dsn: str | None = typer.Option(None, "--db-dsn"),
    host: str | None = typer.Option(None, "--db-host"),
    port: int | None = typer.Option(None, "--db-port"),
    user: str | None = typer.Option(None, "--db-user"),
    password: str | None = typer.Option(None, "--db-password"),
    dbname: str | None = typer.Option(None, "--db-name"),
) -> None:
    log.info("Checking audit trigger and table status")

    db_dsn = get_db_dsn(dsn, host, port, user, password, dbname)
    resolved_audit_table = resolve_audit_table(audit_table)

    try:
        with connect(dsn=db_dsn) as conn, conn.cursor() as cur:
            # Match any trigger whose function name follows the
            # ``audit_<table>_trigger`` convention used by the current
            # generator. The legacy 0.2 pattern looked for
            # ``audit_trigger_fn`` in the action_statement which has
            # been renamed since.
            cur.execute("""
                SELECT event_object_table AS "table", trigger_name
                FROM information_schema.triggers
                WHERE trigger_name LIKE 'audit\\_%\\_trigger' ESCAPE '\\'
                ORDER BY event_object_table
            """)
            triggers = cur.fetchall()

            cur.execute(
                psycopg_sql.SQL(
                    "SELECT inhrelid::regclass AS partition "
                    "FROM pg_inherits WHERE inhparent = {}::regclass"
                ).format(psycopg_sql.Literal(resolved_audit_table))
            )
            partitions = cur.fetchall()

            cur.execute(
                psycopg_sql.SQL("SELECT COUNT(*) FROM {}").format(
                    psycopg_sql.Identifier(resolved_audit_table)
                )
            )
            row = cur.fetchone()
            row_count = row[0] if row else 0

        log.info("Audit triggers found", count=len(triggers))
        for tbl, trig in triggers:
            rich_print(f"[yellow]- {tbl}: {trig}[/yellow]")

        log.info("Auditlog partitions", count=len(partitions))
        for p in partitions:
            rich_print(f"[blue]- {p[0]}[/blue]")

        log.info("Auditlog row count", rows=row_count)

    except Exception as e:
        log.error("Failed to check audit status", error=str(e))


def display_sql(sql: str, output: str | None = None) -> None:
    if output:
        with open(output, "w") as f:
            f.write(sql + "\n")
        log.info("SQL written to file", path=output)
    else:
        rich_print(f"[bold green]{sql}[/bold green]")


def run_static_sql(
    sql: str,
    dry_run: bool,
    output: str | None,
    db_dsn: str | None,
) -> None:
    if dry_run:
        display_sql(sql, output)
        return

    if not db_dsn:
        log.error("No database DSN provided; pass --db-dsn/--db-* or set PGHOST/PGUSER/...")
        return

    try:
        with connect(dsn=db_dsn) as conn, conn.cursor() as cur:
            cur.execute(sql)
            conn.commit()
            log.info("SQL successfully executed")
    except Exception as e:
        log.error("Failed to execute SQL", error=str(e))


def execute_or_print_sql(
    sql_fn: Callable[..., str],
    sql_args: tuple,
    dry_run: bool,
    output: str | None = None,
    db_dsn: str | None = None,
) -> None:
    if not db_dsn:
        log.error("No database DSN provided; pass --db-dsn/--db-* or set PGHOST/PGUSER/...")
        return
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


@app.command()
def harden(
    audit_table: str | None = None,
    context_table: str = typer.Option(
        "audit_context", "--context-table", help="Context table name"
    ),
    app_role: str | None = typer.Option(
        None, "--app-role", help="Application role to revoke write privileges from"
    ),
    admin_role: str | None = typer.Option(
        None, "--admin-role", help="Admin role to grant full privileges to"
    ),
    dry_run: bool = typer.Option(False, "--dry-run", help="Print SQL instead of executing"),
    output: str | None = typer.Option(None, "--output", "-o"),
    dsn: str | None = typer.Option(None, "--db-dsn"),
    host: str | None = typer.Option(None, "--db-host"),
    port: int | None = typer.Option(None, "--db-port"),
    user: str | None = typer.Option(None, "--db-user"),
    password: str | None = typer.Option(None, "--db-password"),
    dbname: str | None = typer.Option(None, "--db-name"),
) -> None:
    """Revoke direct writes on auditlog/audit_context to make them truly append-only.

    Audit trigger functions are SECURITY DEFINER and run as their owner,
    so they continue to produce audit rows even after direct INSERT is
    revoked from the app role. See docs/hardening.md for the deployment
    model and role split.
    """
    log.info("Generating hardening SQL")
    tbl = resolve_audit_table(audit_table)
    parts = [generate_revoke_sql(tbl, app_role=app_role, context_table=context_table)]
    if admin_role:
        parts.append(generate_grant_admin_sql(tbl, admin_role, context_table=context_table))
    sql_text = "\n".join(parts)
    run_static_sql(sql_text, dry_run, output, get_db_dsn(dsn, host, port, user, password, dbname))


@app.command("enable-hash-chain")
def enable_hash_chain(
    audit_table: str | None = None,
    dry_run: bool = typer.Option(False, "--dry-run", help="Print SQL instead of executing"),
    output: str | None = typer.Option(None, "--output", "-o"),
    dsn: str | None = typer.Option(None, "--db-dsn"),
    host: str | None = typer.Option(None, "--db-host"),
    port: int | None = typer.Option(None, "--db-port"),
    user: str | None = typer.Option(None, "--db-user"),
    password: str | None = typer.Option(None, "--db-password"),
    dbname: str | None = typer.Option(None, "--db-name"),
) -> None:
    """Enable optional SHA-256 hash chaining for tamper detection."""
    log.info("Generating hash-chain SQL")
    sql_text = generate_hash_chain_sql(resolve_audit_table(audit_table))
    run_static_sql(sql_text, dry_run, output, get_db_dsn(dsn, host, port, user, password, dbname))


@app.command("verify-chain")
def verify_chain_cmd(
    audit_table: str | None = None,
    dsn: str | None = typer.Option(None, "--db-dsn"),
    host: str | None = typer.Option(None, "--db-host"),
    port: int | None = typer.Option(None, "--db-port"),
    user: str | None = typer.Option(None, "--db-user"),
    password: str | None = typer.Option(None, "--db-password"),
    dbname: str | None = typer.Option(None, "--db-name"),
) -> None:
    """Verify the integrity of the audit log hash chain."""
    db_dsn = get_db_dsn(dsn, host, port, user, password, dbname)
    if not db_dsn:
        log.error("No database DSN provided")
        return
    try:
        with connect(dsn=db_dsn) as conn:
            result = verify_chain(conn, resolve_audit_table(audit_table))
    except Exception as e:
        log.error("Failed to verify chain", error=str(e))
        return
    if result["ok"]:
        rich_print(f"[green]chain OK: {result['checked']} rows verified[/green]")
    else:
        rich_print(
            f"[red]chain BROKEN: {len(result['broken'])} of {result['checked']} rows invalid[/red]"
        )
        for rid, reason in result["broken"]:
            rich_print(f"[red]  - row {rid}: {reason}[/red]")


@app.command()
def purge(
    older_than: str = typer.Option(..., "--older-than", help="e.g. '30 days', '6 months', '2 years'"),
    audit_table: str | None = None,
    drop_partitions: bool = typer.Option(
        False,
        "--drop-partitions",
        help="Drop month partitions older than the cutoff instead of issuing DELETE",
    ),
    dry_run: bool = typer.Option(False, "--dry-run"),
    dsn: str | None = typer.Option(None, "--db-dsn"),
    host: str | None = typer.Option(None, "--db-host"),
    port: int | None = typer.Option(None, "--db-port"),
    user: str | None = typer.Option(None, "--db-user"),
    password: str | None = typer.Option(None, "--db-password"),
    dbname: str | None = typer.Option(None, "--db-name"),
) -> None:
    """Purge audit rows older than a given interval (retention enforcement)."""
    tbl = resolve_audit_table(audit_table)
    db_dsn = get_db_dsn(dsn, host, port, user, password, dbname)

    if drop_partitions:
        if dry_run:
            log.info("Dry-run: would drop partitions older than", older_than=older_than)
            return
        if not db_dsn:
            log.error("No database DSN provided")
            return
        try:
            with connect(dsn=db_dsn) as conn:
                dropped = drop_old_partitions(conn, tbl, older_than)
            log.info("Partitions dropped", count=len(dropped), names=dropped)
        except Exception as e:
            log.error("Failed to drop partitions", error=str(e))
        return

    try:
        query = generate_purge_sql(tbl, older_than)
    except ValueError as e:
        log.error(str(e))
        return

    if dry_run:
        rich_print(f"[bold green]{query.as_string(None)}[/bold green]")
        return

    if not db_dsn:
        log.error("No database DSN provided")
        return
    try:
        with connect(dsn=db_dsn) as conn, conn.cursor() as cur:
            cur.execute(query)
            deleted = cur.rowcount
            conn.commit()
        log.info("Audit rows purged", deleted=deleted)
    except Exception as e:
        log.error("Failed to purge", error=str(e))


@app.command()
def blame(
    table: str,
    object_id: str,
    field: str | None = typer.Argument(
        None, help="If set, show only changes affecting this column"
    ),
    audit_table: str | None = None,
    limit: int = typer.Option(200, "--limit", "-n"),
    fmt: str = typer.Option(
        "rich", "--format", "-f", help="rich|text|json"
    ),
    dsn: str | None = typer.Option(None, "--db-dsn"),
    host: str | None = typer.Option(None, "--db-host"),
    port: int | None = typer.Option(None, "--db-port"),
    user: str | None = typer.Option(None, "--db-user"),
    password: str | None = typer.Option(None, "--db-password"),
    dbname: str | None = typer.Option(None, "--db-name"),
) -> None:
    """Git-style blame for an audited row. Shows who changed what, when, and why."""
    db_dsn = get_db_dsn(dsn, host, port, user, password, dbname)
    if not db_dsn:
        log.error("No database DSN provided")
        return

    if fmt not in ("rich", "text", "json"):
        log.error("format must be one of rich/text/json", got=fmt)
        return

    try:
        with connect(dsn=db_dsn) as conn:
            entries = fetch_blame(
                conn,
                table=table,
                object_id=object_id,
                field=field,
                audit_table=resolve_audit_table(audit_table),
                limit=limit,
            )
    except Exception as e:
        log.error("Failed to fetch blame", error=str(e))
        return

    output = format_blame(
        entries,
        field=field,
        fmt=fmt,  # type: ignore[arg-type]
        table=table,
        object_id=object_id,
    )

    if fmt == "rich":
        rich_print(output)
    else:
        print(output)


@app.command("as-of")
def as_of(
    table: str,
    at: str = typer.Argument(
        ..., help="ISO-8601 timestamp, e.g. 2024-06-12T14:23:00+00:00"
    ),
    object_id: str | None = typer.Option(
        None,
        "--id",
        help="If set, reconstruct only this row. Otherwise stream the whole table.",
    ),
    audit_table: str | None = None,
    fmt: str = typer.Option("json", "--format", "-f", help="json|jsonl"),
    limit: int | None = typer.Option(
        None, "--limit", help="Max rows when streaming the whole table"
    ),
    dsn: str | None = typer.Option(None, "--db-dsn"),
    host: str | None = typer.Option(None, "--db-host"),
    port: int | None = typer.Option(None, "--db-port"),
    user: str | None = typer.Option(None, "--db-user"),
    password: str | None = typer.Option(None, "--db-password"),
    dbname: str | None = typer.Option(None, "--db-name"),
) -> None:
    """Reconstruct the state of a row or table at a given point in time."""
    import json
    from datetime import datetime

    try:
        target_at = datetime.fromisoformat(at)
    except ValueError as e:
        log.error("Invalid --at timestamp; expected ISO-8601", error=str(e))
        return

    db_dsn = get_db_dsn(dsn, host, port, user, password, dbname)
    if not db_dsn:
        log.error("No database DSN provided")
        return

    resolved_audit_table = resolve_audit_table(audit_table)

    try:
        with connect(dsn=db_dsn) as conn:
            if object_id is not None:
                row = reconstruct_row(
                    conn,
                    table=table,
                    object_id=object_id,
                    at=target_at,
                    audit_table=resolved_audit_table,
                )
                if row is None:
                    log.info(
                        "row did not exist at the target timestamp",
                        table=table,
                        object_id=object_id,
                    )
                    return
                print(json.dumps(row, indent=2 if fmt == "json" else None, default=str))
                return

            count = 0
            for obj_id, row_data in reconstruct_table(
                conn,
                table=table,
                at=target_at,
                audit_table=resolved_audit_table,
            ):
                if limit is not None and count >= limit:
                    break
                if fmt == "jsonl":
                    print(json.dumps({"object_id": obj_id, "row": row_data}, default=str))
                else:
                    print(
                        json.dumps(
                            {"object_id": obj_id, "row": row_data},
                            indent=2,
                            default=str,
                        )
                    )
                count += 1
            log.info("reconstructed rows", count=count, table=table, at=target_at.isoformat())
    except Exception as e:
        log.error("Failed to reconstruct", error=str(e))


if __name__ == "__main__":
    app()
