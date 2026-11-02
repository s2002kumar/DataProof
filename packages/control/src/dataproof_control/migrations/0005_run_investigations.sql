-- Investigations: advisory explanations of a run's failures, by the model-off baseline and, when
-- one is configured, by a model. They sit beside the checks and never change them.

CREATE TABLE run_investigations (
    investigation_id uuid PRIMARY KEY,
    run_id           uuid NOT NULL REFERENCES runs (run_id),
    model            text NOT NULL,
    status           text NOT NULL
        CHECK (status IN ('COMPLETED', 'NOT_AVAILABLE', 'INVALID_OUTPUT', 'BUDGET_EXHAUSTED')),
    investigation    jsonb NOT NULL,
    recorded_at      timestamptz NOT NULL DEFAULT statement_timestamp(),
    CHECK (investigation ->> 'investigation_id' = investigation_id::text),
    CHECK (investigation ->> 'run_id' = run_id::text),
    CHECK (investigation ->> 'status' = status),
    CHECK (investigation ->> 'model' = model)
);

CREATE INDEX run_investigations_by_run ON run_investigations (run_id);

CREATE TRIGGER run_investigations_write_once BEFORE UPDATE OR DELETE ON run_investigations
    FOR EACH STATEMENT EXECUTE FUNCTION run_results_are_write_once();
