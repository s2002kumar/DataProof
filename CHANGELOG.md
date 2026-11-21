# Changelog

What changed for someone who uses DataProof. Pull request numbers are in this repository. A version exists only once its tag does; until then, pin a commit.

## Next: 1.0.0 (not tagged)

The first version to be tagged, once the gates in [RELEASE.md](docs/RELEASE.md) are met and the owner approves. Its known limitations are listed there.

### The GitHub Action (database tier)

- **Verdicts and exit codes.** `CHECKED` and `CHECKED_WITH_WARNINGS` exit 0, `BLOCKED` 1, `INCOMPLETE` 2 and `NOT_EXECUTED` 3. A run that could not be made exits 4 (`ERROR`) and says why. Nothing is green by omission (#86).
- **Trusted inputs from the base branch.** The configuration, oracles, fixture, requirements and binding approvals come from the base branch's tip, read from git objects. A pull request's edits to them are ignored and listed (#86).
- **Isolation.** The pull request's migration runs in a local-Docker step container with no token, no secrets and no network beyond its own disposable database. Its role cannot create roles or databases ([ADR-0007](docs/adr/0007-github-action-database-tier.md), #86).
- **Checks.** `migration.execute`, `migration.rerun` (C7), and for each oracle a data check and a type check, `types.<contract>` (C8) (#88, #90, #91).
- **Untrusted text cannot forge output.** Binding paths, relation names, contract ids and error text are escaped in the summary and folded to one line in the log, so no pull request can write a workflow command (#92).
- **Applications whose `env.py` reads its own settings.** `[alembic.environment]` on the base branch declares variables for both migration steps, with placeholders for the disposable database's coordinates. A package the base branch's requirements file names replaces DataProof's own pin for it (#93).
- **Changed files are counted from the merge base,** as GitHub's diff counts them. A branch behind its base is no longer credited with the base's later changes (#95).

### The GitHub App and its control plane

- Verified webhook intake, commit-bound check runs, GitHub sign-in for reviewers, binding decisions, a dashboard, a signed preview gateway on a separate origin per preview, and a deployment run from one configuration file (#49 to #57).
- The newest run's check run is created last on each commit, so a superseded run cannot shadow it under branch protection (#71, #73). Seen to hold on real GitHub on new commits, in a burst of approvals and with an older run turning green after a newer one (#78).
- Correction replay as a child run, reproduction of failed checks on a subset, and queued reproduction (#46, #48, #59, #68).
- A signed session is accepted only in the spelling it was issued in (#94).
- A stale pull request is no longer refused for a dependency change its base made (#95).

### Verification

- Protected oracles on the base branch, bindings proposed by the pull request and approved by digest, typed comparisons (`EXACT`, `DECIMAL`, `INSTANT`), sealed and restore-verified snapshots, and evidence manifests (#20, #21, #29, #40).
- API behaviour checks (C6) through live previews (#37, #39).

### AI, advisory only

- A bounded Failure Investigator and Change Analyst: allowlisted read-only tools, validated citations and hard budgets. An answer that fails validation is never shown, and neither role changes a verdict (#42 to #44, #75). Both are experimental and off unless an operator configures them. Their first live evaluation (2026-11-19) did not establish their value: the investigator answered in 1 of 18 investigations, stopped the other 17 times by its own tool-call limit, and the Change Analyst's model arm sent no request ([results](docs/AI_EVALUATION_RESULTS.md)).
- The investigator now finishes ([ADR-0009](docs/adr/0009-the-investigator-finishes.md)): it is told its budget and what is left, a call past the limit is answered "not run" and the answer is asked for, a sixth tool (`get_briefing`) returns what the run recorded in one call, the changed files are named, and a refusal says what to do instead. No bound changes, and the verifier is untouched. Not yet measured on held-out cases. An independent review before any live run found, among other things, that `read_source` returned its lines out of order, that an answer over a limit the model was never told was thrown away, that the cost limit could refuse the answer's own turn, and that a pull request's file names were written into the Change Analyst's task; each is fixed and held by a test (`failure-investigator/3`, `change-analyst/3`, `tools/3`). The development stage of the second evaluation then showed that no answer cited the briefing, though answers stated what only it showed; the prompt now says to (`failure-investigator/4`).

### Benchmark

- A seeded fault corpus with clean controls, fair baselines and a pipeline tier, which a test regenerates exactly (#60, #61, #65, #87).

### Dependencies

- Next.js 16.4.5 in the dashboard and in the sample app's web front end, which fixes [GHSA-mfr8-8qww-v76h](https://github.com/advisories/GHSA-mfr8-8qww-v76h) (moderate: a `'use cache'` function called through `unstable_cache()` could leak data across root params). Neither app uses `'use cache'` or `unstable_cache`, so neither was exposed; the upgrade clears the advisory.
