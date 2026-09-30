"""D1: ingestion job model and raw-upload provenance (design 5.2, 18.1).

Revision ID: 0007
Revises: 0006
"""
from alembic import op

from app.db.migrate import run_sql

revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None


def upgrade() -> None:
    run_sql(op, """
    -- One row per ingested upload: raw storage key + sha256, detection result and the
    -- fixed status lifecycle of design 18.1. id is a ULID (app.ingest.jobs.new_id).
    CREATE TABLE IF NOT EXISTS job (
        id              text PRIMARY KEY,
        device          text NOT NULL,
        status          text NOT NULL DEFAULT 'RECEIVED' CHECK (status IN (
                            'RECEIVED', 'DETECTED', 'FLATTENED', 'NORMALIZED',
                            'PARTIALLY_NORMALIZED', 'AUDITED', 'NEEDS_VENDOR',
                            'UNKNOWN_VENDOR', 'FAILED')),
        object_key      text NOT NULL,
        sha256          text NOT NULL,
        uploader        text,
        declared_vendor text,
        vendor_family   text,
        dialect         text,
        detection       jsonb,
        status_history  jsonb NOT NULL DEFAULT '[]',
        created_at      timestamptz NOT NULL DEFAULT now(),
        updated_at      timestamptz NOT NULL DEFAULT now()
    );
    CREATE INDEX IF NOT EXISTS job_device_hash_idx ON job (device, sha256);
    CREATE INDEX IF NOT EXISTS job_status_idx ON job (status);

    -- Which job produced a saved normalized document (nullable: direct CLI runs of
    -- normalize/check without `ingest` create no job).
    ALTER TABLE device ADD COLUMN IF NOT EXISTS job_id text REFERENCES job(id) ON DELETE SET NULL;
    """)


def downgrade() -> None:
    raise NotImplementedError("the registry is migrated forward only")
