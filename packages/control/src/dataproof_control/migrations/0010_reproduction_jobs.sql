-- Reproduction requests and the queue that runs them.
--
-- Reproducing a failed check takes minutes in fresh environments, so a reviewer's request is
-- recorded (write-once, one per check of a run) and a worker runs it. The job row is the
-- mutable queue state: lease, attempts, and how it finished.

CREATE TABLE reproduction_requests (
    request_id   uuid PRIMARY KEY,
    run_id       uuid NOT NULL REFERENCES runs (run_id),
    check_id     text NOT NULL,
    requested_by text NOT NULL,
    requested_at timestamptz NOT NULL DEFAULT statement_timestamp(),
    UNIQUE (run_id, check_id)
);

CREATE TRIGGER reproduction_requests_write_once BEFORE UPDATE OR DELETE ON reproduction_requests
    FOR EACH STATEMENT EXECUTE FUNCTION run_results_are_write_once();

CREATE TABLE reproduction_jobs (
    request_id       uuid PRIMARY KEY REFERENCES reproduction_requests (request_id),
    attempts         integer NOT NULL DEFAULT 0 CHECK (attempts >= 0),
    max_attempts     integer NOT NULL CHECK (max_attempts > 0),
    lease_owner      text,
    lease_expires_at timestamptz,
    finished_at      timestamptz,
    reproduction_id  uuid REFERENCES run_reproductions (reproduction_id),
    failure          text,
    CHECK (reproduction_id IS NULL OR failure IS NULL),
    CHECK ((finished_at IS NULL) = (reproduction_id IS NULL AND failure IS NULL))
);

CREATE INDEX reproduction_jobs_pending ON reproduction_jobs (request_id)
    WHERE finished_at IS NULL;
