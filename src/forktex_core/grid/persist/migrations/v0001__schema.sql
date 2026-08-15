CREATE TABLE "{schema}".grid_space (
    id              uuid PRIMARY KEY,
    namespace       varchar(255) NOT NULL DEFAULT '',
    slug            varchar(128) NOT NULL,
    label           varchar(255) NOT NULL,
    config          jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at      timestamp NOT NULL DEFAULT now(),
    updated_at      timestamp NOT NULL DEFAULT now(),
    created_by_id   uuid,
    updated_by_id   uuid,
    archived_at     timestamptz,
    is_active       boolean NOT NULL DEFAULT true,
    CONSTRAINT ck_grid_space_active_archive
        CHECK ((is_active AND archived_at IS NULL) OR (NOT is_active AND archived_at IS NOT NULL))
);
CREATE UNIQUE INDEX uq_grid_space_active ON "{schema}".grid_space (namespace, slug) WHERE archived_at IS NULL;
CREATE INDEX ix_grid_space_namespace ON "{schema}".grid_space (namespace);


CREATE TABLE "{schema}".grid_table (
    id                   uuid PRIMARY KEY,
    namespace            varchar(255) NOT NULL DEFAULT '',
    space_id             uuid REFERENCES "{schema}".grid_space (id) ON DELETE SET NULL,
    slug                 varchar(128) NOT NULL,
    label                varchar(255) NOT NULL,
    ownership            varchar(64) NOT NULL DEFAULT 'owned',
    binding              jsonb,
    projection_predicate jsonb,
    natural_key          jsonb,
    is_system            boolean NOT NULL DEFAULT false,
    created_at           timestamp NOT NULL DEFAULT now(),
    updated_at           timestamp NOT NULL DEFAULT now(),
    created_by_id        uuid,
    updated_by_id        uuid,
    archived_at          timestamptz,
    is_active            boolean NOT NULL DEFAULT true,
    CONSTRAINT ck_grid_table_active_archive
        CHECK ((is_active AND archived_at IS NULL) OR (NOT is_active AND archived_at IS NOT NULL)),
    CONSTRAINT ck_grid_table_bound_requires_binding
        CHECK ((ownership <> 'bound') OR (binding IS NOT NULL))
);
CREATE UNIQUE INDEX uq_grid_table_active ON "{schema}".grid_table (namespace, slug) WHERE archived_at IS NULL;
CREATE INDEX ix_grid_table_namespace ON "{schema}".grid_table (namespace);
CREATE INDEX ix_grid_table_space_id ON "{schema}".grid_table (space_id);


CREATE TABLE "{schema}".grid_relation (
    id                uuid PRIMARY KEY,
    namespace         varchar(255) NOT NULL DEFAULT '',
    key               varchar(128) NOT NULL,
    source_table_id   uuid NOT NULL REFERENCES "{schema}".grid_table (id) ON DELETE CASCADE,
    target_table_id   uuid NOT NULL REFERENCES "{schema}".grid_table (id) ON DELETE RESTRICT,
    through_table_id  uuid REFERENCES "{schema}".grid_table (id) ON DELETE RESTRICT,
    relation_type     varchar(64) NOT NULL,
    on_delete         varchar(64) NOT NULL DEFAULT 'restrict',
    created_at        timestamp NOT NULL DEFAULT now(),
    updated_at        timestamp NOT NULL DEFAULT now(),
    created_by_id     uuid,
    updated_by_id     uuid,
    archived_at       timestamptz,
    is_active         boolean NOT NULL DEFAULT true,
    CONSTRAINT ck_grid_relation_active_archive
        CHECK ((is_active AND archived_at IS NULL) OR (NOT is_active AND archived_at IS NOT NULL)),
    CONSTRAINT ck_grid_relation_m2m_has_through
        CHECK (((relation_type = 'many_to_many') AND through_table_id IS NOT NULL)
            OR ((relation_type <> 'many_to_many') AND through_table_id IS NULL))
);
CREATE UNIQUE INDEX uq_grid_relation_active ON "{schema}".grid_relation (source_table_id, key) WHERE archived_at IS NULL;
CREATE INDEX ix_grid_relation_namespace ON "{schema}".grid_relation (namespace);
CREATE INDEX ix_grid_relation_source ON "{schema}".grid_relation (source_table_id);
CREATE INDEX ix_grid_relation_target ON "{schema}".grid_relation (target_table_id);
CREATE INDEX ix_grid_relation_through ON "{schema}".grid_relation (through_table_id);


