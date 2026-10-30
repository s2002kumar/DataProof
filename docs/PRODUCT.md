# PRODUCT — Single Complete Product Contract

## User and problem

**Primary user:** a backend/platform engineer reviewing a PostgreSQL application migration with data-changing SQL. Secondary user: a data engineer responsible for integrity during schema evolution. **Job to be done:** before approving a PR, inspect a working preview and know exactly which declared database and API guarantees were checked, what failed and why.

Many migrations execute successfully while damaging values, relationships or application behavior. A green SQL command, matching table row counts and HTTP 200s are not proof that the intended business meaning survived.

## Complete customer journey

1. A developer opens/updates a GitHub PR in a **configured supported application repository**. An on-demand manual run in the dashboard is also supported.
2. DataProof pins `repo`, PR, base SHA, head SHA, immutable migration file digest and data-contract revision. It analyzes SQL changes, affected schema and referenced code. AI provides an *advisory* risk explanation and suggested edge cases.
3. A deterministic controller validates the project manifest, supported workflow, quotas and policy. Ineligible/untrusted repos or changes are reported `NOT_EXECUTED`, never guessed through.
4. The runner provisions a reference preview from the base commit and a candidate preview from the head commit (candidate source/archive verified against head SHA; runtime and dependency image pinned). Each contains an isolated PostgreSQL database, backend API and preview frontend. Each environment uses a known non-sensitive baseline fixture; the candidate applies the proposed migration. The reference is not a production connection.
5. The runner executes migration checks, database integrity contracts, selected API behavior tests and preapproved stress/negative cases. It emits structured evidence, timings and a coverage manifest.
6. AI investigates failures through a five-tool allowlist, read-only SQL and evidence-citation validation; it must cite actual evidence IDs, query results and code locations and acknowledge uncertainty. Deterministic code alone produces check conclusions.
7. DataProof identifies affected keys and columns, and attempts a *verified* smaller reproducer for suitable failures. If it cannot construct one, it reports that limitation rather than inventing a repro. A proposed correction can be evaluated only after an explicit developer action, in a **new** disposable environment.
8. UI and a commit-bound GitHub check show migration execution, preview URLs, evidence, failed and unexecuted checks, AI findings, reproduction outcome, logs and resources. Developer decides whether to merge/deploy; DataProof itself does not.
9. All per-run resources expire/tear down with a terminal cleanup record; evidence artifacts persist under defined retention rules.

## Committed product capabilities

- Configured GitHub PR integration with validated webhooks, run lifecycle and status/check runs.
- Migration review with static analysis and explicit `unsupported/unknown` handling.
- Real preview of a PostgreSQL + FastAPI + frontend application, reference and candidate.
- Approved deterministic seed data of nontrivial size and diverse edge cases; reproducible snapshot versioning.
- Schema, key, relationship, null, decimal, timestamp, duplicate, transformation-invariant and selected API response checks.
- AI change analyst and AI failure investigator (with a correction-advice mode), strictly bounded.
- Evidence reports with affected-row samples, query/query-result provenance, migration step IDs and commit binding.
- Reproducer extraction where technically possible, with automatic replay verification; approved correction rerun.
- Reviewer UI, GitHub summary and verification coverage; explicit human review.
- Public benchmark with faults and clean controls, deterministic baselines and AI ablations; honest failure cases.
- AWS-hosted restricted demo using isolated tasks and fully provisioned infrastructure-as-code; cost limits and cleanup.

## One supported demonstration profile

A **Commerce Orders & Refunds** FastAPI + Next.js sample service with PostgreSQL. Reference-schema entities: `customers`, `orders`, `order_items` and `legacy_payment_events` (one row per capture or refund, distinguished by `event_kind`). SQL migration: normalize `legacy_payment_events` into `ledger_entries`, one ledger row per source event, matched by `source_event_id`. Test conditions include one-to-many joins (order to items and to events), multiple captures and refunds per order, zero-refund orders, two currencies, decimal precision and rounding, NULL optional fields, timestamp instants across DST boundaries, missing/duplicated/multiplied events and the absence/presence of a refund. Exact schemas, mappings, invariants C1–C8 and fixture rules are fixed in `SCENARIO_CONTRACT.md`.

A trusted-base `dataproof.yml` declares **`runtime_image_digest` and `allowed_change_roots`**, protected dependency versions, Alembic migration paths, dataset/`oracle` IDs, proposed/reviewed `candidate_binding`, test APIs and preview routes. Candidate Python/TS source changes run only inside isolated approved runtime; `Dockerfile` and dependency/lockfile changes are `NOT_EXECUTED` until a trusted new image is approved. A reviewer must explicitly approve a new candidate binding; binding-dependent checks can pass only in the new run that the approval starts. The public hosted demo only executes allowlisted repository/image combinations. This is **not** a claim of arbitrary application support; the supported integration contract must be documented and exercised with a fresh repository checkout.

## Product UI contract

- Runs page: PR/commit, reviewer, state, check coverage, timestamps, deployment links.
- Run details: stage timeline, app preview links (reference and candidate), schema diff, measured execution, before/after APIs.
- Integrity view: failed invariant, severity rule, affected keys/columns, expected-vs-observed examples, source SQL link, sampled vs total mismatches.
- Investigation view: structured evidence citations, hypotheses, diagnostic tools invoked, confidence caveat, correction suggestion and replay status.
- Evidence: downloadable versioned JSON report, redacted logs, reproducibility information, run cleanup status and artifact hashes.

## Verdict semantics

- Every check is `PASS`, `FAIL`, `ERROR` or `NOT_RUN`; unknown/unsupported coverage cannot be silently relabeled as pass.
- Review summary is `BLOCKED` (required failure/error), `INCOMPLETE` (required check missing/not run, including pending binding review), `NOT_EXECUTED` (policy/path/dependency denial), `CHECKED` (all required checks pass without warnings), or `CHECKED_WITH_WARNINGS` (all required checks pass, but advisory or optional check warnings). GitHub Check conclusions: BLOCKED = `failure`; INCOMPLETE = `action_required`; NOT_EXECUTED = `neutral` (never success); CHECKED* = `success` only with an explicit required-check coverage table. None is a universal safety guarantee.
- An explicit developer/human decision is stored separately from the machine's evidence.

See `SCENARIO_CONTRACT.md` for the proposed exact entity mapping, fixture requirements and a rule preventing a PR from silently weakening its own validation contracts.

## Definition of done — no second-version promise

The product is complete when a clean checkout, local end-to-end demo, guarded AWS demo, GitHub PR workflow, preview URLs, benchmark regeneration, independently confirmed failure and correction scenarios, automatic cleanup, docs, CI, and recorded walkthrough all work and evidence can be reproduced. If an item cannot be delivered, record it as a concrete shortfall and reassess the product claim rather than silently moving it to an imagined next version.
