"""Shared FastAPI dependencies."""
from __future__ import annotations

import psycopg

from app.db.session import connect


def get_db() -> psycopg.Connection:
    conn = connect()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
