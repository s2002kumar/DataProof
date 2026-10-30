# IMPLEMENTATION — One Complete Project, Built in Reviewable Slices

**No staged product promise:** The following are dependency-ordered engineering tasks leading to the single completion contract in PRODUCT.md. A milestone is not an excuse to defer a committed capability or invent a second version.

## Repository standards

- One monorepo; no orphan prototypes, empty module sprawl, generated build artifacts or credentials committed.
- `main` protected; focused feature branches (`feat/runner-isolation`, `feat/verifier-contracts`) and reviewable PRs. Avoid giant omnibus PRs. Each PR describes motivation, design changes, tests, screenshots/evidence, risks and rollback.
- Conventional commits: `docs:`, `feat(runner):`, `test(verifier):`, `fix(github):`, `infra:`. Never manufacture commits for graph appearance, impersonate manual authorship, or assert tests not run. Use truthful author/co-author conventions appropriate to tools used.
- Python: Python 3.12+, **`uv` workspace**, `ruff`, **`mypy --strict`**, pytest with `testcontainers-python` locally and PostgreSQL CI service; Pydantic schema export and round-trip tests. TypeScript: **`pnpm`**, strict mode and Playwright e2e. `pre-commit`, secret scanning, reproducible lockfiles and the project-owned **Apache-2.0 license** (adopted 2026-10-30; all included material was written for this project and the fixture data is synthetic).
- `AGENTS.md` specifies constraints, safe coding rules and PR acceptance; human reviews every architectural/security/SQL change.
- CI run: formatting/lint/type/unit tests, PostgreSQL integration on fixtures, frontend build/tests, a network-disabled `next build` of the sample app (ADR-0004), security scans and smoke benchmark. Release gate: end-to-end PR + cloud run + cleanup + independent reproducibility.
- Documentation updated with behavior change in the same PR. ADR when a design choice changes trust boundary, core data contract or architectural split.

## Dependency ordered work packages

| Order | Issue group | Exit evidence |
| --- | --- | --- |
| 0 | Document sign-off and threat-model review | Independent design review with all blockers resolved; ADRs accepted |
| 1 | Typed monorepo foundation, result contracts and gated CI | New checkout green; JSON schema and PostgreSQL integration test run |
| 2 | Commerce baseline, Alembic migrations, deterministic fixture and F1 hand-written proof | Same row counts/keys but wrong refund value is demonstrated against real PostgreSQL, and the safe migration passes the same check with zero mismatches |
| 3 | Deterministic verification engine against plain PostgreSQL | Safe restructure passes; F1 and F3 fail for correct reasons; clean controls avoid false positives |
| 4 | Disposable PostgreSQL runner + durable lifecycle/cleanup | Safe/failing real migrations captured; TTL/cancel/retry, new head supersession and snapshot sealing pass |
| 5 | Reference/candidate FastAPI + frontend shadow preview | Two working isolated URLs bound to exact base/head SHA and baseline; offline candidate build validated; measured candidate-task memory, CPU and build time recorded |
| 6 | Evidence provenance, sealed `pg_dump` restore and reports | Redacted immutable reference/candidate snapshots tied to run/commit/contract; restore/replay tested |
| 7 | Change Analyst and bounded Failure Investigator | Typed tool calls, evidence-citation tests, prompt-injection negatives, model-off baseline |
| 8 | FK-closure subset reproduction + developer-reviewed correction replay | Verified reproducer or honest `NOT_REPRODUCED`; correction gets a fresh immutable run |
| 9 | GitHub App, signed preview gateway and review dashboard | Commit-bound check, binding-approval UI, authorized previews, diff inspector and AI finding view |
| 10 | Fault benchmark, clean cases, baseline comparison and AI ablation | Results regenerated from frozen seeds; per-category coverage, fair baselines and limitations |
| 11 | Restricted AWS hosting, Terraform, logs and independent task TTL cleanup | Real cloud run with no runner credentials, isolation and actual spend/cleanup evidence |
| 12 | Release hardening: e2e/security sweeps, demo, README and runbook | Clean-checkout walkthrough; failures and unsupported cases documented honestly |

Task order may overlap only when contracts are stable; do not begin speculative UI/API integration before run/evidence schemas have tests.

## Hard gates

