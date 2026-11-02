-- Commit-bound GitHub check runs, published from the control database.
--
-- Each pull request run gets one check run on its exact head commit. Every status published to
-- it (queued, in progress, completed with a conclusion) is recorded once, after GitHub accepted
-- it, so a publisher that crashed or lost a race publishes again only what is missing.

CREATE TABLE github_check_runs (
    run_id       uuid PRIMARY KEY REFERENCES runs (run_id),
    repository   text NOT NULL,
    head_sha     text NOT NULL CHECK (head_sha ~ '^[0-9a-f]{40}$'),
    check_run_id bigint NOT NULL CHECK (check_run_id > 0),
    created_at   timestamptz NOT NULL DEFAULT statement_timestamp()
);

CREATE TRIGGER github_check_runs_write_once BEFORE UPDATE OR DELETE ON github_check_runs
    FOR EACH STATEMENT EXECUTE FUNCTION run_results_are_write_once();

CREATE TABLE github_check_publications (
    run_id       uuid NOT NULL REFERENCES github_check_runs (run_id),
    status       text NOT NULL CHECK (status IN ('queued', 'in_progress', 'completed')),
    conclusion   text,
    published_at timestamptz NOT NULL DEFAULT statement_timestamp(),
    PRIMARY KEY (run_id, status),
    CHECK ((status = 'completed') = (conclusion IS NOT NULL))
);

CREATE TRIGGER github_check_publications_write_once
    BEFORE UPDATE OR DELETE ON github_check_publications
    FOR EACH STATEMENT EXECUTE FUNCTION run_results_are_write_once();
