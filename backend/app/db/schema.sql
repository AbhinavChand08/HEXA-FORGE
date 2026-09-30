-- Merged idempotent schema for SIH 26155 (all Alembic migrations 0001-0011 + audit tables).
-- Every statement uses CREATE/ALTER IF NOT EXISTS so this is safe to re-run.

CREATE EXTENSION IF NOT EXISTS vector;

-- =============================================================================
-- Vendor profiles
-- =============================================================================
CREATE TABLE IF NOT EXISTS vendor_profile (
    id              integer GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    name            text NOT NULL UNIQUE,
    family          text NOT NULL,
    dialect         text,
    dialect_params  jsonb NOT NULL DEFAULT '{}',
    fingerprints    jsonb NOT NULL DEFAULT '[]',
    threshold       real NOT NULL DEFAULT 1,
    status          text NOT NULL DEFAULT 'onboarding'
                    CHECK (status IN ('onboarding', 'ready')),
    learned         jsonb NOT NULL DEFAULT '{}',
    parent_id       integer REFERENCES vendor_profile(id) ON DELETE SET NULL
                    CHECK (parent_id <> id)
);

-- =============================================================================
-- Registry version counter
-- =============================================================================
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

-- =============================================================================
-- Canonical key ontology
-- =============================================================================
CREATE TABLE IF NOT EXISTS canonical_key (
    key             text PRIMARY KEY CHECK (key ~ '^[A-Z][A-Z0-9_]*(\.[A-Z][A-Z0-9_]*)+$'),
    type            text NOT NULL CHECK (type IN ('int', 'float', 'bool', 'string', 'enum', 'ip', 'cidr')),
    unit            text,
    scope           text NOT NULL DEFAULT 'GLOBAL' CHECK (scope ~ '^[A-Z][A-Z0-9_]*$'),
    cardinality     text NOT NULL DEFAULT 'single' CHECK (cardinality IN ('single', 'list')),
    category        text NOT NULL,
    description     text NOT NULL,
    enum_values     jsonb,
    aliases         jsonb NOT NULL DEFAULT '[]',
    seed_group      text,
    allowed         jsonb,
    embedding       vector(384)
);

-- =============================================================================
-- Templates
-- =============================================================================
CREATE TABLE IF NOT EXISTS template (
    id              bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    vendor_family   text NOT NULL,
    dialect         text NOT NULL,
    os_range        text,
    parent_id       bigint REFERENCES template(id) ON DELETE CASCADE,
    signature       text NOT NULL UNIQUE,
    pattern         jsonb NOT NULL,
    path            jsonb NOT NULL,
    display         text NOT NULL,
    structured      boolean NOT NULL DEFAULT false,
    slots           jsonb NOT NULL DEFAULT '[]',
    vars            jsonb NOT NULL DEFAULT '[]',
    examples        jsonb NOT NULL DEFAULT '[]',
    frequency       integer NOT NULL DEFAULT 0,
    device_count    integer NOT NULL DEFAULT 0,
    status          text NOT NULL DEFAULT 'unknown'
                    CHECK (status IN ('unknown', 'suggested', 'approved', 'ignored', 'merged')),
    merged_into     bigint REFERENCES template(id) ON DELETE SET NULL,
    first_seen      timestamptz NOT NULL DEFAULT now(),
    last_seen       timestamptz NOT NULL DEFAULT now(),
    embedding       vector(384)
);
CREATE INDEX IF NOT EXISTS template_family_idx ON template (vendor_family, parent_id);
CREATE INDEX IF NOT EXISTS template_status_idx ON template (status, frequency DESC);
CREATE INDEX IF NOT EXISTS template_embedding_idx ON template USING hnsw (embedding vector_cosine_ops);

-- =============================================================================
-- Mappings
-- =============================================================================
CREATE TABLE IF NOT EXISTS mapping (
    id              bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    template_id     bigint NOT NULL REFERENCES template(id) ON DELETE CASCADE,
    version         integer NOT NULL,
    slot_names      jsonb NOT NULL DEFAULT '[]',
    scope           jsonb,
    emits           jsonb NOT NULL DEFAULT '[]',
    approved_by     text,
    confidence      real,
    source          text NOT NULL CHECK (source IN ('human', 'model', 'auto_ml', 'seed')),
    active          boolean NOT NULL DEFAULT false,
    evidence        jsonb,
    model_version   text,
    created_at      timestamptz NOT NULL DEFAULT now(),
    registry_version bigint NOT NULL DEFAULT 0,
    UNIQUE (template_id, version)
);
CREATE UNIQUE INDEX IF NOT EXISTS mapping_one_active ON mapping (template_id) WHERE active;