CREATE TABLE "{schema}".grid_column (
    id                    uuid PRIMARY KEY,
    namespace             varchar(255) NOT NULL DEFAULT '',
    table_id              uuid NOT NULL REFERENCES "{schema}".grid_table (id) ON DELETE CASCADE,
    key                   varchar(128) NOT NULL,
    label                 varchar(255) NOT NULL,
    type_id               varchar(64) NOT NULL,
    cardinality           varchar(64) NOT NULL DEFAULT 'one',
    materialization       varchar(64) NOT NULL DEFAULT 'payload',
    promoted_column       varchar(128),
    derived_source        varchar(255),
    is_required           boolean NOT NULL DEFAULT false,
    is_unique             boolean NOT NULL DEFAULT false,
    default_value         jsonb,
    relation_id           uuid REFERENCES "{schema}".grid_relation (id) ON DELETE SET NULL,
    config                jsonb NOT NULL DEFAULT '{}'::jsonb,
    display_order         integer NOT NULL DEFAULT 0,
    created_at            timestamp NOT NULL DEFAULT now(),
    updated_at            timestamp NOT NULL DEFAULT now(),
    created_by_id         uuid,
    updated_by_id         uuid,
    archived_at           timestamptz,
    is_active             boolean NOT NULL DEFAULT true,
    CONSTRAINT ck_grid_column_active_archive
        CHECK ((is_active AND archived_at IS NULL) OR (NOT is_active AND archived_at IS NOT NULL)),
    CONSTRAINT ck_grid_column_ref_projects_relation
        CHECK (((type_id = 'ref') AND relation_id IS NOT NULL)
            OR ((type_id <> 'ref') AND relation_id IS NULL)),
    CONSTRAINT ck_grid_column_promoted_has_column
        CHECK (((materialization = 'promoted') AND promoted_column IS NOT NULL)
            OR ((materialization <> 'promoted') AND promoted_column IS NULL)),
    CONSTRAINT ck_grid_column_derived_has_source
        CHECK (((materialization = 'derived') AND derived_source IS NOT NULL)
            OR ((materialization <> 'derived') AND derived_source IS NULL))
);
CREATE UNIQUE INDEX uq_grid_column_active ON "{schema}".grid_column (table_id, key) WHERE archived_at IS NULL;
CREATE INDEX ix_grid_column_namespace ON "{schema}".grid_column (namespace);
CREATE INDEX ix_grid_column_table_id ON "{schema}".grid_column (table_id);
CREATE INDEX ix_grid_column_relation_id ON "{schema}".grid_column (relation_id);


CREATE TABLE "{schema}".grid_index (
    id              uuid PRIMARY KEY,
    namespace       varchar(255) NOT NULL DEFAULT '',
    table_id        uuid NOT NULL REFERENCES "{schema}".grid_table (id) ON DELETE CASCADE,
    column_keys     jsonb NOT NULL,
    index_kind      varchar(64) NOT NULL DEFAULT 'btree',
    is_unique       boolean NOT NULL DEFAULT false,
    physical_name   varchar(63),
    state           varchar(64) NOT NULL DEFAULT 'pending',
    created_at      timestamp NOT NULL DEFAULT now(),
    updated_at      timestamp NOT NULL DEFAULT now(),
    created_by_id   uuid,
    updated_by_id   uuid,
    archived_at     timestamptz,
    is_active       boolean NOT NULL DEFAULT true,
    CONSTRAINT ck_grid_index_active_archive
        CHECK ((is_active AND archived_at IS NULL) OR (NOT is_active AND archived_at IS NOT NULL))
);
CREATE UNIQUE INDEX uq_grid_index_active
    ON "{schema}".grid_index (namespace, table_id, (column_keys::text), index_kind)
    WHERE archived_at IS NULL;
CREATE INDEX ix_grid_index_namespace ON "{schema}".grid_index (namespace);
CREATE INDEX ix_grid_index_table_id ON "{schema}".grid_index (table_id);


-- ── Data plane ──────────────────────────────────────────────────────────────

CREATE TABLE "{schema}".grid_row (
    id              uuid PRIMARY KEY,
    namespace       varchar(255) NOT NULL DEFAULT '',
    table_id        uuid NOT NULL REFERENCES "{schema}".grid_table (id) ON DELETE CASCADE,
    payload         jsonb NOT NULL DEFAULT '{}'::jsonb,
    external_ref    uuid,
    created_at      timestamp NOT NULL DEFAULT now(),
    updated_at      timestamp NOT NULL DEFAULT now(),
    created_by_id   uuid,
    updated_by_id   uuid,
    archived_at     timestamptz,
    is_active       boolean NOT NULL DEFAULT true,
    CONSTRAINT ck_grid_row_active_archive
        CHECK ((is_active AND archived_at IS NULL) OR (NOT is_active AND archived_at IS NOT NULL))
);
CREATE INDEX ix_grid_row_namespace ON "{schema}".grid_row (namespace);
CREATE INDEX ix_grid_row_table ON "{schema}".grid_row (table_id);
CREATE INDEX ix_grid_row_payload_gin ON "{schema}".grid_row USING gin (payload jsonb_path_ops);
CREATE UNIQUE INDEX uq_grid_row_external_ref
    ON "{schema}".grid_row (table_id, external_ref) WHERE external_ref IS NOT NULL;


CREATE TABLE "{schema}".grid_edge (
    id            uuid PRIMARY KEY,
    namespace     varchar(255) NOT NULL DEFAULT '',
    relation_id   uuid NOT NULL REFERENCES "{schema}".grid_relation (id) ON DELETE CASCADE,
    source_row_id uuid NOT NULL REFERENCES "{schema}".grid_row (id) ON DELETE CASCADE,
    target_row_id uuid NOT NULL REFERENCES "{schema}".grid_row (id) ON DELETE CASCADE,
    payload       jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at    timestamp NOT NULL DEFAULT now(),
    updated_at    timestamp NOT NULL DEFAULT now(),
    created_by_id uuid,
    updated_by_id uuid,
    archived_at   timestamptz,
    is_active     boolean NOT NULL DEFAULT true,
    CONSTRAINT ck_grid_edge_active_archive
        CHECK ((is_active AND archived_at IS NULL) OR (NOT is_active AND archived_at IS NOT NULL)),
    CONSTRAINT uq_grid_edge_triple UNIQUE (relation_id, source_row_id, target_row_id)
);
CREATE INDEX ix_grid_edge_namespace ON "{schema}".grid_edge (namespace);
CREATE INDEX ix_grid_edge_relation ON "{schema}".grid_edge (relation_id);
CREATE INDEX ix_grid_edge_source ON "{schema}".grid_edge (namespace, source_row_id);
CREATE INDEX ix_grid_edge_target ON "{schema}".grid_edge (namespace, target_row_id);
