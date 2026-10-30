-- Run results: staged sources, executed steps, check results, query evidence and the report.
--
-- Rows are written by a worker in the same transaction as the transition that records the stage,
-- so a stage's results exist exactly when the run has moved past that stage. They are never
-- rewritten: a correction or a new decision is a new run.

CREATE FUNCTION run_results_are_write_once() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION '% is write-once', TG_TABLE_NAME;
END;
$$;

CREATE TABLE run_sources (
    run_id      uuid NOT NULL REFERENCES runs (run_id),
    environment text NOT NULL CHECK (environment IN ('REFERENCE', 'CANDIDATE')),
    commit_sha  text NOT NULL CHECK (commit_sha ~ '^[0-9a-f]{40}$'),
    sha256      text NOT NULL CHECK (sha256 ~ '^[0-9a-f]{64}$'),
    size_bytes  bigint NOT NULL CHECK (size_bytes > 0),
    object_key  text NOT NULL,
    PRIMARY KEY (run_id, environment)
);

CREATE TABLE run_steps (
    run_id        uuid NOT NULL REFERENCES runs (run_id),
    step_id       text NOT NULL,
    environment   text NOT NULL CHECK (environment IN ('REFERENCE', 'CANDIDATE')),
    kind          text NOT NULL,
    source_sha256 text NOT NULL CHECK (source_sha256 ~ '^[0-9a-f]{64}$'),
    exit_code     integer,
    timed_out     boolean NOT NULL,
    duration_ms   integer NOT NULL CHECK (duration_ms >= 0),
    output        text NOT NULL,
    recorded_at   timestamptz NOT NULL DEFAULT statement_timestamp(),
    PRIMARY KEY (run_id, step_id),
    CHECK (timed_out = (exit_code IS NULL))
);

CREATE TABLE run_checks (
    run_id           uuid NOT NULL REFERENCES runs (run_id),
    check_id         text NOT NULL,
    required         boolean NOT NULL,
    outcome          text NOT NULL CHECK (outcome IN ('PASS', 'FAIL', 'ERROR', 'NOT_RUN')),
    result           jsonb NOT NULL,
    binding_analysis jsonb,
    binding_problem  text,
    PRIMARY KEY (run_id, check_id),
    CHECK (result ->> 'check_id' = check_id),
    CHECK (result ->> 'outcome' = outcome),
    CHECK ((result -> 'required')::boolean = required)
);

CREATE TABLE run_evidence (
    run_id      uuid NOT NULL REFERENCES runs (run_id),
    evidence_id text NOT NULL,
    record      jsonb NOT NULL,
    PRIMARY KEY (run_id, evidence_id)
);

CREATE TABLE run_reports (
    run_id     uuid PRIMARY KEY REFERENCES runs (run_id),
    verdict    text NOT NULL
        CHECK (verdict IN ('BLOCKED', 'INCOMPLETE', 'NOT_EXECUTED', 'CHECKED',
                           'CHECKED_WITH_WARNINGS')),
    conclusion text NOT NULL CHECK (conclusion IN ('failure', 'action_required', 'neutral',
                                                   'success')),
    report     jsonb NOT NULL,
    created_at timestamptz NOT NULL DEFAULT statement_timestamp()
);

CREATE TRIGGER run_sources_write_once BEFORE UPDATE OR DELETE ON run_sources
    FOR EACH STATEMENT EXECUTE FUNCTION run_results_are_write_once();
CREATE TRIGGER run_steps_write_once BEFORE UPDATE OR DELETE ON run_steps
    FOR EACH STATEMENT EXECUTE FUNCTION run_results_are_write_once();
CREATE TRIGGER run_checks_write_once BEFORE UPDATE OR DELETE ON run_checks
    FOR EACH STATEMENT EXECUTE FUNCTION run_results_are_write_once();
CREATE TRIGGER run_evidence_write_once BEFORE UPDATE OR DELETE ON run_evidence
    FOR EACH STATEMENT EXECUTE FUNCTION run_results_are_write_once();
CREATE TRIGGER run_reports_write_once BEFORE UPDATE OR DELETE ON run_reports
    FOR EACH STATEMENT EXECUTE FUNCTION run_results_are_write_once();
