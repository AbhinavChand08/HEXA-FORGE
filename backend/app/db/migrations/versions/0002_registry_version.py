"""C1: global registry version counter, stamped on mappings and saved documents.

Revision ID: 0002
Revises: 0001
"""
from alembic import op

from app.db.migrate import run_sql

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    run_sql(op, """
    -- One row per registry write (design 9.1, 9.5). Version 0 = before versioning existed.
    CREATE TABLE IF NOT EXISTS registry_version (
        version      bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
        changed_at   timestamptz NOT NULL DEFAULT now(),
        change_kind  text NOT NULL
                     CHECK (change_kind IN ('mapping', 'key', 'default', 'profile', 'rule')),
        change_ref   text,
        actor        text,
        detail       jsonb
    );
    CREATE INDEX IF NOT EXISTS registry_version_ref_idx ON registry_version (change_ref);

    ALTER TABLE mapping ADD COLUMN IF NOT EXISTS registry_version bigint;
    UPDATE mapping SET registry_version = 0 WHERE registry_version IS NULL;
    ALTER TABLE mapping ALTER COLUMN registry_version SET NOT NULL;

    ALTER TABLE device ADD COLUMN IF NOT EXISTS registry_version bigint;
    """)


def downgrade() -> None:
    raise NotImplementedError("the registry is migrated forward only")
