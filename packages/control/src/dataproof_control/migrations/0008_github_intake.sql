-- GitHub intake: every webhook delivery that passed signature verification, with what intake did
-- about it, and the pull request facts that GitHub reported for each head.

CREATE TABLE github_deliveries (
    delivery_id text PRIMARY KEY CHECK (length(delivery_id) BETWEEN 1 AND 100),
    event       text NOT NULL,
    action      text,
    repository  text,
    outcome     text NOT NULL CHECK (
        outcome IN ('RUN_CREATED', 'RUN_EXISTS', 'RUNS_CANCELLED', 'IGNORED', 'REFUSED')
    ),
    run_id      uuid REFERENCES runs (run_id),
    detail      jsonb NOT NULL DEFAULT '{}',
    received_at timestamptz NOT NULL DEFAULT statement_timestamp(),
    CHECK ((outcome IN ('RUN_CREATED', 'RUN_EXISTS')) = (run_id IS NOT NULL))
);

CREATE TRIGGER github_deliveries_write_once BEFORE UPDATE OR DELETE ON github_deliveries
    FOR EACH STATEMENT EXECUTE FUNCTION run_results_are_write_once();

-- Who opened the pull request matters to the approver rule (ADR-0005); where the head was
-- pushed decides whether it is a fork. Recorded per head, as GitHub reported it.
CREATE TABLE github_pull_request_heads (
    repository      text NOT NULL,
    pull_request    integer NOT NULL CHECK (pull_request > 0),
    head_sha        text NOT NULL CHECK (head_sha ~ '^[0-9a-f]{40}$'),
    head_repository text NOT NULL,
    author          text NOT NULL,
    installation_id bigint NOT NULL,
    recorded_at     timestamptz NOT NULL DEFAULT statement_timestamp(),
    PRIMARY KEY (repository, pull_request, head_sha)
);

CREATE TRIGGER github_pull_request_heads_write_once
    BEFORE UPDATE OR DELETE ON github_pull_request_heads
    FOR EACH STATEMENT EXECUTE FUNCTION run_results_are_write_once();
