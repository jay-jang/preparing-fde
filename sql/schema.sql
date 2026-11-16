-- Run ONCE in an empty, disposable database with a migration administrator.
-- Production authentication, TLS and credential rotation are platform concerns.
CREATE ROLE fde_reader NOLOGIN NOSUPERUSER NOBYPASSRLS;
CREATE ROLE fde_ingest NOLOGIN NOSUPERUSER NOBYPASSRLS;
CREATE SCHEMA fde;
REVOKE ALL ON SCHEMA fde FROM PUBLIC;
GRANT USAGE ON SCHEMA fde TO fde_reader, fde_ingest;

CREATE TABLE fde.identity_map (
    tenant_id text NOT NULL,
    source text NOT NULL,
    source_key text NOT NULL,
    asset_id uuid NOT NULL,
    authoritative boolean NOT NULL DEFAULT false,
    PRIMARY KEY (tenant_id, source, source_key)
);
-- Only one authoritative source per asset in this kernel.
CREATE UNIQUE INDEX one_authority ON fde.identity_map (tenant_id, asset_id)
    WHERE authoritative;

CREATE TABLE fde.receipts (
    event_id text PRIMARY KEY,
    payload_hash text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp()
);
CREATE TABLE fde.assets (
    tenant_id text NOT NULL,
    asset_id uuid NOT NULL,
    source text NOT NULL,
    name text NOT NULL,
    status text NOT NULL CHECK (status IN ('running', 'stopped', 'unknown')),
    criticality integer NOT NULL CHECK (criticality BETWEEN 1 AND 5),
    epoch bigint NOT NULL,
    source_offset bigint NOT NULL,
    event_sequence bigint NOT NULL,
    deleted boolean NOT NULL DEFAULT false,
    PRIMARY KEY (tenant_id, asset_id)
);
-- An outbox, not a WORM audit store. A separate exporter delivers records to it.
CREATE TABLE fde.audit_outbox (
    event_id text PRIMARY KEY,
    record jsonb NOT NULL,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp()
);
GRANT SELECT ON fde.identity_map TO fde_ingest;
GRANT SELECT, INSERT ON fde.receipts TO fde_ingest;
GRANT SELECT, INSERT, UPDATE ON fde.assets TO fde_ingest;
GRANT INSERT ON fde.audit_outbox TO fde_ingest;

CREATE TABLE fde.memberships (
    tenant_id text NOT NULL,
    subject text NOT NULL,
    roles text[] NOT NULL,
    PRIMARY KEY (tenant_id, subject)
);
CREATE TABLE fde.chunks (
    tenant_id text NOT NULL,
    chunk_id text NOT NULL,
    document_version bigint NOT NULL,
    content text NOT NULL CHECK (length(content) BETWEEN 1 AND 8000),
    embedding double precision[] NOT NULL,
    embedding_model text NOT NULL,
    allowed_roles text[] NOT NULL CHECK (cardinality(allowed_roles) > 0),
    deleted boolean NOT NULL DEFAULT false,
    PRIMARY KEY (tenant_id, chunk_id),
    CHECK (array_ndims(embedding) = 1 AND cardinality(embedding) > 0)
);
CREATE INDEX chunks_tenant ON fde.chunks (tenant_id) WHERE NOT deleted;
ALTER TABLE fde.memberships ENABLE ROW LEVEL SECURITY;
ALTER TABLE fde.memberships FORCE ROW LEVEL SECURITY;
ALTER TABLE fde.chunks ENABLE ROW LEVEL SECURITY;
ALTER TABLE fde.chunks FORCE ROW LEVEL SECURITY;

-- Identity settings are set ONLY by the trusted gateway after JWT validation.
-- RLS cannot defend against an application able to forge these settings.
CREATE POLICY own_membership ON fde.memberships FOR SELECT TO fde_reader
USING (
    tenant_id = current_setting('app.tenant', true)
    AND subject = current_setting('app.subject', true)
);
CREATE POLICY readable_chunks ON fde.chunks FOR SELECT TO fde_reader
USING (
    tenant_id = current_setting('app.tenant', true)
    AND NOT deleted
    AND EXISTS (
        SELECT 1 FROM fde.memberships m
        WHERE m.tenant_id = chunks.tenant_id
          AND m.subject = current_setting('app.subject', true)
          AND m.roles && chunks.allowed_roles
    )
);
GRANT SELECT ON fde.chunks, fde.memberships TO fde_reader;
-- No mutation, table ownership or membership administration for fde_reader.
