-- Sealed evidence: pg_dump snapshots of both environments, their restore verification, and the
-- run's evidence manifest. Written once, before the run is reported, so that diagnosis and
-- reproduction never depend on an environment that cleanup destroys.

CREATE TABLE run_snapshots (
    run_id           uuid NOT NULL REFERENCES runs (run_id),
    environment      text NOT NULL CHECK (environment IN ('REFERENCE', 'CANDIDATE')),
    object_key       text NOT NULL,
    sha256           text NOT NULL CHECK (sha256 ~ '^[0-9a-f]{64}$'),
    size_bytes       bigint NOT NULL CHECK (size_bytes > 0),
    captured_at      timestamptz NOT NULL,
    database_digest  text NOT NULL CHECK (database_digest ~ '^[0-9a-f]{64}$'),
    restored_digest  text CHECK (restored_digest ~ '^[0-9a-f]{64}$'),
    restore_verified boolean NOT NULL,
    verified_at      timestamptz NOT NULL,
    restore_error    text,
    PRIMARY KEY (run_id, environment),
    -- Verified means the restored database has exactly the rows the live one had.
    CHECK (restore_verified = coalesce(restored_digest = database_digest, false)),
    CHECK ((restore_error IS NULL) = (restored_digest IS NOT NULL))
);

CREATE TABLE run_manifests (
    run_id     uuid PRIMARY KEY REFERENCES runs (run_id),
    object_key text NOT NULL,
    sha256     text NOT NULL CHECK (sha256 ~ '^[0-9a-f]{64}$'),
    manifest   jsonb NOT NULL,
    sealed_at  timestamptz NOT NULL,
    CHECK (manifest ->> 'run_id' = run_id::text)
);

CREATE TRIGGER run_snapshots_write_once BEFORE UPDATE OR DELETE ON run_snapshots
    FOR EACH STATEMENT EXECUTE FUNCTION run_results_are_write_once();
CREATE TRIGGER run_manifests_write_once BEFORE UPDATE OR DELETE ON run_manifests
    FOR EACH STATEMENT EXECUTE FUNCTION run_results_are_write_once();
