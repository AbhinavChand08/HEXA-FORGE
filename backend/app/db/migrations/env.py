"""Alembic environment: plain SQL migrations over psycopg 3 (no ORM metadata)."""
from __future__ import annotations

import psycopg
from alembic import context
from sqlalchemy import create_engine, pool

from app import settings

cfg = context.config
if context.is_offline_mode():
    raise SystemExit("offline (--sql) migrations are not supported; run against a database")

dsn = cfg.attributes.get("dsn") or settings.DATABASE_URL
engine = create_engine("postgresql+psycopg://", creator=lambda: psycopg.connect(dsn),
                       poolclass=pool.NullPool)
with engine.connect() as connection:
    context.configure(connection=connection, target_metadata=None)
    with context.begin_transaction():
        context.run_migrations()
