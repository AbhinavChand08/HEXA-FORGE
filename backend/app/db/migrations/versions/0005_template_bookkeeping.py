"""C4: template bookkeeping (job_template link, device_count).

Revision ID: 0005
Revises: 0004
"""
from alembic import op

from app.db.migrate import run_sql

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    run_sql(op, """
    -- Which templates each saved config used. job_id is a device row (one ingested config)
    -- until the job table of task D1 replaces it.
    CREATE TABLE IF NOT EXISTS job_template (
        job_id       bigint NOT NULL REFERENCES device(id) ON DELETE CASCADE,
        template_id  bigint NOT NULL REFERENCES template(id) ON DELETE CASCADE,
        count        integer NOT NULL,
        PRIMARY KEY (job_id, template_id)
    );
    CREATE INDEX IF NOT EXISTS job_template_template_idx ON job_template (template_id);

    ALTER TABLE template ADD COLUMN IF NOT EXISTS device_count integer NOT NULL DEFAULT 0;
    """)


def downgrade() -> None:
    raise NotImplementedError("the registry is migrated forward only")
