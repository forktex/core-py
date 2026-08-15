-- Complete forktex_core.flow schema — all tables, indexes, and constraints
-- in one migration. Applied once on fresh install; subsequent migrations
-- (if any) add only additive DDL. The {schema} placeholder is substituted
-- by the runner (a plain string replace, not str.format — literal `{`/`}`
-- like the JSONB defaults below are left untouched) at apply time.

-- ── Workflow registry ────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS {schema}.workflow (
    name             VARCHAR(255) NOT NULL,
    version          INTEGER      NOT NULL,
    ast_hash         VARCHAR(64),
    registered_at    TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    PRIMARY KEY (name, version)
);

-- ── Run — one row per flow.run() ─────────────────────────────────────
CREATE TABLE IF NOT EXISTS {schema}.run (
    id                 UUID         PRIMARY KEY,
    workflow_name      VARCHAR(255) NOT NULL,
    workflow_version   INTEGER      NOT NULL,
    status             VARCHAR(16)  NOT NULL,
    input              JSONB        NOT NULL DEFAULT '{}'::jsonb,
    output             JSONB,
    error              TEXT,
    metadata           JSONB        NOT NULL DEFAULT '{}'::jsonb,
    triggered_by       VARCHAR(32)  NOT NULL DEFAULT 'manual',
    started_at         TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    finished_at        TIMESTAMPTZ,
    cancelled_at       TIMESTAMPTZ,
    cancel_reason      TEXT,
    CONSTRAINT ck_run_status
        CHECK (status IN ('pending','running','completed','failed','cancelled')),
    CONSTRAINT ck_run_triggered_by
        CHECK (triggered_by IN ('manual','schedule','replay'))
);

CREATE INDEX IF NOT EXISTS ix_run_workflow_name  ON {schema}.run (workflow_name);
CREATE INDEX IF NOT EXISTS ix_run_status         ON {schema}.run (status);
CREATE INDEX IF NOT EXISTS ix_run_started_at     ON {schema}.run (started_at DESC);
CREATE INDEX IF NOT EXISTS ix_run_metadata_gin   ON {schema}.run USING GIN (metadata);
-- Payload filtering via flow.query().state(**kv)
CREATE INDEX IF NOT EXISTS ix_run_input_gin      ON {schema}.run USING GIN (input jsonb_path_ops);

-- ── Step run — one row per durable @step invocation ──────────────────
CREATE TABLE IF NOT EXISTS {schema}.step_run (
    id                 UUID         PRIMARY KEY,
    run_id             UUID         NOT NULL REFERENCES {schema}.run(id) ON DELETE CASCADE,
    step_name          VARCHAR(255) NOT NULL,
    step_qualname      VARCHAR(512) NOT NULL,
    step_index         INTEGER      NOT NULL,
    args_hash          VARCHAR(64)  NOT NULL,
    status             VARCHAR(16)  NOT NULL,
    output             JSONB,
    error              TEXT,
    attempts           INTEGER      NOT NULL DEFAULT 0,
    max_attempts       INTEGER      NOT NULL,
    next_attempt_at    TIMESTAMPTZ,
    started_at         TIMESTAMPTZ,
    finished_at        TIMESTAMPTZ,
    heartbeat_at       TIMESTAMPTZ,
    CONSTRAINT ck_step_run_status
        CHECK (status IN ('pending','running','completed','failed','cancelled')),
    CONSTRAINT uq_step_run_identity UNIQUE (run_id, step_qualname, args_hash)
);

CREATE INDEX IF NOT EXISTS ix_step_run_status_heartbeat
    ON {schema}.step_run (status, heartbeat_at);
CREATE INDEX IF NOT EXISTS ix_step_run_run_id_index
    ON {schema}.step_run (run_id, step_index);

-- ── Run event — append-only observability log ─────────────────────────
CREATE TABLE IF NOT EXISTS {schema}.run_event (
    id                 BIGSERIAL    PRIMARY KEY,
    run_id             UUID         NOT NULL REFERENCES {schema}.run(id) ON DELETE CASCADE,
    ts                 TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    event_type         VARCHAR(64)  NOT NULL,
    payload            JSONB        NOT NULL DEFAULT '{}'::jsonb
);
CREATE INDEX IF NOT EXISTS ix_run_event_run_id_ts ON {schema}.run_event (run_id, ts);

-- ── Scheduled run — @flow.scheduled state ────────────────────────────
CREATE TABLE IF NOT EXISTS {schema}.scheduled_run (
    workflow_name      VARCHAR(255) NOT NULL,
    workflow_version   INTEGER      NOT NULL,
    cron               VARCHAR(128) NOT NULL,
    enabled            BOOLEAN      NOT NULL DEFAULT TRUE,
    last_fired_at      TIMESTAMPTZ,
    next_fire_at       TIMESTAMPTZ  NOT NULL,
    PRIMARY KEY (workflow_name, workflow_version),
    FOREIGN KEY (workflow_name, workflow_version)
        REFERENCES {schema}.workflow(name, version) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS ix_scheduled_run_enabled_next
    ON {schema}.scheduled_run (enabled, next_fire_at);

-- ── Signal — ctx.send() / wait_edge inbox ─────────────────────────────
CREATE TABLE IF NOT EXISTS {schema}.signal (
    id            BIGSERIAL    PRIMARY KEY,
    run_id        UUID         NOT NULL REFERENCES {schema}.run(id) ON DELETE CASCADE,
    signal_name   VARCHAR(128) NOT NULL,
    payload       JSONB,
    sent_at       TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    consumed_at   TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS ix_signal_run_pending
    ON {schema}.signal (run_id, signal_name, consumed_at);

-- ── Namespace-track workflow definitions ─────────────────────────────
-- Platform-track definitions live in code; only namespace-track ones
-- are stored here so they survive restarts and can be managed via API.
CREATE TABLE IF NOT EXISTS {schema}.workflow_definition (
    id          UUID         PRIMARY KEY DEFAULT gen_random_uuid(),
    name        VARCHAR(255) NOT NULL,
    version     INTEGER      NOT NULL,
    namespace   TEXT         NOT NULL,
    type        VARCHAR(16)  NOT NULL,
    config      JSONB        NOT NULL,
    created_at  TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    updated_at  TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    CONSTRAINT uq_workflow_definition_identity UNIQUE (name, version, namespace),
    CONSTRAINT ck_workflow_definition_type CHECK (type IN ('pipeline', 'graph', 'scheduled'))
);

CREATE INDEX IF NOT EXISTS ix_workflow_definition_namespace
    ON {schema}.workflow_definition (namespace);
