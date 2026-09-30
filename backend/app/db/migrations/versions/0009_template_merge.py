"""E3: offline re-mining can supersede a runtime-registered unknown template with a
better-mined one; 'merged' keeps it out of the review queue but still traceable
(design 7.7, 13.3).

Revision ID: 0009
Revises: 0008
"""
from alembic import op

from app.db.migrate import run_sql

revision = "0009"
down_revision = "0008"
branch_labels = None
depends_on = None


def upgrade() -> None:
    run_sql(op, """
    ALTER TABLE template DROP CONSTRAINT IF EXISTS template_status_check;
    ALTER TABLE template ADD CONSTRAINT template_status_check
        CHECK (status IN ('unknown', 'suggested', 'approved', 'ignored', 'merged'));
    ALTER TABLE template ADD COLUMN IF NOT EXISTS merged_into bigint
        REFERENCES template(id) ON DELETE SET NULL;
    """)


def downgrade() -> None:
    raise NotImplementedError("the registry is migrated forward only")