-- =============================================================================
-- Vendor defaults and rules
-- =============================================================================
CREATE TABLE IF NOT EXISTS vendor_default (
    id              integer GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    vendor_family   text NOT NULL,
    key             text NOT NULL REFERENCES canonical_key(key) ON DELETE CASCADE,
    value           jsonb NOT NULL,
    os_range        text,
    note            text
);
CREATE UNIQUE INDEX IF NOT EXISTS vendor_default_uq
    ON vendor_default (vendor_family, key, COALESCE(os_range, ''));

CREATE TABLE IF NOT EXISTS rule (
    id              text PRIMARY KEY,
    benchmark       text NOT NULL,
    key             text NOT NULL REFERENCES canonical_key(key) ON DELETE CASCADE,
    operator        text NOT NULL CHECK (operator IN ('eq', 'ne', 'lt', 'le', 'gt', 'ge', 'between',
                                                      'in', 'not_in', 'contains', 'not_contains',
                                                      'present', 'absent')),
    expected        jsonb,
    scope_selector  jsonb,
    severity        text NOT NULL DEFAULT 'medium'
                    CHECK (severity IN ('low', 'medium', 'high', 'critical')),
    title           text,
    ref             text,
    seed_group      text
);

-- =============================================================================
-- Jobs (before device, since device.job_id references job)
-- =============================================================================
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

-- =============================================================================
-- Normalized device documents
-- =============================================================================
CREATE TABLE IF NOT EXISTS device (
    id              bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    name            text,
    vendor_family   text,
    source_file     text,
    summary         jsonb,
    registry_version bigint,
    job_id          text REFERENCES job(id) ON DELETE SET NULL,
    ingested_at     timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS observation (
    id              bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    device_id       bigint NOT NULL REFERENCES device(id) ON DELETE CASCADE,
    key             text NOT NULL REFERENCES canonical_key(key),
    scope_type      text NOT NULL,
    scope_id        text NOT NULL DEFAULT '',
    value           jsonb NOT NULL,
    source          text NOT NULL CHECK (source IN ('config', 'default')),
    template_id     bigint REFERENCES template(id) ON DELETE SET NULL,
    mapping_id      bigint REFERENCES mapping(id) ON DELETE SET NULL,
    src_line        integer,
    raw             text
);
CREATE INDEX IF NOT EXISTS observation_device_idx ON observation (device_id, key);

-- job_template.job_id references device.id (not job.id) — this is intentional naming
CREATE TABLE IF NOT EXISTS job_template (
    job_id          bigint NOT NULL REFERENCES device(id) ON DELETE CASCADE,
    template_id     bigint NOT NULL REFERENCES template(id) ON DELETE CASCADE,
    count           integer NOT NULL,
    PRIMARY KEY (job_id, template_id)
);
CREATE INDEX IF NOT EXISTS job_template_template_idx ON job_template (template_id);

-- =============================================================================
-- Flat records (persisted parse tree per job)
-- =============================================================================
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

-- =============================================================================
-- Training labels (reviewer decisions for ML reranker training)
-- =============================================================================
CREATE TABLE IF NOT EXISTS training_label (
    id              bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    template_id     bigint NOT NULL REFERENCES template(id) ON DELETE CASCADE,
    suggested_key   text,
    chosen_key      text,
    decision        text NOT NULL
                    CHECK (decision IN ('approved', 'edited', 'ignored', 'new_key')),
    candidates      jsonb,
    model_version   text,
    created_at      timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS training_label_template_idx ON training_label (template_id);
CREATE INDEX IF NOT EXISTS training_label_created_idx ON training_label (created_at DESC);

-- =============================================================================
-- Audit runs and results
-- =============================================================================
CREATE TABLE IF NOT EXISTS audit_run (
    id              bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    job_id          text NOT NULL REFERENCES job(id) ON DELETE CASCADE,
    benchmarks      text[] NOT NULL DEFAULT '{}',
    registry_version bigint,
    started_at      timestamptz NOT NULL DEFAULT now(),
    finished_at     timestamptz,
    summary         jsonb
);
CREATE INDEX IF NOT EXISTS audit_run_job_idx ON audit_run (job_id);

CREATE TABLE IF NOT EXISTS rule_result (
    id              bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    audit_run_id    bigint NOT NULL REFERENCES audit_run(id) ON DELETE CASCADE,
    rule_id         text NOT NULL,
    scope_instance  text NOT NULL DEFAULT '',
    status          text NOT NULL,
    actual          jsonb,
    expected        jsonb,
    severity        text,
    title           text,
    key             text,
    operator        text,
    source          text,
    src_line        integer,
    evidence        jsonb
);
CREATE INDEX IF NOT EXISTS rule_result_run_idx ON rule_result (audit_run_id);
