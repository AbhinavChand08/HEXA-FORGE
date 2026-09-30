"""C3: profile inheritance (a child profile reuses its parent's templates and mappings).

Revision ID: 0004
Revises: 0003
"""
from alembic import op

from app.db.migrate import run_sql

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    run_sql(op, """
    ALTER TABLE vendor_profile ADD COLUMN IF NOT EXISTS parent_id integer
        REFERENCES vendor_profile(id) ON DELETE SET NULL CHECK (parent_id <> id);
    """)


def downgrade() -> None:
    raise NotImplementedError("the registry is migrated forward only")
