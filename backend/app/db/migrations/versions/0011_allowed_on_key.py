"""F6: add `allowed` jsonb column to canonical_key for value validation (min/max/enum).

Revision ID: 0011
Revises: 0010
"""
from alembic import op

from app.db.migrate import run_sql

revision = "0011"
down_revision = "0010"
branch_labels = None
depends_on = None


def upgrade() -> None:
    run_sql(op, """
    ALTER TABLE canonical_key ADD COLUMN IF NOT EXISTS allowed jsonb;
    """)


def downgrade() -> None:
    raise NotImplementedError("the registry is migrated forward only")
