"""Connections. DATABASE_URL selects the database (default: the docker-compose registry)."""
from __future__ import annotations

import re
from pathlib import Path

import psycopg
from psycopg.rows import dict_row

from app import settings
from app.db.migrate import dsn_of


def connect(dsn: str | None = None) -> psycopg.Connection:
    return psycopg.connect(dsn or settings.DATABASE_URL, row_factory=dict_row)


def apply_schema(conn) -> None:
    """Apply the merged schema SQL (all CREATE IF NOT EXISTS — fully idempotent)."""
    schema_sql = (Path(__file__).parent / "schema.sql").read_text()
    # Strip single-line comments so semicolons inside them do not cause a split
    cleaned = re.sub(r'--[^\n]*', '', schema_sql)
    stmts = [s.strip() for s in cleaned.split(';') if s.strip()]
    target_dsn = dsn_of(conn)
    conn.commit()
    with psycopg.connect(target_dsn, row_factory=dict_row) as new_conn:
        for stmt in stmts:
            new_conn.execute(stmt)
        new_conn.commit()
