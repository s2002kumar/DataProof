# Requirements against verified functionality

This compares the original product contract ([PRODUCT](PRODUCT.md)) with what has been **verified**. Implemented is not enough to count.

**Verified** means checked by something other than the author's say-so, which anyone can rerun or inspect:

- CI on every merged pull request;
- the [live GitHub run](DEMO.md#live-run), whose check runs are public;
- a [probe of GitHub's branch protection](DEMO.md#check-run-finality);
- the [benchmark](../benchmarks/README.md), which a test regenerates exactly;
- the [local rehearsal](DEMO.md#local-rehearsal) on real migrations.

Nothing here has been verified by a third party. The benchmark corpus was written by the author of the system it tests.

As of 2026-11-04.

## Committed capabilities

| # | Requirement (PRODUCT) | Status | Evidence | Shortfall |
| --- | --- | --- | --- | --- |
| 1 | GitHub PR integration: validated webhooks, run lifecycle, check runs | **Verified, live** | Real `pull_request` webhooks started runs on [PR #2](https://github.com/s2002kumar/dataproof-demo-commerce/pull/2), and the App published a check run per run. GitHub's check-run finality rule was probed under branch protection, and the publisher fixed with regression tests against a simulated GitHub (#73) | **The fixed publisher has not run against real GitHub** (#78): the fix is tested, not demonstrated live. Each commit has one latest `DataProof` check, shared by pull requests with the same head commit |
| 2 | Migration review with static analysis and explicit unsupported or unknown handling | **Verified** | Policy refusals (`NOT_EXECUTED` for forks, dependency and out-of-root changes) are tested. Static risk rules run in `ANALYZING` on every eligible run (#75), and the [change-analysis benchmark](../benchmarks/results/CHANGE_ANALYSIS.md) regenerates exactly | The rules are patterns, not proof: they alarm on 6 of 12 clean controls, and their recall is optimistic because they were written with the corpus in view |
| 3 | Real reference and candidate previews (PostgreSQL, FastAPI, frontend) | **Verified, live and local** | Live run, rehearsal, and 15 pipeline-tier runs with previews of both commits through the signed gateway | Local Docker only |
| 4 | Approved deterministic seed data and reproducible snapshots | **Verified** | The fixture digest is pinned in every run. Sealed `pg_dump` snapshots must restore to the same rows, and data contracts replay from them (tests) | — |
| 5 | Schema, key, relationship, null, decimal, timestamp, duplicate, transformation-invariant and API checks | **Verified as narrowed in [ADR-0008](adr/0008-invariants-c4-to-c8.md)** | C1 (event preservation, per key and field, including amounts and instants), C2 (refunds), C3 (captures) and C6 (API through previews) are protected oracles, with 18 of 20 benchmark faults detected and 0 of 11 false positives. **C8** is built as the type check `types.<contract>` (declared scale and instant-capable types; `money-columns-whole-units` and `period-naive-timestamp` blocked, clean controls pass). **C7** is built as the rerun check `migration.rerun` in the Action (a second upgrade must change nothing; `env.py` that writes on every run is blocked). **C4** and **C5** are established by C1's keyed comparison of reference columns and instants, not by separate contracts | The claims are narrowed, not met in full: no constraint audit (PK/FK/uniqueness from the catalog), no standalone relational invariant, no timestamp tolerance policy, no rerun check in the App's pipeline yet, and no schema diff run (#6: the named tool does not import on Python 3.12 and was not run here). NULL and empty-string confusion is invisible to the declared contracts (2 benchmark misses) |
| 6 | AI Change Analyst and AI Failure Investigator, strictly bounded | **Bounds verified; value not measured** | Both run the same bounded loop: allowlisted read-only tools, citation validation, budgets, and injection negatives (tests, including Docker pipeline tests). A rejected answer is never shown | **No live model call has been made yet.** The owner approved the budget on 2026-11-03 (Failure Investigator 1 + 9 USD, Change Analyst 5.10 USD); the live runs follow the frozen protocol and are next. The rule-based arms are measured: the rules name the root cause in 9 of 18 failing cases ([protocol](AI_EVALUATION.md)). The correction-advice mode is only the `suggested_action` field |
| 7 | Evidence reports: affected rows, query provenance, migration steps, commit binding | **Verified** | Report and evidence manifest (tests), and the live run's reports | — |
| 8 | Verified reproducer and approved correction rerun | **Verified** | Subset reproduction is `REPRODUCED` or honestly `NOT_REPRODUCED` (benchmark, tests). Correction replay ran live, `BLOCKED` then `CHECKED` | Extra-row failures cannot be reproduced from a reference subset (documented) |
| 9 | Reviewer UI, GitHub summary, coverage, explicit human review | **Verified** | Dashboard browser test (CI `e2e`). Live run: GitHub sign-in, binding approvals and correction replay. The five-step run page (#76) was driven through two full rehearsals on real runs. The check summary carries the coverage table | One person was both author and reviewer (procedural separation). An organizational second maintainer is untested live |
| 10 | Public benchmark: faults, clean controls, baselines, AI ablations | **Partly verified** | 31 database-tier cases, a pipeline tier and fair baselines, regenerated by tests | The live AI ablation (B3) and the live Change Analyst run wait for a budget. The corpus is not independently authored |
| 11 | AWS-hosted restricted demo | **Not built** | — | Needs owner approval of a measured cost and security review (ADR-0006) |

## Definition of done

| Item | Status |
| --- | --- |
| Clean checkout | Verified: CI on every merge |
| Local end-to-end demo | Verified: the rehearsal, recorded |
| Guarded AWS demo | **Not built** (approval-gated) |
| GitHub PR workflow | Verified live |
| Preview URLs | Verified live |
| Benchmark regeneration | Verified by tests |
| Independently confirmed failure and correction | Verified live, on public check runs, by one person |
| Automatic cleanup | Verified: no resources left after the live run, the rehearsals and 15 pipeline runs; reaper test |
| Docs, CI, recorded walk-through | Verified |

## Gates

| Gate | Status |
| --- | --- |
| G1, runner | Met: migrations run twice, isolated, recover on cancel and timeout, nothing stale |
| G2, correctness | Met for C1–C3 and C6, and for C4, C5, C7 and C8 as narrowed in ADR-0008 (type fitness, a no-op rerun in the Action, relationships and instants through C1) |
| G3, AI | **Half met:** it cannot alter a verdict or an authorization (verified). It has not been measured against fixed rules with a real model |
| G4, product | Met live: PR → preview → failure → diagnosis → evidence → human review, without the terminal |
| G5, release | **Not met:** no AWS demo. See "What is released" below for what is |

## What is released, and what is not

Two different things are called "release" in the original plan, and they are kept apart here.

**The locally demonstrated release** exists and is what every verified claim above rests on: the GitHub App running on one machine against real GitHub (the live run), the local rehearsal, the benchmark, and the GitHub Action with its public example repository. Its criteria, all met: a clean checkout builds and its CI is green on every merge; the review runs end to end from a real pull request to a commit-bound check; a failure and its correction are confirmed on public check runs; cleanup leaves nothing behind; the benchmark regenerates from frozen seeds; the Action's security tests pass in CI and it has produced genuine results on a second schema; the documents say what is verified and what is not.

**The AWS-hosted restricted demo** (PRODUCT's last committed capability, gate G5, work package 11) is **not built**, and this document does not mark it satisfied. It waits for the owner's approval of a measured cost and security review (ADR-0006, #5, #10, #11). Nothing in the local release stands in for it: local Docker is a trusted-development boundary, and no document may present it as the hosted one.

**Not yet true either way:** an external user has not run the Action (#80); the fixed check-run publisher has not run a live take (#78); the two AI roles have not been measured with a live model (budget approved, key pending).

## Beyond the original contract: the GitHub Action

Added on 2026-11-04 ([ADR-0007](adr/0007-github-action-database-tier.md), [docs/ACTION.md](ACTION.md)): the database tier of the review as a GitHub Action over the same verifier, backend and report, with trusted inputs read from the base branch and the pull request's migration isolated in a container.

| Item | Status | Evidence | Shortfall |
| --- | --- | --- | --- |
| The Action verifies a PostgreSQL/Alembic pull request on a second, independently designed schema | **Verified locally and in CI** | `tests/integration/test_action.py` on `examples/subscriptions`: a correct cents-to-money migration is `CHECKED`, integer division is `BLOCKED` with the exact amounts, an unapproved binding fails closed | Not yet installed on a repository outside this one (#80) |
| Security gate: migrations isolated from secrets, tokens, the Docker socket and the runner | **Verified by adversarial tests** | A hostile migration's probes are all denied and the job's canary secrets reach nothing; self-approval and oracle weakening are ignored; `pull_request_target` is refused | A pull request can still edit the workflow that calls the Action; branch protection and code-owner review are the control (ADR-0007) |
| Real GitHub Actions results | **Verified on a public example repository** | [dataproof-example-subscriptions](https://github.com/s2002kumar/dataproof-example-subscriptions): #1 `INCOMPLETE` → approval on `main` → `CHECKED`; #2 `INCOMPLETE` → `BLOCKED` with 396 mismatches; 40 s from pull request to verdict ([field results](ACTION_FIELD_RESULTS.md)). Also on this repository's own pull requests | Run by the author only; no external user yet; no version tag |
| Independently sourced failure scenarios | **Verified at the database tier** | Seven faults re-implemented from documented OSS incidents (linked) plus two authored, with three clean controls; every one blocked or passed as expected with deterministic counts ([example README](../examples/subscriptions/README.md), `tests/integration/test_subscriptions_scenarios.py`) | The incidents show the fault shapes occur; they say nothing about frequency, and the re-implementations are small |

## Decisions that remain the owner's

- **Model budget:** decided on 2026-11-03. At most 10 USD for the AI ablation (pilot 1 USD, then 9 USD) and 5.10 USD for the Change Analyst's live run, 15.10 USD in all, under the frozen [protocol](AI_EVALUATION.md). Spending against it is reported with the results.
- **The missing invariants:** build C4, C5, C7 and C8 and the schema diff (#6), or narrow requirement 5.
- **AWS:** a cost estimate and security review for the hosted demo, or descope it (#5, #10, #11).
- **Name clearance** (#13).
