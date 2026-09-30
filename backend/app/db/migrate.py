"""Schema migrations (Alembic). `upgrade()` is what `mapper init` / `make migrate` run.

Every migration is written to be safe on a database created by the pre-Alembic
0001_baseline.sql (IF NOT EXISTS everywhere), so older registries upgrade in place.
"""
from __future__ import annotations

from pathlib import Path

from alembic import command
from alembic.config import Config
from psycopg.conninfo import make_conninfo

MIGRATIONS = Path(__file__).resolve().parent / "migrations"


def config(dsn: str) -> Config:
    cfg = Config()
    cfg.set_main_option("script_location", str(MIGRATIONS))
    cfg.attributes["dsn"] = dsn
    return cfg


def dsn_of(conn) -> str:
    """libpq DSN of an open psycopg connection (conn.info.dsn omits the password)."""
    return make_conninfo(conn.info.dsn, password=conn.info.password or None)


def upgrade(dsn: str, revision: str = "head") -> None:
    command.upgrade(config(dsn), revision)


def run_sql(op, sql: str) -> None:
    """Run a multi-statement SQL script inside a migration (simple query protocol)."""
    op.get_bind().execution_options(no_parameters=True).exec_driver_sql(sql)
