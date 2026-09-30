"""C2: vendor profiles as data (dialect params, weighted fingerprints, threshold, status).

Revision ID: 0003
Revises: 0002
"""
from alembic import op

from app.db.migrate import run_sql

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    run_sql(op, """
    -- dialect NULL = detected per file (Junos brace/set, PAN-OS xml/set).
    ALTER TABLE vendor_profile ALTER COLUMN dialect DROP NOT NULL;
    ALTER TABLE vendor_profile ADD COLUMN IF NOT EXISTS threshold real NOT NULL DEFAULT 1;
    ALTER TABLE vendor_profile ADD COLUMN IF NOT EXISTS status text NOT NULL DEFAULT 'onboarding'
        CHECK (status IN ('onboarding', 'ready'));
    -- Learner output ({dialect, signature, files}): a routing signal owned by `learn`
    -- and the normalizer, kept apart from the reviewed profile fields.
    ALTER TABLE vendor_profile ADD COLUMN IF NOT EXISTS learned jsonb NOT NULL DEFAULT '{}';

    -- fingerprints used to hold the learned {signature, files}; it is now [{pattern, weight}].
    UPDATE vendor_profile
       SET learned = jsonb_build_object('dialect', dialect,
                                        'signature', COALESCE(fingerprints -> 'signature', '[]'),
                                        'files', COALESCE(fingerprints -> 'files', '[]')),
           fingerprints = '[]'
     WHERE jsonb_typeof(fingerprints) = 'object';
    ALTER TABLE vendor_profile ALTER COLUMN fingerprints SET DEFAULT '[]';
    """)


def downgrade() -> None:
    raise NotImplementedError("the registry is migrated forward only")
