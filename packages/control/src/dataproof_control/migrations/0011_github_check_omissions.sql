-- Runs left without a check run of their own, because a newer run on the same commit reports.
--
-- A required check reads the DataProof check run created last on a commit (docs/DEMO.md,
-- "Check-run finality"). A check run created for a run after a newer run's would be the one it
-- reads, so a run that has not reached GitHub by the time a newer pull request run exists on its
-- commit is never given one. Which newer run existed is recorded once; the run is then never
-- reconsidered. Its report and evidence are unchanged in the control plane.

CREATE TABLE github_check_omissions (
    run_id       uuid PRIMARY KEY REFERENCES runs (run_id),
    newer_run_id uuid NOT NULL REFERENCES runs (run_id),
    recorded_at  timestamptz NOT NULL DEFAULT statement_timestamp(),
    CHECK (newer_run_id <> run_id)
);

CREATE TRIGGER github_check_omissions_write_once BEFORE UPDATE OR DELETE ON github_check_omissions
    FOR EACH STATEMENT EXECUTE FUNCTION run_results_are_write_once();
