"""G2/G4: training_label table, auto_ml mapping source and model_version column.

G2: 'auto_ml' is added to the mapping.source CHECK so auto-approved mappings can be
    distinguished from human ('human') and model-suggestion ('model') ones (§12.7).
G4: training_label records every reviewer decision for reranker training (§12.4, §13.6).
    model_version on mapping enables rollback tracking (§12.9).

Revision ID: 0010
Revises: 0009
"""
from alembic import op

from app.db.migrate import run_sql

revision = "0010"
down_revision = "0009"
branch_labels = None
depends_on = None


def upgrade() -> None:
    run_sql(op, """
    -- G2: allow 'auto_ml' as a mapping source (auto-approved by the ML service).
    ALTER TABLE mapping DROP CONSTRAINT IF EXISTS mapping_source_check;
    ALTER TABLE mapping ADD CONSTRAINT mapping_source_check
        CHECK (source IN ('human', 'model', 'auto_ml', 'seed'));

    -- G4: model_version tag for rollback tracking (§12.9).
    ALTER TABLE mapping ADD COLUMN IF NOT EXISTS model_version text;

    -- G4: one row per reviewer decision, written by the review API / CLI on every
    -- approve / edited-approve / ignore / propose-new-key action (§13.6 step 4).
    -- These rows are the training data for the LightGBM reranker (§12.4, G4).
    CREATE TABLE IF NOT EXISTS training_label (
        id              bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
        template_id     bigint NOT NULL REFERENCES template(id) ON DELETE CASCADE,
        suggested_key   text,           -- top candidate the model showed (null = none)
        chosen_key      text,           -- key the reviewer selected (null = ignored/new_key)
        decision        text NOT NULL
                        CHECK (decision IN ('approved', 'edited', 'ignored', 'new_key')),
        candidates      jsonb,          -- full ranked list (positive + negative examples)
        model_version   text,           -- which model produced the suggestion
        created_at      timestamptz NOT NULL DEFAULT now()
    );
    CREATE INDEX IF NOT EXISTS training_label_template_idx
        ON training_label (template_id);
    CREATE INDEX IF NOT EXISTS training_label_created_idx
        ON training_label (created_at DESC);
    """)


def downgrade() -> None:
    raise NotImplementedError("the registry is migrated forward only")
