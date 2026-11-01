-- Live previews: one row per environment whose application a run built and served, with what
-- it cost (ADR-0004: candidate-task memory, CPU and build time are a measured gate). The preview
-- token itself is never stored here, only its digest.

CREATE TABLE run_previews (
    run_id            uuid NOT NULL REFERENCES runs (run_id),
    environment       text NOT NULL CHECK (environment IN ('REFERENCE', 'CANDIDATE')),
    commit_sha        text NOT NULL CHECK (commit_sha ~ '^[0-9a-f]{40}$'),
    ready             boolean NOT NULL,
    failure           text CHECK (failure IN ('build', 'start')),
    url               text,
    token_sha256      text CHECK (token_sha256 ~ '^[0-9a-f]{64}$'),
    ready_seconds     numeric(10, 1) NOT NULL CHECK (ready_seconds >= 0),
    build_seconds     numeric(10, 1) CHECK (build_seconds >= 0),
    peak_memory_bytes bigint NOT NULL CHECK (peak_memory_bytes >= 0),
    cpu_seconds       numeric(12, 1) NOT NULL CHECK (cpu_seconds >= 0),
    output            text NOT NULL,
    recorded_at       timestamptz NOT NULL DEFAULT statement_timestamp(),
    PRIMARY KEY (run_id, environment),
    CHECK (ready = (failure IS NULL)),
    CHECK (ready = (url IS NOT NULL)),
    CHECK ((url IS NULL) = (token_sha256 IS NULL))
);

CREATE TRIGGER run_previews_write_once BEFORE UPDATE OR DELETE ON run_previews
    FOR EACH STATEMENT EXECUTE FUNCTION run_results_are_write_once();
