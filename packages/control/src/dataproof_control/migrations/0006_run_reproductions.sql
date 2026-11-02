-- Reproductions: a failed check replayed on a small FK-closure subset of the baseline in fresh
-- environments. Advisory evidence beside the run; it never changes the run's results.

CREATE TABLE run_reproductions (
    reproduction_id uuid PRIMARY KEY,
    run_id          uuid NOT NULL REFERENCES runs (run_id),
    check_id        text NOT NULL,
    status          text NOT NULL CHECK (status IN ('REPRODUCED', 'NOT_REPRODUCED')),
    reproduction    jsonb NOT NULL,
    recorded_at     timestamptz NOT NULL DEFAULT statement_timestamp(),
    CHECK (reproduction ->> 'reproduction_id' = reproduction_id::text),
    CHECK (reproduction ->> 'run_id' = run_id::text),
    CHECK (reproduction ->> 'check_id' = check_id),
    CHECK (reproduction ->> 'status' = status)
);

CREATE INDEX run_reproductions_by_run ON run_reproductions (run_id);

CREATE TRIGGER run_reproductions_write_once BEFORE UPDATE OR DELETE ON run_reproductions
    FOR EACH STATEMENT EXECUTE FUNCTION run_results_are_write_once();
