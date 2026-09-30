"""E2: persist flattened records per job, so re-normalization can skip re-parsing
(design 6.7, 13.6).

Revision ID: 0008
Revises: 0007
"""
from alembic import op

from app.db.migrate import run_sql

revision = "0008"
down_revision = "0007"
branch_labels = None
depends_on = None


def upgrade() -> None:
    run_sql(op, """
    -- One row per flat record of a job (app.core.models.FlatCommand). seq/parent_seq are
    -- FlatCommand.id/parent_id, so the parent tree is rebuilt without re-parsing raw text.
    CREATE TABLE IF NOT EXISTS flat_record (
        id              bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
        job_id          text NOT NULL REFERENCES job(id) ON DELETE CASCADE,
        seq             integer NOT NULL,
        parent_seq      integer,
        tokens          jsonb NOT NULL,
        negated         boolean NOT NULL DEFAULT false,
        is_block        boolean NOT NULL DEFAULT false,
        inactive        boolean NOT NULL DEFAULT false,
        secret_masked   boolean NOT NULL DEFAULT false,
        src_line        integer,
        src_lines       jsonb,
        blob            text,
        raw             text,
        device_id       text,
        UNIQUE (job_id, seq)
    );
    CREATE INDEX IF NOT EXISTS flat_record_job_idx ON flat_record (job_id);
    """)


def downgrade() -> None:
    raise NotImplementedError("the registry is migrated forward only")
