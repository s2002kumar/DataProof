-- Project configurations by digest, the human decisions recorded about runs, and the binding
-- each check used.
--
-- A run pins its configuration by digest (RunInputs.config_digest). Storing every configuration
-- under its digest lets a worker load exactly the one a run was created under, so runs whose
-- configurations differ (a binding approval changes the digest) can share one worker.

CREATE TABLE project_configs (
    config_digest text PRIMARY KEY CHECK (config_digest ~ '^[0-9a-f]{64}$'),
    repository    text NOT NULL,
    config        jsonb NOT NULL,
    recorded_at   timestamptz NOT NULL DEFAULT statement_timestamp(),
    CHECK (config ->> 'repository' = repository)
);

CREATE TRIGGER project_configs_write_once BEFORE UPDATE OR DELETE ON project_configs
    FOR EACH STATEMENT EXECUTE FUNCTION run_results_are_write_once();

-- A human decision never edits a check result. A binding decision is bound to the head SHA of
-- the run decided on and to the binding's digest; the first decision on a binding at a head
-- stands. An approval or a correction request starts a new run, recorded with the decision.
CREATE TABLE human_decisions (
    decision_id    uuid PRIMARY KEY,
    run_id         uuid NOT NULL REFERENCES runs (run_id),
    repository     text NOT NULL,
    head_sha       text NOT NULL,
    action         text NOT NULL CHECK (
        action IN ('APPROVE_BINDING', 'REJECT_BINDING', 'REQUEST_CORRECTION_REPLAY', 'CANCEL_RUN')
    ),
    actor          text NOT NULL,
    binding_digest text,
    started_run_id uuid REFERENCES runs (run_id),
    decision       jsonb NOT NULL,
    recorded_at    timestamptz NOT NULL DEFAULT statement_timestamp(),
    CHECK (decision ->> 'decision_id' = decision_id::text),
    CHECK (decision ->> 'run_id' = run_id::text),
    CHECK (decision ->> 'head_sha' = head_sha),
    CHECK (decision ->> 'action' = action),
    CHECK (decision ->> 'actor' = actor),
    CHECK ((decision ->> 'binding_digest') IS NOT DISTINCT FROM binding_digest),
    CHECK (
        (action IN ('APPROVE_BINDING', 'REQUEST_CORRECTION_REPLAY')) = (started_run_id IS NOT NULL)
    )
);

CREATE INDEX human_decisions_by_run ON human_decisions (run_id);

CREATE UNIQUE INDEX human_decisions_one_per_binding
    ON human_decisions (repository, head_sha, binding_digest)
    WHERE binding_digest IS NOT NULL;

CREATE TRIGGER human_decisions_write_once BEFORE UPDATE OR DELETE ON human_decisions
    FOR EACH STATEMENT EXECUTE FUNCTION run_results_are_write_once();

-- The candidate binding each check was evaluated against or is waiting on, so that a reviewer
-- decides on exactly what the run used, and a decision can be checked against it.
ALTER TABLE run_checks ADD COLUMN binding jsonb;
