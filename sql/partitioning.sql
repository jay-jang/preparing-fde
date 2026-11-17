-- Factory tenants as LIST partitions with RLS on the parent. Run ONCE after sql/schema.sql
-- (reuses its fde_reader role) in a disposable database, as the migration administrator.
-- Separate schema fde_part: nothing here touches the fde schema.
CREATE SCHEMA fde_part;
REVOKE ALL ON SCHEMA fde_part FROM PUBLIC;
GRANT USAGE ON SCHEMA fde_part TO fde_reader;

CREATE TABLE fde_part.memberships (
    factory text NOT NULL,
    subject text NOT NULL,
    roles text[] NOT NULL,
    PRIMARY KEY (factory, subject)
);
-- The primary key must include the partition key; chunk_id is unique per factory only.
-- No DEFAULT partition: a row for an unregistered factory fails instead of landing
-- in a shared bucket.
CREATE TABLE fde_part.chunks (
    factory text NOT NULL,
    chunk_id text NOT NULL,
    document_version bigint NOT NULL,
    content text NOT NULL CHECK (length(content) BETWEEN 1 AND 8000),
    embedding double precision[] NOT NULL,
    embedding_model text NOT NULL,
    allowed_roles text[] NOT NULL CHECK (cardinality(allowed_roles) > 0),
    deleted boolean NOT NULL DEFAULT false,
    PRIMARY KEY (factory, chunk_id),
    CHECK (array_ndims(embedding) = 1 AND cardinality(embedding) > 0)
) PARTITION BY LIST (factory);

ALTER TABLE fde_part.memberships ENABLE ROW LEVEL SECURITY;
ALTER TABLE fde_part.memberships FORCE ROW LEVEL SECURITY;
ALTER TABLE fde_part.chunks ENABLE ROW LEVEL SECURITY;
ALTER TABLE fde_part.chunks FORCE ROW LEVEL SECURITY;

-- Queries through the parent use these policies for every partition. The factory
-- comparison is also what prunes the other partitions at executor start-up.
CREATE POLICY own_membership ON fde_part.memberships FOR SELECT TO fde_reader
USING (
    factory = current_setting('app.tenant', true)
    AND subject = current_setting('app.subject', true)
);
CREATE POLICY readable_chunks ON fde_part.chunks FOR SELECT TO fde_reader
USING (
    factory = current_setting('app.tenant', true)
    AND NOT deleted
    AND EXISTS (
        SELECT 1 FROM fde_part.memberships m
        WHERE m.factory = chunks.factory
          AND m.subject = current_setting('app.subject', true)
          AND m.roles && chunks.allowed_roles
    )
);
GRANT SELECT ON fde_part.chunks, fde_part.memberships TO fde_reader;

-- Onboarding. A partition does not inherit the parent's RLS flags, and a partition
-- named directly in a query uses only its own policies. Every partition therefore
-- gets RLS with no policy (default deny) and no grant: direct reads show nothing.
CREATE PROCEDURE fde_part.add_factory(factory text) LANGUAGE plpgsql AS $$
DECLARE
    partition_name text := 'chunks_' || factory;
BEGIN
    IF factory IS NULL OR factory !~ '^[a-z][a-z0-9_]{0,39}$' THEN
        RAISE EXCEPTION 'invalid factory id: %', factory;
    END IF;
    EXECUTE format('CREATE TABLE fde_part.%I PARTITION OF fde_part.chunks FOR VALUES IN (%L)',
                   partition_name, factory);
    EXECUTE format('ALTER TABLE fde_part.%I ENABLE ROW LEVEL SECURITY', partition_name);
    EXECUTE format('ALTER TABLE fde_part.%I FORCE ROW LEVEL SECURITY', partition_name);
    EXECUTE format('REVOKE ALL ON fde_part.%I FROM PUBLIC', partition_name);
END
$$;
REVOKE ALL ON PROCEDURE fde_part.add_factory(text) FROM PUBLIC;

-- Release check after any CREATE ... PARTITION OF or ATTACH PARTITION: must be empty.
-- pg_partition_tree covers every level, so a sub-partitioned factory's leaves are checked
-- too; has_any_column_privilege also catches column-level grants.
CREATE VIEW fde_part.unguarded_partitions AS
SELECT c.oid::regclass AS partition
FROM pg_partition_tree('fde_part.chunks') t
JOIN pg_class c ON c.oid = t.relid
WHERE t.level > 0
  AND (NOT c.relrowsecurity OR NOT c.relforcerowsecurity
       OR has_any_column_privilege('fde_reader', c.oid, 'SELECT'));
REVOKE ALL ON fde_part.unguarded_partitions FROM PUBLIC;

CALL fde_part.add_factory('busan');
CALL fde_part.add_factory('ulsan');

-- Offboarding (run by the administrator, not here):
--   ALTER TABLE fde_part.chunks DETACH PARTITION fde_part.chunks_ulsan;
-- The detached table remains a standalone table for export, then DROP TABLE.
