-- Run lifecycle: run records, the append-only transition log and the durable job queue.

CREATE TABLE run_states (
    state    text PRIMARY KEY,
    terminal boolean NOT NULL
);

INSERT INTO run_states (state, terminal) VALUES
    ('QUEUED', false), ('FETCHING', false), ('ANALYZING', false), ('PROVISIONING', false),
    ('MIGRATING', false), ('VERIFYING', false), ('INVESTIGATING', false), ('REPRODUCING', false),
    ('REPORTING', false), ('READY_FOR_REVIEW', false), ('CLEANING', false), ('ERROR', false),
    ('CANCELLED', false), ('TIMED_OUT', false),
    ('COMPLETE', true), ('SUPERSEDED', true), ('NOT_EXECUTED', true);

-- Mirrors dataproof_control.lifecycle.TRANSITIONS (a test asserts they are identical). The
-- transition log references it, so the database itself rejects an illegal transition.
CREATE TABLE run_state_transitions (
    from_state text NOT NULL REFERENCES run_states (state),
    to_state   text NOT NULL REFERENCES run_states (state),
    PRIMARY KEY (from_state, to_state)
);

INSERT INTO run_state_transitions (from_state, to_state) VALUES
    ('QUEUED', 'FETCHING'),
    ('FETCHING', 'ANALYZING'),
    ('ANALYZING', 'PROVISIONING'), ('ANALYZING', 'NOT_EXECUTED'),
    ('PROVISIONING', 'MIGRATING'),
    ('MIGRATING', 'VERIFYING'),
    ('VERIFYING', 'INVESTIGATING'), ('VERIFYING', 'REPORTING'),
    ('INVESTIGATING', 'REPRODUCING'), ('INVESTIGATING', 'REPORTING'),
    ('REPRODUCING', 'REPORTING'),
    ('REPORTING', 'READY_FOR_REVIEW'),
    ('READY_FOR_REVIEW', 'CLEANING'), ('READY_FOR_REVIEW', 'CANCELLED'),
    ('CLEANING', 'COMPLETE'), ('CLEANING', 'SUPERSEDED'),
    ('ERROR', 'CLEANING'), ('CANCELLED', 'CLEANING'), ('TIMED_OUT', 'CLEANING');

-- Every working state may be interrupted.
INSERT INTO run_state_transitions (from_state, to_state)
SELECT working.state, interruption.state
FROM (VALUES ('QUEUED'), ('FETCHING'), ('ANALYZING'), ('PROVISIONING'), ('MIGRATING'),
             ('VERIFYING'), ('INVESTIGATING'), ('REPRODUCING'), ('REPORTING')) AS working (state)
CROSS JOIN (VALUES ('ERROR'), ('CANCELLED'), ('TIMED_OUT')) AS interruption (state);

CREATE TABLE runs (
    run_id              uuid PRIMARY KEY,
    input_digest        text NOT NULL UNIQUE CHECK (input_digest ~ '^[0-9a-f]{64}$'),
    inputs              jsonb NOT NULL,
    repository          text NOT NULL,
    pull_request        integer CHECK (pull_request > 0),
    head_sha            text NOT NULL,
    trigger             text NOT NULL
        CHECK (trigger IN ('webhook', 'manual', 'binding_approval', 'correction_replay')),
    parent_run_id       uuid REFERENCES runs (run_id),
    state               text NOT NULL REFERENCES run_states (state),
    error_code          text,
    not_executed_reason text,
    superseded_by       uuid REFERENCES runs (run_id),
    isolation_id        text,
    deadline_at         timestamptz NOT NULL,
    created_at          timestamptz NOT NULL,
    updated_at          timestamptz NOT NULL,
    -- The denormalized columns used for supersession must agree with the pinned inputs.
    CHECK (repository = inputs ->> 'repository'),
    CHECK (head_sha = inputs ->> 'head_sha'),
    CHECK (pull_request IS NOT DISTINCT FROM (inputs ->> 'pull_request')::integer),
    CHECK ((trigger IN ('binding_approval', 'correction_replay')) = (parent_run_id IS NOT NULL)),
    CHECK (parent_run_id <> run_id),
    CHECK ((state = 'NOT_EXECUTED') = (not_executed_reason IS NOT NULL)),
    CHECK (state <> 'SUPERSEDED' OR superseded_by IS NOT NULL),
    CHECK (superseded_by <> run_id),
    CHECK (created_at <= updated_at)
);

CREATE INDEX runs_open_by_pull_request ON runs (repository, pull_request)
    WHERE state NOT IN ('COMPLETE', 'SUPERSEDED', 'NOT_EXECUTED');

CREATE TABLE run_transitions (
    run_id        uuid NOT NULL REFERENCES runs (run_id),
    seq           integer NOT NULL CHECK (seq > 0),
    from_state    text,
    to_state      text NOT NULL REFERENCES run_states (state),
    actor         text NOT NULL CHECK (actor <> ''),
    fencing_token bigint,
    detail        jsonb NOT NULL DEFAULT '{}'::jsonb,
    at            timestamptz NOT NULL,
    PRIMARY KEY (run_id, seq),
    FOREIGN KEY (from_state, to_state) REFERENCES run_state_transitions (from_state, to_state),
    CHECK ((from_state IS NULL) = (seq = 1)),
    CHECK (from_state IS NOT NULL OR to_state = 'QUEUED')
);

CREATE FUNCTION run_transitions_are_append_only() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'run_transitions is append-only';
END;
$$;

CREATE TRIGGER run_transitions_append_only
    BEFORE UPDATE OR DELETE ON run_transitions
    FOR EACH STATEMENT EXECUTE FUNCTION run_transitions_are_append_only();

-- One row per non-terminal run: the run needs a worker. A claim takes the row with
-- FOR UPDATE SKIP LOCKED, increments the fencing token and the attempt counter, and leases it.
-- Every worker write names the token, so a worker whose lease expired or was revoked (by a
-- cancellation, a timeout or a supersession) can no longer change the run.
CREATE TABLE run_jobs (
    run_id            uuid PRIMARY KEY REFERENCES runs (run_id),
    available_at      timestamptz NOT NULL,
    attempts          integer NOT NULL DEFAULT 0 CHECK (attempts >= 0),
    max_attempts      integer NOT NULL CHECK (max_attempts > 0),
    fencing_token     bigint NOT NULL DEFAULT 0 CHECK (fencing_token >= 0),
    lease_owner       text,
    lease_acquired_at timestamptz,
    heartbeat_at      timestamptz,
    lease_expires_at  timestamptz,
    last_error        text,
    CHECK (
        (lease_owner IS NULL AND lease_acquired_at IS NULL AND heartbeat_at IS NULL
            AND lease_expires_at IS NULL)
        OR (lease_owner IS NOT NULL AND lease_acquired_at <= heartbeat_at
            AND heartbeat_at < lease_expires_at)
    )
);

CREATE INDEX run_jobs_by_availability ON run_jobs (available_at);
