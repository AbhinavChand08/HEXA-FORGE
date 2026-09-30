-- Mapping registry for SIH 26155: templates -> canonical keys.
-- Frozen Alembic baseline (revision 0001): later changes go in new revisions.
-- Idempotent, so databases created from this file before Alembic upgrade in place.

CREATE EXTENSION IF NOT EXISTS vector;

-- One row per learned vendor profile. fingerprints.signature = top-level command
-- set used to route a new config to its family.
CREATE TABLE IF NOT EXISTS vendor_profile (
    id              integer GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    name            text NOT NULL UNIQUE,
    family          text NOT NULL,
    dialect         text NOT NULL,
    dialect_params  jsonb NOT NULL DEFAULT '{}',
    fingerprints    jsonb NOT NULL DEFAULT '{}'
);

-- The ontology: vendor-neutral words.
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
    embedding       vector(384)
);

-- signature = hash(vendor_family, dialect, path) from app/templating/signature.py.
-- display is the full path ('line con <NUM> > exec-timeout <NUM> <NUM>');
-- vars names every slot of the path v1..vn (parents first) for transforms.
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
    status          text NOT NULL DEFAULT 'unknown'
                    CHECK (status IN ('unknown', 'suggested', 'approved', 'ignored')),
    first_seen      timestamptz NOT NULL DEFAULT now(),
    last_seen       timestamptz NOT NULL DEFAULT now(),
    embedding       vector(384)
);
CREATE INDEX IF NOT EXISTS template_family_idx ON template (vendor_family, parent_id);
CREATE INDEX IF NOT EXISTS template_status_idx ON template (status, frequency DESC);
CREATE INDEX IF NOT EXISTS template_embedding_idx ON template USING hnsw (embedding vector_cosine_ops);

-- A template emits zero or more keys. emits = [{key, value, when?}], value/when are
-- sandboxed expressions (app/mapping/transforms/). scope = {type, id} where id is a
-- '{slot}' format string. Mappings with a scope and no emits declare scope
-- instances (e.g. 'line vty 0 4') so vendor defaults can fill them.
-- source=model rows are suggestions (inactive) with the ranked candidates in evidence.
CREATE TABLE IF NOT EXISTS mapping (
    id              bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    template_id     bigint NOT NULL REFERENCES template(id) ON DELETE CASCADE,
    version         integer NOT NULL,
    slot_names      jsonb NOT NULL DEFAULT '[]',
    scope           jsonb,
    emits           jsonb NOT NULL DEFAULT '[]',
    approved_by     text,
    confidence      real,
    source          text NOT NULL CHECK (source IN ('human', 'model', 'seed')),
    active          boolean NOT NULL DEFAULT false,
    evidence        jsonb,
    created_at      timestamptz NOT NULL DEFAULT now(),
    UNIQUE (template_id, version)
);
CREATE UNIQUE INDEX IF NOT EXISTS mapping_one_active ON mapping (template_id) WHERE active;

-- What 'absent' means per vendor. os_range: '>=15.0,<17' against SYSTEM.OS_VERSION.
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
    ref             text
);

-- Normalised output per ingested config, with lineage back to the config line.
CREATE TABLE IF NOT EXISTS device (
    id              bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    name            text,
    vendor_family   text,
    source_file     text,
    summary         jsonb,
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