- **G0 — Sign-off:** Product contract, supported app/profile, verification semantics, AWS trust model and benchmark design approved.
- **G1 — Runner:** Execute actual migration twice, isolated from control plane, recover on cancel/timeout, no stale artifacts.
- **G2 — Correctness:** Known semantic corruption detected; legitimate transformations not wrongly rejected; error/unknown semantics handled.
- **G3 — AI:** Model can explain with actual evidence but cannot alter verdict/authorization; measured vs fixed rules.
- **G4 — Product:** GitHub PR → preview → observed failure → investigation → evidence → human review works without terminal handholding.
- **G5 — Release:** AWS restricted demo, cleanup and costs observed; reproducible benchmark and video; docs reflect reality.

## Scope and time reality

Planning estimate: **20–26 focused solo weeks at 15–20 genuinely available hours/week** (roughly 300–520 focused hours), potentially longer for security/cloud/review blockers. This is an engineering forecast, **not** a promise of a deliverable date; a 10–14-week estimate would require materially more weekly hours and demonstrated acceleration. Scope remains one complete product. Progress milestones are engineering checkpoints, not separately marketed versions. Do not present unfinished functionality as shipped.

## First commit and PR train

1. **PR 1** — `docs: resolve adversarial review findings and adopt provisioning/contract ADRs` (specification only: this document set).
2. **PR 2** — `chore: initialize uv/pnpm workspace, typed contracts, and CI`; includes `DataContract`, `CheckResult`, `Finding`, `EvidenceManifest`, `HumanDecision` and round-trip tests. The run-state enum has no binding-review state: `AWAITING_BINDING_REVIEW` is a `NOT_RUN` reason and an `INCOMPLETE` verdict cause (ARCHITECTURE, Run state machine).
3. **PR 3** — `feat(sample): add commerce baseline, Alembic safe/faulty migrations and F1 proof`. Exit criteria, both green in the same PR: (a) **detection**: against real PostgreSQL the F1 revision executes successfully, row counts and key sets still match, and the per-event C1 comparison reports `FAIL` with expected `49.99` and observed `99.98`; (b) **clean control**: after the safe revision, the same comparison reports `PASS` with zero mismatches. The **test suite itself PASSES** by asserting both outcomes; it must not fail CI merely because F1 is defective, and without (b) a check that always fails would satisfy (a).
4. `feat(verifier): add protected oracle and reviewed candidate bindings`.
5. `feat(runner): add isolated migration execution, state machine, retries and cleanup`.
6. `feat(preview): add reference/candidate FastAPI and Next.js previews`.
7. `feat(evidence): seal reference/candidate pg_dump artifacts and replay`.
8. `feat(investigator): bounded AI analysis and evidence-based diagnosis`.
9. `feat(repro): FK-closure reproducer and approved correction replay`.
10. `feat(github): SHA-bound review checks and binding decision flow`.
11. `feat(web): reviewer dashboard and signed preview gateway`.
12. `test(benchmark): neutral-label clean/fault corpus and ablations`.
13. `infra: isolated Fargate execution, Terraform, independent TTL reconciler`.
14. `docs: publish verified demo, benchmark, cost and limitations`.

These are suggested **future** commits, not actions already taken.

## Release checklist

- [ ] Every `PRODUCT.md` requirement has linkable code+test/demo evidence.
- [ ] No GitHub check marks `NOT_EXECUTED` or `INCOMPLETE` as success; `BLOCKED` -> failure, `INCOMPLETE` -> action_required, `NOT_EXECUTED` -> neutral, `CHECKED`/`CHECKED_WITH_WARNINGS` -> success only when all required checks passed, with explicit coverage table.
- [ ] Both isolated shadow apps and DBs are usable; correct base/head SHA and identical baseline digest pinned; changed frontend built offline inside restricted task.
- [ ] Corruption case found despite successful migration, with independent evidence.
- [ ] AI diagnosis tested vs deterministic diagnosis; unsupported claims never cited as facts.
- [ ] At least one verified smaller reproducer or explicit impossibility boundary.
- [ ] Correction replay cannot mutate original run and is human-initiated.
- [ ] Unauthorized SQL, preview access and webhook tests are negative.
- [ ] AWS task isolation and cleanup demonstrated; billing thresholds documented.
- [ ] Benchmark results, source licenses, local reproduce and video are accurate.
