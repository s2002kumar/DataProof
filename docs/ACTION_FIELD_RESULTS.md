# The Action in the field

Every real run of the Action outside its own tests, in order, including the ones that failed. Timings are as measured, from the Actions page or a clock; nothing here is rounded to a target. A run by someone other than the author is marked **external**; until one exists, no claim of external adoption is made ([#80](https://github.com/s2002kumar/DataProof/issues/80)).

## Timings so far

| Date | Where | What | Result | Measured | Notes |
| --- | --- | --- | --- | --- | --- |
| 2026-11-04 | This repository, local (Windows 11, Docker Desktop, 22 CPUs) | `tests/integration/test_action.py`: 11 runs of the Action in a row, the first building the migration image | 11 passed | 7 min 14 s for the suite, about 35 s to 1 min 40 s a run; the first `CHECKED` run, image build included, 1 min 43 s | Each run provisions two PostgreSQL containers and runs three migration steps |
| 2026-11-04 | This repository, GitHub Actions (`ubuntu-24.04`) | CI job `action-tests`: the same 11 runs | passed | 2 min 0 s for the job | Faster than the laptop; image layers were not cached between runs |
| 2026-11-04 | This repository, GitHub Actions, pull request #86 | The first `dataproof` workflow run: the Action on the pull request that introduces its own configuration | `Not verified (ERROR)`, exit 4, by design | 10 s to the refusal; `uv sync` of the Action's 21 packages 8 ms (cached) | The configuration is read from `main`, where it did not exist yet. Recorded as the bootstrapping rule in the quickstart |
| 2026-11-04 | [dataproof-example-subscriptions#1](https://github.com/s2002kumar/dataproof-example-subscriptions/pull/1), the correct migration, first run | The Action pinned to `2cabbc16`, on a repository that is not this one | `INCOMPLETE`, exit 2: the binding awaits approval | Job 31 s (image build, two PostgreSQL containers, three migration steps of about 0.8 s each, verification, cleanup); 21.7 s inside the Action; 40 s from trigger to result | The annotation names the digest to approve. `setup-uv` warned that its cache glob matched nothing in the caller's repository; fixed in the next commit of the Action |
| 2026-11-04 | [dataproof-example-subscriptions#2](https://github.com/s2002kumar/dataproof-example-subscriptions/pull/2), the corrupt migration, first run | Same | `INCOMPLETE`, exit 2 | 42 s from trigger to result | Same binding text, same digest |
| 2026-11-04 | [dataproof-example-subscriptions#3](https://github.com/s2002kumar/dataproof-example-subscriptions/pull/3), the approval (a one-line change to `dataproof/config.toml`) | Same | `CHECKED`, exit 0 | about 30 s | Reference and candidate identical; nothing to bind |
| 2026-11-04 | #1 re-run after the approval merged | Same workflow, same commit, re-run from the Actions page | **`CHECKED`**, exit 0 | Job 30 s | The Action read `main`'s new tip (`bbbc1a73`) although the event still recorded the old one (`98c5cd13`): the approval was seen without a new commit on the pull request |
| 2026-11-04 | #2 re-run after the approval merged | Same | **`BLOCKED`**, exit 1 | Job 40 s; 22.1 s inside the Action | `subscriptions.invoice_amounts` failed with 396 mismatches over 187 invoices, for example invoice 5003: subtotal 19.99 → 19.00, tax 2.59 → 2.00, total 22.58 → 22.00. The other three checks passed. The rule-based diagnosis: "values equal the reference values rounded to whole units: a cast or column type lost the fractional part" |

| 2026-11-04 | #1 and #2 reopened after the example repository pinned `a0789aa9` ([#4](https://github.com/s2002kumar/dataproof-example-subscriptions/pull/4)) | The Action with the type checks and the rerun check: eight required checks | #1 **`CHECKED`** (all eight), #2 **`BLOCKED`** (`invoice_amounts`, 396 mismatches; the seven others passed) | #1: job 28 s, 17.4 s inside the Action, four steps of about 0.7 s each; #2: job 34 s | A reopened pull request runs the workflow of its current merge, so the new pin took effect without a push. The `setup-uv` warning is gone |

Time to first check on the example repository, from opening a pull request to its verdict: **40 to 42 s** on the first commit (no cache), **28 to 34 s** on the second. The approval round trip (merge a one-line change, re-run) added about three minutes of a person's time and one 30-second job.

## Installations

| Date | Repository | Who | Setup time | First job | Verdicts | Problems |
| --- | --- | --- | --- | --- | --- | --- |
| 2026-11-04 | [dataproof-example-subscriptions](https://github.com/s2002kumar/dataproof-example-subscriptions) (public) | The author; **not external** | Not a cold measurement: the files were written during development, then materialized by `scripts/demo/build_action_example_repo.py` | 31 s | `INCOMPLETE` → `CHECKED` (#1), `INCOMPLETE` → `BLOCKED` (#2), `CHECKED` (#3) | The `setup-uv` cache warning above. The bootstrapping rule had to be learned on #86 |
| — | — | — | — | — | — | No installation by anyone else yet |

## False positives and misses

None observed outside the test corpus yet. The known structural miss is stated in the subscriptions README: a fault no oracle declares passes.

## Changes between pinned commits

| From | To | What changed for a caller |
| --- | --- | --- |
| `2cabbc16` | `a0789aa9` (2026-11-04) | Two more required checks per data contract run: `types.<contract>` (C8, the candidate's declared column types) and `migration.rerun` (C7, a second upgrade must change nothing). A migration that passed before can now be `BLOCKED` by either; the summary says which column or digest. The `setup-uv` cache is keyed on the Action's own lockfile, so the warning about the caller's repository is gone. No configuration change is needed. The example repository re-pinned to this commit in its [#4](https://github.com/s2002kumar/dataproof-example-subscriptions/pull/4) |
