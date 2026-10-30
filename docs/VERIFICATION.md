# VERIFICATION — What Counts as Proof

## Non-negotiable logic

A database migration is never 'safe' in the abstract. A run only establishes that **specific checks** passed for an exact commit, database snapshot, configuration, engine and observation window. The verdict cannot be provided by an LLM. A valid migration may intentionally change table counts and schemas; the rules must match the expected transformation, not naive equality.

## Primary application scenario

A commerce service migrates from legacy order payment/refund storage to an explicit transaction ledger. Domain truth is defined by previously established transaction semantics. Examples (to turn into contract fixtures before code):

- For each order ID, captured payments minus refunds is preserved to the cent, except explicitly authorized business adjustments.
- Each source payment/refund event maps to the expected target entry/entities (support composite event keys, stable business identity, and declared intentional one-to-many mappings).
- All target entries reference a real order and belong to the same tenant.
- No source refund disappears or is multiplied through a join.
- Decimal values use exact NUMERIC/Decimal arithmetic; rounding policy must be explicit.
- Timestamp instants remain equal under expected timezone conversion; representation differences alone do not fail.
- Declared API endpoints (`GET /orders/{id}`, `/orders/{id}/refunds`, totals summary) return semantically equivalent values between reference and candidate after normalization; endpoints allowed to change must be explicitly excluded or mapped.
- Clean migrations with intentional schema restructures pass when mapped contracts pass, even when raw counts differ.

## Verification tiers

1. **Static risk findings:** parse supported SQL constructs, identify dropped columns, casts/precision narrowing, NOT NULL, FK/index changes, unsafe transaction/locking patterns and dependency breakages. These findings are advisory and require observed execution/context to confirm.
2. **Execution:** SQL applied on disposable PostgreSQL with timeouts and correct migrator privileges; record every migration step and any error. An exception is not evidence of data correctness.
3. **Schema:** target schema matches declared version and required constraints; introspect authoritative PostgreSQL catalogs. Intentionally removed columns are checked against mapping policy.
4. **Data:** row counts, keys, nulls, duplicate keys, FK/orphan checks, deterministic relational mappings, exact decimals, typed timestamps, deliberate scalar/range tolerances and explicit business invariants.
5. **Application behavior:** selected before/after API request/response comparisons at stable endpoints; HTTP 200 alone is insufficient. Normalize nondeterministic IDs/order/timestamps only by reviewed rules.
6. **Reproduction:** candidate subset + dependent rows actually replays the *same invariant violation* in fresh isolated runner; otherwise label `NOT_REPRODUCED` and explain why.
7. **Correction replay:** a newly submitted developer-approved SQL correction executes in a fresh environment with the same dataset/contracts; new evidence and new result. Never revise old evidence.

## Row matching and canonicalization

- Prefer declared stable primary or business keys; composite keys are tuples of typed values, not delimited string concatenations.
- If stable row identity is unavailable, use well-defined bag/multiset checks or business aggregates and report `NOT_RUN` for unsupported row-level localization. Never claim exact row attribution for ambiguous keyless duplicates.
- Decimal normalization preserves exact scale and currency policy; do not cast monetary sums to IEEE float.
- Timestamp rules define UTC instant vs local wall time, DST assumptions and allowed precision change.
- NULL remains distinct from empty string and 0 unless an explicit policy says otherwise.
- For structured fields, JSON equality and string Unicode normalization must be declared, not silently standardized.
- Hashes help large-scale screening but any found mismatch needs exact keyed query evidence; hash equality is not a universal semantic guarantee.

## Contract governance — two-part trust boundary (ADR-0005)

Each `DataContract` contains:

- `oracle` (base-SHA protected): a read-only reference query or reference-derived expected keyed values, coverage universe, business invariant, datatype/precision/threshold, severity and requiredness. The PR cannot modify these.
- `candidate_binding` (PR-supplied and **untrusted**): restricted candidate read query/view mapping and selected candidate API route/field selectors. The binding can express new table/view names but **cannot redefine the expected source key universe**, comparator, tolerances or requiredness. It must account for every oracle key; missing coverage is `FAIL`/`ERROR`, never silently excluded.

If a schema change introduces a new candidate binding, the runner may perform the migration/preview but the result goes to `AWAITING_BINDING_REVIEW` with **report `INCOMPLETE` and non-green GitHub check**. An authorized independent reviewer inspects the binding and explicitly records `HumanDecision(action=APPROVE_BINDING, head_sha, binding_digest, actor, time)` or rejection. On approval a new config digest/new immutable verification attempt is created; the old incomplete outcome is preserved. Candidate SQL is executed only as a restricted read-only query against isolated candidate data, with statement timeout and bounded results. If binding cannot be safely evaluated, return `ERROR`/`NOT_RUN`, not PASS.

**Reference-first rule:** For any invariant that is **not** reference/candidate equivalence (example: `refund_total <= capture_total`), evaluate it first against the reference fixture. If reference fails, emit `ERROR: ORACLE_VIOLATED_ON_REFERENCE` attributed to fixture/contract, **not** PR-induced corruption. Only then test candidate. No required check may be reported green if not executed.


## Check outcome taxonomy

- `PASS`: executed, expected condition demonstrably satisfied for this snapshot.
- `FAIL`: executed, supported condition demonstrably violated, with deterministic evidence.
- `ERROR`: technical problem prevented a reliable check; required errors block completion.
- `NOT_RUN`: unsupported, excluded or timed out; required missing checks produce `INCOMPLETE`. If no trusted approved candidate binding exists, checks requiring it are `NOT_RUN: AWAITING_BINDING_REVIEW`.

Check severity, requiredness and thresholds are versioned reviewed input. AI may suggest a new check but cannot edit an approved check definition or change pass/fail thresholds during the same evaluation. New checks trigger re-review and a new run/config digest.

## Evidence fields

Each failure includes: check ID and rule version, SQL source location/step ID if known, affected key count, exact bounded examples, query and digest, `source`/`candidate` typed values, privacy/redaction status, checked dataset size, runtime, evidence hashes and whether inference or observation supports step localization.

## Honest limits

Snapshots do not prove production-scale locking, traffic race conditions, transaction isolation edge cases, full workload performance or untested application paths. Report these as limitations, not a passing safety score.
