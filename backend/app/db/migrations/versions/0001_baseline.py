"""Baseline: the schema as it was before Alembic (tasks A and B).

Revision ID: 0001
Revises:
"""
from pathlib import Path

from alembic import op

from app.db.migrate import run_sql

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    run_sql(op, (Path(__file__).with_suffix(".sql")).read_text())


def downgrade() -> None:
    raise NotImplementedError("the registry is migrated forward only")
