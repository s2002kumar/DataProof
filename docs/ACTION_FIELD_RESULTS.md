# The Action in the field

Every real run of the Action outside its own tests, in order, including the ones that failed. Timings are as measured, from the Actions page or a clock; nothing here is rounded to a target. A run by someone other than the author is marked **external**; until one exists, no claim of external adoption is made ([#80](https://github.com/s2002kumar/DataProof/issues/80)).

## Timings so far

| Date | Where | What | Result | Measured | Notes |
| --- | --- | --- | --- | --- | --- |
| 2026-11-04 | This repository, local (Windows 11, Docker Desktop, 22 CPUs) | `tests/integration/test_action.py`: 11 runs of the Action in a row, the first building the migration image | 11 passed | 7 min 14 s for the suite, about 35 s to 1 min 40 s a run; the first `CHECKED` run, image build included, 1 min 43 s | Each run provisions two PostgreSQL containers and runs three migration steps |
| 2026-11-04 | This repository, GitHub Actions (`ubuntu-24.04`) | CI job `action-tests`: the same 11 runs | passed | 2 min 0 s for the job | Faster than the laptop; image layers were not cached between runs |
| 2026-11-04 | This repository, GitHub Actions, pull request #86 | The first `dataproof` workflow run: the Action on the pull request that introduces its own configuration | `Not verified (ERROR)`, exit 4, by design | 10 s to the refusal; `uv sync` of the Action's 21 packages 8 ms (cached) | The configuration is read from `main`, where it did not exist yet. Recorded as the bootstrapping rule in the quickstart |

## Installations

| Date | Repository | Who | Setup time | First job | Verdicts | Problems |
| --- | --- | --- | --- | --- | --- | --- |
| — | — | — | — | — | — | No installation outside this repository yet |

## False positives and misses

None observed outside the test corpus yet. The known structural miss is stated in the subscriptions README: a fault no oracle declares passes.

## Breaking changes between commits

None yet.
