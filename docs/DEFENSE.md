# DataProof: the defense

What to say, and what not to claim, when DataProof is examined: in an interview, a design review or a due-diligence call. Every statement here is backed by something in the repository; where the honest answer is "not measured" or "not built", it says so. Links point at the evidence.

## One paragraph

DataProof verifies a PostgreSQL/Alembic migration on a pull request by running it in disposable reference and candidate databases and comparing the data, keyed row by row with exact comparators, against contracts the pull request cannot edit. The expected values come from a *protected oracle* on the base branch; the pull request may only propose a *binding* that says how to read its new schema, and a person approves that binding before any check that depends on it can pass. Deterministic code owns every verdict; the two AI roles are advisory, bounded to read-only tools, and must cite evidence. It exists as a GitHub App with previews, API checks, reproduction and a dashboard, and as a GitHub Action for the database tier that runs inside a repository's own CI with the migration isolated from the job's token and secrets. The demo pull request's migration executes without error and turns a 49.99 refund into 99.98; DataProof's public check run on that commit says `BLOCKED`, with the row.

## Architecture, in the order a reviewer should see it

1. **Trust boundaries first.** Three kinds of input: the base branch (trusted: oracles, fixture, configuration, approvals), the pull request (untrusted: migrations, bindings), and model output (untrusted: advisory, schema-validated, citation-checked). Every design decision follows from keeping these apart ([ADR-0002](adr/0002-ai-execution-boundary.md), [ADR-0005](adr/0005-oracle-binding-governance.md), [ADR-0007](adr/0007-github-action-database-tier.md)).
2. **The verification engine** ([`packages/verifier`](../packages/verifier/README.md)): oracle query on the reference, binding query on the candidate, typed keyed comparison (`EXACT`, `DECIMAL` with scale and tolerance, `INSTANT`), a fixed evaluation order where every exit is a typed outcome, a reference-first precondition for non-equivalence invariants, and the type check for the candidate's declared column types. Query safety is layered: AST-level binding review by PostgreSQL itself (a temporary view that is never executed), allowlisted functions, prepared statements so a string cannot smuggle a second statement past a read-only transaction, a least-privileged role, timeouts and row caps.
3. **Execution** ([`services/runner`](../services/runner/README.md), [ADR-0004](adr/0004-candidate-source-execution.md), [ADR-0006](adr/0006-pluggable-execution-backends.md)): a backend interface with one implementation, local Docker; per run and role an internal network, a digest-pinned PostgreSQL with owner and reader roles, step containers from a trusted image whose entrypoint verifies the source archive's SHA-256 as an unprivileged user before any pull-request code runs; every control has a negative test.
4. **The control plane** ([`packages/control`](../packages/control/README.md), [`services/api`](../services/api/README.md)): a PostgreSQL-backed state machine with leases, fencing and retries; webhook intake with signature verification and exact-SHA pinning; commit-bound check runs with the finality rule GitHub actually applies; the reviewer API with the approver rule; a signed per-preview gateway on a separate origin.
5. **The Action** ([`packages/action`](../docs/ACTION.md)): the same verifier, backend and report, with trusted inputs read from the base branch's tip at run time and approval as a reviewed change to the base branch.
6. **Evidence and reporting** ([`packages/report`](../packages/report/README.md)): every check cites query digests; databases are dumped by trusted code; the summary a reviewer reads is built from trusted results only, with values from the candidate database escaped and bounded.

## Difficult bugs, and what each taught

