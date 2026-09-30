"""C6: remember the seed file of keys and rules, so an export writes them back to it.

Revision ID: 0006
Revises: 0005
"""
from alembic import op

from app.db.migrate import run_sql

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None


def upgrade() -> None:
    run_sql(op, """
    ALTER TABLE canonical_key ADD COLUMN IF NOT EXISTS seed_group text;
    ALTER TABLE rule ADD COLUMN IF NOT EXISTS seed_group text;
    """)


def downgrade() -> None:
    raise NotImplementedError("the registry is migrated forward only")
