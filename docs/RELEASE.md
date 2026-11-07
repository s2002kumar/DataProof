# Releasing 1.0.0

What has to be true before `v1.0.0` is tagged, what is true now, and what the release will not claim. Tagging is the owner's decision. Nothing in this repository tags, publishes a GitHub release or lists the Action on the Marketplace.

1.0.0 covers both ways to use DataProof: **the GitHub App** with its control plane, and **the GitHub Action** for the database tier.

As of 2026-11-07.

## Gates

A gate is met only by evidence on the release commit, or on a commit whose Action and service code is identical to it, never by a run on an earlier one.

| Gate | Evidence a reader can check | Status |
| --- | --- | --- |
| Every CI job is green on the release commit | All nine jobs, not only the required ones, with no test skipped (`DATAPROOF_FORBID_SKIPS` fails a job that skips one) | **Met on `ba733cfe`**, the commit the field repositories pin: all nine jobs green, 778 tests in `python-tests` and 20 in `action-tests`, none skipped. To be checked again on the release commit |
| Every row of the [release security review](ACTION_SECURITY_REVIEW.md) holds | The review's tests run in `action-tests`, a required check | **Met on `ba733cfe`** (`action-tests` green) |
| A clean installation from the pinned commit | Repositories with no DataProof history pin the commit and get the verdicts predicted before the run ([field results](ACTION_FIELD_RESULTS.md)) | **Met at `ba733cfe`** on 2026-11-07: the example repository (#1 `CHECKED`, #2 `BLOCKED`) and the field fork of an application DataProof's author did not write (#1 `CHECKED`, #2 `BLOCKED`), every verdict as predicted |
| The App's check-run fix holds on real GitHub ([#78](https://github.com/s2002kumar/DataProof/issues/78)) | The live probe of superseded runs on one commit, with check-run links, in [DEMO.md](DEMO.md#check-run-finality) | **Not met.** The fix is tested against a simulated GitHub only. The probe is written (`scripts/demo/finality_probe.py`) and needs Docker on the owner's machine |
| The claims match the evidence | README, STATUS, the Action's metadata and this file say what the tests and runs show, and no more | Checked on 2026-11-07; corrections are in the pull request that adds this file |
| The owner approves the tag | A message from the owner | **Not given** |

## The open issues, classified

| Issue | Class | Why |
| --- | --- | --- |
| [#78](https://github.com/s2002kumar/DataProof/issues/78) the fixed check-run publisher against real GitHub | **Release blocker** | A required check that reads a superseded run is the App's worst failure. It is fixed and tested against a simulated GitHub. 1.0.0 includes the App, so the fix must be seen on real GitHub first |
| [#80](https://github.com/s2002kumar/DataProof/issues/80) external usability | **Documented limitation** | The Action ran on an application its author did not write ([field results](ACTION_FIELD_RESULTS.md)). Nobody but the author has installed it, so no adoption is claimed. The invitation is [EXTERNAL_TEST.md](EXTERNAL_TEST.md) |
| [#7](https://github.com/s2002kumar/DataProof/issues/7) the Change Analyst's value, and the live AI ablation in [#58](https://github.com/s2002kumar/DataProof/issues/58) | **Documented limitation** | Both AI roles are advisory and never change a verdict. Their value is unmeasured until the frozen evaluation runs ([AI_EVALUATION.md](AI_EVALUATION.md)), so 1.0.0 claims nothing for them |
| [#6](https://github.com/s2002kumar/DataProof/issues/6) the schema-diff comparator | **Documented limitation** | Optional, and not allowed to delay the release |
| [#13](https://github.com/s2002kumar/DataProof/issues/13) name clearance | **Owner decision before a launch** | It does not block a tag (owner, 2026-10-30). It does block a Marketplace listing or an announcement |
| [#5](https://github.com/s2002kumar/DataProof/issues/5), [#10](https://github.com/s2002kumar/DataProof/issues/10), [#11](https://github.com/s2002kumar/DataProof/issues/11) | **Cloud-gated** | They concern a hosted backend that is not built and not authorized. 1.0.0 is local Docker and GitHub-hosted runners only |

## Known limitations

These hold for 1.0.0 and go into its release notes.

- **What a verdict means.** A run shows that specific checks passed for an exact commit, fixture, configuration and engine version. It is not a safety guarantee for production data, locking, traffic or performance ([Verification](VERIFICATION.md#honest-limits)).
- **What no contract declares is not checked.** NULL and empty-string confusion is invisible to the declared contracts, which is why the benchmark misses 2 of 20 faults. No schema-diff tool has run (#6).
- **The benchmark was written by DataProof's author.** No third party has verified a result.
- **The AI roles are unmeasured.** No live model run has been made (#7, #58).
- **Nobody else has installed it** (#80).
- **The App runs on one machine.** Execution is local Docker, which shares the host kernel ([ADR-0006](adr/0006-pluggable-execution-backends.md)). There is no hosted service. The App does not execute pull requests from forks, and it supports one application profile.
- **The Action tests the head, not the merge.** The candidate runs the head commit's migrations on top of the base branch's tip. If the base branch gained a migration after the pull request branched, the head's Alembic cannot find that revision and the run is `BLOCKED` at `migration.execute`; update the branch and run again.
- **The Action installs only the base branch's requirements.** A pull request that changes what `env.py` imports is verified with the versions the base branch pins. Merge the dependency change first.
- **A pull request can edit the workflow that calls the Action.** Branch protection and code-owner review of `.github/` are the control ([ADR-0007](adr/0007-github-action-database-tier.md)).
- **Check runs belong to a commit.** Two pull requests with the same head commit share one latest `DataProof` check.
- **Supply chain.** The migration image's packages are pinned by version, not by hash ([security review](ACTION_SECURITY_REVIEW.md)).

## How to tag, once approved

Only after every gate above reads met and the owner has approved:

1. **A release pull request** sets the version to `1.0.0` (`VERSION` in `packages/action/src/dataproof_action/run.py`, which every report records, and each package's `pyproject.toml`), dates the changelog's next section, and re-pins the field repositories to its head. It merges on green CI like any other.
2. **Tag its merge commit:**

```bash
git switch main && git pull --ff-only
git tag -a v1.0.0 -m "DataProof 1.0.0" <release commit>
git push origin v1.0.0
gh release create v1.0.0 --title "DataProof 1.0.0" --notes-file <notes> --verify-tag
```

The release notes are the changelog's next section with its date, plus the known limitations above. No moving `v1` tag is created: callers pin a commit, and a tag can be moved after they resolve it.
