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
| 2026-11-07 | [dataproof-field-fastapi-template#1](https://github.com/s2002kumar/dataproof-field-fastapi-template/pull/1), the template's own UUID-key migration (upstream `e65b427`, unchanged) | The Action pinned to `50333112`, on a fork of an application DataProof's author did not write | **`CHECKED`**, exit 0: all six checks | 37 s from opening to verdict; job 30 s; 20.2 s inside the Action: the migration image with the template's requirements 5.9 s, both databases 10.0 s, three migration steps and the rerun 3.0 s, verification 0.9 s | Upstream revision `d98dd8ec85a3` ran through the template's own `env.py` with the six declared variables. The summary wrongly listed the base branch's five DataProof files as changed by the pull request, and counted 24 changed paths for a 9-file diff: the changed-paths bug, fixed in DataProof #95 |
| 2026-11-07 | [dataproof-field-fastapi-template#2](https://github.com/s2002kumar/dataproof-field-fastapi-template/pull/2), the same migration with each item's new owner key filled by joining on `item.id` instead of `item.owner_id`, a fault written by DataProof's author | Same | **`BLOCKED`**, exit 1 | 37 s from opening; job 29 s; 19.3 s inside | `template.item_owners` failed with 8 mismatches over 8 items. Five moved to another user ("Café notes": `blake@example.com` became `casey@example.org`) and three lost their owner ("Bug triage": `blake@example.com` became NULL). The migration ran without error, and `template.users` passed. The same false trusted-file warnings |
| 2026-11-07 | Fork #1 and #2 reopened after the fork pinned `ba733cfe` | The Action with DataProof #95 | #1 **`CHECKED`**, #2 **`BLOCKED`** (8 mismatches) | #1: job 32 s, 21.7 s inside, 39 s from reopening to verdict; #2: job 30 s, 20.7 s inside, 37 s | Counted from the merge base, upstream `cc480df7`: 9 paths and no trusted files |
| 2026-11-07 | [dataproof-example-subscriptions#5](https://github.com/s2002kumar/dataproof-example-subscriptions/pull/5), the re-pin to `ba733cfe`, then #1 and #2 reopened | The Action at `ba733cfe` | #5 **`CHECKED`**; #1 **`CHECKED`** (all eight checks); #2 **`BLOCKED`** (`invoice_amounts`, 396 mismatches) | #1: job 29 s, 20.7 s inside; #2: job 32 s, 20.7 s inside | At `a0789aa9`, #1 and #2 listed `dataproof/config.toml` as a trusted file they changed, and the workflow as a changed file; `main` changed both after they branched (#3, #4). Now each lists only its own two files |

Time to first check on the example repository, from opening a pull request to its verdict: **40 to 42 s** on the first commit (no cache), **28 to 34 s** on the second. On the field fork, from opening to verdict: **37 s** for both pull requests. The approval round trip (merge a one-line change, re-run) added about three minutes of a person's time and one 30-second job.

## An application DataProof's author did not write

[dataproof-field-fastapi-template](https://github.com/s2002kumar/dataproof-field-fastapi-template) is a public fork of [fastapi/full-stack-fastapi-template](https://github.com/fastapi/full-stack-fastapi-template) (MIT). Its base branch is upstream commit `cc480df7` plus one commit of DataProof files. The migration under test is upstream commit `e65b427`, which replaces both tables' integer keys, and the foreign key between them, with UUIDs.

- **The template's authors wrote** the schema, the migration and its `env.py`.
- **DataProof's author wrote** the configuration, a synthetic fixture (5 users, 8 items), two oracles and the faulty variant. The expected verdicts were written in the fork's [FIELD_TEST.md](https://github.com/s2002kumar/dataproof-field-fastapi-template/blob/main/FIELD_TEST.md) before the first run.
- **The oracles never mention an id.** Users are keyed by email and items by title, and each item's owner is read through the foreign key as the owner's email. So the key change needed no binding.

What the template needed, and what the configuration says:

| The template | The configuration | Since |
| --- | --- | --- |
| `env.py` builds its URL from `POSTGRES_SERVER`, `POSTGRES_PORT`, `POSTGRES_USER`, `POSTGRES_PASSWORD` and `POSTGRES_DB`, not `DATABASE_URL` | `[alembic.environment]` maps each to a placeholder: `{host}`, `{port}`, `{user}`, `{password}`, `{dbname}` | #93 |
| `env.py` imports `app.models`, and `alembic.ini` has no `prepend_sys_path` at this commit | `PYTHONPATH = "."`: the step runs in `alembic.ini`'s directory | #93 |
| `app.models` needs sqlmodel 0.0.19, which requires SQLAlchemy below 2.1; DataProof pins 2.1.6 | A requirements file pinned to this commit's `poetry.lock`. Its `alembic`, `sqlalchemy` and `psycopg` replace DataProof's pins | #93 |
| The migration runs `CREATE EXTENSION IF NOT EXISTS "uuid-ossp"` | Nothing: `uuid-ossp` is a trusted extension, and the migration role owns its database | — |
| The pull request bumps sqlmodel to 0.0.20 | Nothing can. The base branch's requirements are installed, so the migration ran with 0.0.19. Both versions map UUID fields; the migration uses `postgresql.UUID` directly | A limitation ([RELEASE.md](RELEASE.md#known-limitations)) |

Nothing in `backend/` changed. The fork removed the upstream workflows and `dependabot.yml`, so that only DataProof runs there, and restricts Actions to the four actions the workflow uses, pinned by commit.

## Installations

| Date | Repository | Who | Setup time | First job | Verdicts | Problems |
| --- | --- | --- | --- | --- | --- | --- |
| 2026-11-04 | [dataproof-example-subscriptions](https://github.com/s2002kumar/dataproof-example-subscriptions) (public) | The author; **not external** | Not a cold measurement: the files were written during development, then materialized by `scripts/demo/build_action_example_repo.py` | 31 s | `INCOMPLETE` → `CHECKED` (#1), `INCOMPLETE` → `BLOCKED` (#2), `CHECKED` (#3) | The `setup-uv` cache warning above. The bootstrapping rule had to be learned on #86 |
| 2026-11-07 | [dataproof-field-fastapi-template](https://github.com/s2002kumar/dataproof-field-fastapi-template) (public fork of an MIT application) | The author; **not external**. The schema and migration are the template authors' | Not a cold measurement: the author, who knows DataProof, wrote the configuration, fixture and two oracles in one sitting | 30 s | `CHECKED` (#1), `BLOCKED` (#2), as predicted before the runs | It needed step variables and requirement pins (#93), and found the changed-paths bug (#95) |
| — | — | — | — | — | — | No installation by anyone else yet |

## False positives and misses

No false verdict observed on the example repository or the field fork. The field fork's first runs gave false *warnings*: trusted files listed as changed by a pull request that never touched them (the changed-paths bug above, fixed). The known structural miss is stated in the subscriptions README: a fault no oracle declares passes.

## Changes between pinned commits

| From | To | What changed for a caller |
| --- | --- | --- |
| `2cabbc16` | `a0789aa9` (2026-11-04) | Two more required checks per data contract run: `types.<contract>` (C8, the candidate's declared column types) and `migration.rerun` (C7, a second upgrade must change nothing). A migration that passed before can now be `BLOCKED` by either; the summary says which column or digest. The `setup-uv` cache is keyed on the Action's own lockfile, so the warning about the caller's repository is gone. No configuration change is needed. The example repository re-pinned to this commit in its [#4](https://github.com/s2002kumar/dataproof-example-subscriptions/pull/4) |
| `a0789aa9` | `ba733cfe` (2026-11-07) | Untrusted text is escaped (#92), so a summary may show a path or contract id in a code span where it showed raw text. `[alembic.environment]`, and requirement names that replace DataProof's pins, are new and optional (#93). Changed files are counted from the merge base (#95), so a pull request behind its base lists fewer changed and trusted files. No configuration change is needed. The example repository re-pinned in its [#5](https://github.com/s2002kumar/dataproof-example-subscriptions/pull/5) |
