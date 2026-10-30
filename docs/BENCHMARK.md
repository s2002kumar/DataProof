# BENCHMARK — Reproducible, Hard-to-Game Evaluation

## Purpose

Demonstrate (1) real migrations run in isolated environments, (2) meaningful corruption and application regressions are detected, (3) data-level localization is correct, and (4) AI adds value in diagnosis compared with deterministic approaches. No fabricated scores, self-declared universal correctness or cherry-picked winning examples.

## Data and licensing

- Primary: versioned, deterministic synthetic **Commerce Orders & Refunds** PostgreSQL database seeded by controlled code. Include USD/cents and a second currency rule; multiple payments/refunds per order; nested join cardinality; precision/null/timezone/Unicode; composite keys; orphan candidates; repeated/partial runs. No real user PII.
- Secondary robustness: independent PostgreSQL sample database such as Pagila (PostgreSQL license) only when relevant; license, exact revision, adaptations and downloaded hashes documented: https://github.com/devrimgunduz/pagila
- Scale tiers: small (fast CI), medium (developer demo), large (only if resource measurements justify); record actual rows, no predetermined 'millions' marketing claim.
- No ground-truth labels, informative branch names (e.g. `bug/refund-join-multiplication`), injected-fault filenames or revealing commit messages are available to the investigator. Use neutral `change/ledger-backfill-07` / `case-007` style identifiers; store labels in a separate inaccessible evaluator manifest. Source changes may be inspected as evidence, but the investigator `read_source` tool excludes benchmark fixtures, labels, evaluation configs and design documents that describe F1/F2/F3.

## Fault families and clean controls

| Family | Example | Expected signal |
|---|---|---|
| Grouped join multiplication (F1) | one refund row amount 49.99 → 99.98; same row/key count | per-event exact amount AND amount SUM baseline when configured |
| Amount transposition (F3) | two stable refund IDs exchange amounts; total unchanged | per-event keyed value check; aggregate-only baseline may pass |
| Balanced loss+duplication | remove one event and duplicate another | keyed/multiset mapping despite equal total rows |
| Lossy numeric conversion | truncate/round fractional currency | exact NUMERIC contract |
| Timestamp conversion | shift UTC vs local time | instant-preservation contract |
| NULL/empty confusion | unset values changed to `''` | typed null profile and per-record mapping |
| FK/orphan | ledger record references absent order | relationship check |
| Wrong column mapping | amount and fee swapped | invariant and exact mapping |
| Partial migration | fail during multi-step update | step/transaction outcome + integrity |
| Retry/duplicate effects | reapply change after interruption | idempotence/effect count |
| Schema/API contract | drop field consumed by API | selected behavioral test |
| Intentional restructure | split table but preserve required semantics | **clean** successful mapped case |
| Legitimate precision/time change | declared compatible normalization | **clean** case, tests false positives |

Target at least 20 seeded defective scenarios and 10 clean/intentional controls before reporting a headline number; obtain 3–5 blinded fault scenarios from a second person **if available**, or freeze a deterministic parametrized template/seed set before tuning and state clearly that it is **not independently authored**. Report label provenance and avoid claiming blind independent design without evidence. This is a test design target, not a claim those cases exist.

## Comparators

1. `B0` (execution smoke baseline): execution success + API HTTP status smoke checks.
2. `B1` (reconciliation baseline): counts, schema, key/uniqueness, aggregate/null/**key hashes** computed over the **reviewed candidate binding view** using explicit `source_event_id` ↔ `event_id`, typed per-order/currency aggregations and the same approved input dataset. Configure the actual refund-amount SUM when applicable. B1 **does detect F1** if its aggregate covers amount; F3 is the aggregate-preserving case where C1 might add value. Never mark an applicable B1 check as `NOT_RUN` just to inflate DataProof's advantage.
3. `B2`: deterministic contract verification and fixed rules without LLM.
4. `B3`: B2 + structured AI investigation with same observed evidence.
5. Run a named schema-diff tool such as `migra` or `pgdiff` on supported schema cases; explicitly label it **schema-only**, not a full value comparator. If a working value-diff tool is evaluated, include its applicable subset, version, limits and results honestly. Do not treat schema diff as equivalent to semantic contracts or claim blanket superiority.

Keep the distinction: AI is evaluated for diagnosis and useful test suggestions, **not permitted to retroactively decide correctness**. Detection efficacy belongs to deterministic checks; AI's added value must be separately measurable.

## Metrics and protocol

- Detection: per-fault family recall; true/false positives; false-positive rate on clean controls; untested/unsupported coverage; macro/micro scores with denominators.
- Localization: affected key and column precision/recall; step localization Top-1/Top-3 only when intermediate evidence exists, with `unknown` tracked separately.
- Diagnosis: ground-truth root-cause accuracy, evidence citation validity, correct suggested correction category, number of tool invocations, latency and model cost; AI vs fixed rules with equal evidence.
- Execution: run time by stage, memory/CPU, cleanup success, deterministic idempotency, reproducibility across three fresh runs of selected fixtures; failures and timeouts included in denominators.
- Human experience: time to understand a failed migration from a small blind manual study if feasible; label subjective measurements as exploratory.

Declare dataset/version, fault seed, input SHA, oracle + binding digests, image versions, configured B1 aggregate fields, baseline applicability and model/config **before** evaluation. Use matched evidence budgets for rule-only vs AI diagnosis; evaluate the change-analyst role separately because its benefit is unproven. Freeze a holdout subset before tuning. Include negative examples where DataProof fails or flags false alarms. Require a single machine-readable `benchmark-results.json` + report generator and a one-command rerun in a fresh environment once code exists.

## Benchmark acceptance

The final README may publish measured results only after reproducible full runs. No minimum "beat baseline by X%" is assumed; if AI is not useful in some categories, say so and keep it advisory. Honest negative results are preferable to invented accuracy.