| Bug | What happened | Why it mattered | Fix and lesson |
| --- | --- | --- | --- |
| A read-only transaction that was not | psycopg sends a parameterless query through the simple protocol, so a single string `SELECT …; COMMIT; DELETE …` ends the `READ ONLY` transaction and runs the `DELETE`. Confirmed against PostgreSQL 17 | Bindings are pull-request SQL. "Read-only" was a claim the driver did not keep | Every contract and diagnostic query is a prepared statement (extended protocol, one statement per message), plus a reader role whose sessions default to read-only. Lesson: a security property must be tested at the protocol level, not inferred from a setting |
| Relations hidden from the dependency catalog | `pg_depend` omits pinned built-in objects, so a binding calling `query_to_xml('select * from legacy_payment_events', …)` reads a table through text with no recorded dependency | The reviewer aid that lists what a binding reads would have been blind to it | Relations and functions are also read from the analyzed rewrite trees of the binding and every view it reaches; only allowlisted built-ins are accepted. Lesson: the catalog answers the question it was designed for, not yours |
| GitHub's check-run finality | A required check reads only the check run *created last* with its name on a commit, whatever order they completed in; an earlier `success` does not count. A burst of approvals created superseded runs' check runs after the newest run's, and a commit DataProof had `CHECKED` read as blocked. It could also fail open | The product's main output is the check; its meaning depended on an undocumented rule | Probed the rule on real GitHub with labelled check runs ([DEMO](DEMO.md#check-run-finality)); the publisher now creates the newest run's check run last, under a per-commit lock, and omits check runs for runs a newer run has overtaken. Lesson: probe the platform; and the fixed publisher still has to be exercised live (#78) |
| Equal instants that compared unequal | Python compares datetimes sharing a `tzinfo` on the wall clock and ignores `fold`, so 01:30 EDT and 01:30 EST on the fall-back night compared equal | Timestamp faults across DST transitions are a benchmark family | Every instant is pinned to its own fixed offset before comparison (`values.py`). Lesson: "aware datetime" is not "instant" |
| The pull request that configured the Action could not be verified by it | The Action reads its configuration from the base branch; the first `dataproof` job ended `ERROR: config does not exist on main` | A correct design surprised its own author on first contact | Documented as the bootstrapping rule in the quickstart; the first external-repository run then went as designed. Lesson: the first real run is the test |
| A silent fault that was not silent | A scenario meant to double tax through a join fan-out errored instead: `SUM(integer)` is `bigint`, and `cents * basis points` overflowed `int4` | A fault scenario that errors is worthless as a test of silent corruption | Casts in the scenario; the general point stands in the README: every fault in the set executes without error. Lesson: the hard part of a corruption corpus is keeping the corruption quiet |
| An approval bound to a commit cannot live on a branch | The App binds an approval to head SHA and binding digest, which is right when the approval starts a run on that commit; for the Action, a base-branch allowlist keyed that way would need re-approval on every push | Two correct designs for two trust models | The Action approves a content digest (contract, query, route, mapping) that survives commits and edits need a new approval; the commit and path are still recorded. Lesson: the same word, approval, can need two definitions |
| Docker address pools and shared kernels | Each run holds about six networks; the machine ran out at three live runs; `docker build --quiet` failed under the test runner but not by hand | Local Docker is a trusted-development boundary with operational limits | Per-run cleanup, an independent reaper, `--iidfile` instead of parsing output. Lesson: local isolation is real but not free, and never the hosted boundary (ADR-0006) |

## Alternative designs, and why not

- **Trust the pull request's own tests or thresholds.** Then a pull request can hide a fault by relaxing what it is held to. The oracle–binding split exists so the expected values and the comparator are never the pull request's to change.
- **Compare schemas instead of data.** A schema diff reports every change set as changed and cannot see a value; the type check keeps the part of a schema comparison that bears on correctness and judges it against the contract ([ADR-0008](adr/0008-invariants-c4-to-c8.md)).
- **Row counts and hashes.** The benchmark's `B1` baseline does exactly that, fairly configured, and catches 13 of 20 faults; keyed typed comparison catches 18 of 18 the contracts can observe. Counts and hashes cannot localize, cannot explain, and miss faults that preserve totals.
- **Let a model decide.** Model output is advisory by construction (ADR-0002); the benchmark's model-off arm already names the root cause in 9 of 18 failing cases, and the live comparison is the next experiment, not an assumption.
- **`pull_request_target` or a hosted endpoint for the Action.** The first hands a write token to the migration; the second is the App. The Action refuses the first and says what it is not.
- **Approval by label or comment.** Grantable by people without write access to the base branch; the Action's approval is a reviewed change to that branch.
- **A universal application profile.** DataProof supports documented profiles (commerce, subscriptions) and says so; a tool that builds arbitrary repositories with a token in reach is the standard mistake the threat model is written against.

## Where it sits among other tools

From public documentation only; none of these was run here, and this is positioning, not a benchmark.

| Tool or practice | What it does | What DataProof does differently |
| --- | --- | --- |
| Migration managers (Alembic, Flyway, Liquibase) | Version and apply schema changes | DataProof runs on top of Alembic and judges the *data* the migration produced; it manages nothing |
| Schema-change safety (Atlas, Bytebase, pgroll, Reshape, squawk) | Lint DDL, plan safe schema changes, zero-downtime patterns | Those judge the statement; DataProof judges the outcome against a protected contract, so a backfill that executes cleanly and halves a refund is still caught |
| Schema diffs (migra, pgdiff) | Compare two schemas | Blind to values; DataProof's type check keeps the correctness-relevant part and compares data |
| Data diffs (Datafold data-diff and similar) | Compare tables across databases, often warehouse-first | Closest in spirit. DataProof adds the governance (oracle on the base branch, reviewed binding, approval), the keyed exact comparators with evidence, and the execution isolation inside a pull-request workflow |
| Database unit tests (pgTAP) | Assertions written by the team against a database | Assertions a pull request can edit; DataProof keeps expected values out of the pull request's hands |
| Database branching (Neon, PlanetScale deploy requests) | Cheap copies of a database for a change | Gives the environment, not the verdict; DataProof could run on such a copy, and today runs on disposable containers |
| "CI with testcontainers" | What teams do by hand | DataProof is the opinionated version of that with the trust model, the contracts and the report; the Action's whole point is to drop into exactly that workflow |

## Security limitations, stated plainly

- **Local Docker is not a VM boundary.** Containers share the kernel; the operator's daemon is trusted. It is the boundary for development, CI and the local demo, never the hosted one (ADR-0006). The hosted design (Fargate tasks with no task role, VPC-endpoint-bound source delivery, an independent reconciler) is written but **not built** and not claimed.
- **A pull request can edit the workflow that calls the Action.** Branch protection with a required check and code-owner review on `.github/workflows/` and `dataproof/` are the control; the Action cannot enforce them and says so.
- **Self-hosted runners.** A kernel escape on a persistent runner yields the runner. The guide says not to run untrusted pull requests on one.
- **Supply chain.** The migration image installs the base branch's pinned requirements with network at build time. What the base branch trusts, the Action trusts.
- **Resource use** is bounded by limits and a timeout, not prevented.
- **One person.** The live demo's author was also its reviewer and the only maintainer; approvals are recorded as procedural separation. The approver rule is enforced when there is more than one maintainer, and that path has not been exercised live.
- **The AI roles** are bounded by tools, budgets, citation validation and injection negatives; prompt injection through SQL, comments or evidence is tested but a model has not yet been run live, so the bounds are verified and the value is not.
- **The fixed check-run publisher** has not run a live take (#78).

## Benchmark weaknesses

- **Authored by the author.** The 31 database-tier cases are generated from templates with a frozen seed by the author of the system under test. The sourced scenarios on the subscriptions schema are re-implementations of documented incidents, still small and still ours.
- **Small.** 20 faults, 11 clean controls, 15 pipeline-tier runs, 14 change sets on the second schema. A difference of one or two cases is noise, and the reports say so.
- **The rules knew the corpus.** The static risk rules anticipate 21 of 22 faults and alarm on 6 of 12 clean controls, and their recall is optimistic because they were written with the corpus in view.
- **Two schemas, two fault shapes each.** Commerce is an events-to-ledger normalization; subscriptions is cents-to-money and a table split. Nothing about warehouses, partitioning at scale, or concurrency.
- **Fixtures are small** (hundreds of rows), so timings prove nothing about production-scale migrations, and the type check exists precisely because a small fixture can miss a declared-type fault.
- **The misses are real.** NULL-and-empty confusion is invisible to the declared contracts (2 of 20), and a fault no oracle declares passes by construction.
- **No external run.** The Action's timings (40 s to a verdict on the example repository) and installations are the author's; the field-results log has no external entry yet.
- **The AI arms are unmeasured.** Protocol frozen, budget approved, key pending; until the run happens, the only honest statement about the investigator is what it cannot do.

## Questions that deserve a direct answer

- *Is this production-ready?* No. It is a locally demonstrated release with a working GitHub integration and an Action that has run on one example repository by its author. See [STATUS](STATUS.md).
- *What would you do first with a real team?* Install the Action on one repository of theirs, record the four timings and every surprise in the field-results log, and let their first false positive drive the next change.
- *What is the single most important design decision?* That the pull request cannot change what it is held to: expected values and comparators live on the base branch, and a binding needs a person.
- *What is the single most important thing not yet done?* Someone else running it.
