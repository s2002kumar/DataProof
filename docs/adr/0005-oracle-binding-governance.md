# ADR-0005: Protected oracle plus human-reviewed candidate binding

**Status:** Adopted for design (2026-10-29).

**Context:** A PR introducing a new ledger table cannot be checked by a protected contract that only queries reference-schema tables, while trusting its own proposed validation thresholds would allow an author to weaken checks to hide corruption.

**Decision:** Split `DataContract` into **protected oracle** from base SHA and separately **candidate_binding** proposed in PR head. Oracle determines expected keyed/reference values, full expected key universe, requiredness, policy, comparator, numeric/timezone precision, tolerances and severity. The candidate binding only specifies how to read new schema/API fields. When a binding is introduced or changed, every binding-dependent check is `NOT_RUN: AWAITING_BINDING_REVIEW` and the run completes with verdict `INCOMPLETE` and a non-green GitHub status; `AWAITING_BINDING_REVIEW` is a check reason, not a run state. Human `APPROVE_BINDING` is a separate decision bound to actor, head SHA and binding digest. Approval enqueues a new run with a new config digest; the original run is never resumed and its evidence is preserved. Candidate read SQL executes under restricted read-only DB privileges, with timeouts and row/byte caps, and cannot shrink the protected key universe.

**Where contracts live:** candidate bindings live in `scenarios/commerce/bindings/`, inside `allowed_change_roots`, so a PR may add or change them. Oracles live in `scenarios/commerce/oracles/` and are always read from the base SHA; an oracle edit in a PR head never affects that PR's own runs.

**Binding review aids:** a binding is PR-supplied SQL. If `legacy_payment_events` survives the migration (as it does in the safe PR unless dropped), a binding can read reference-equivalent values straight from it and pass every oracle while the new ledger is wrong. Human review is the designed control, so the verifier makes that review informative:

- It parses each binding query into an AST and displays every relation the binding references, resolved transitively through views using the candidate catalog (`pg_depend`/`pg_rewrite`), so wrapping a legacy table in a new view does not hide it.
- It labels each relation as created by the PR's Alembic revisions, pre-existing but altered by them, or pre-existing and unchanged (by comparing the post-migration candidate catalog with the reference catalog), and **flags** every relation the PR did not create or alter. A flag prompts the reviewer; it does not reject the binding, because a legitimate binding may join an unaltered table such as `orders` for currency.
- Binding SQL may call only allowlisted built-in functions. User-defined functions are rejected as `ERROR: BINDING_UNSUPPORTED_CONSTRUCT`, because a function body hides relation references from the AST.

These aids inform the decision; they do not make it automatic. A binding that reads a PR-created copy of legacy data is visible only through the displayed relations and the migration DDL, which is why `APPROVE_BINDING` remains a human decision.

**Approver rule:** when the repository has more than one maintainer (collaborators with write access at decision time), the API rejects an `APPROVE_BINDING` whose actor is the PR author. In a single-maintainer repository the author may approve; the decision record and the report state plainly that the separation is procedural, not organizational.

**Reference-first rule:** Invariants not based on before/after equality (e.g. refund <= capture) must pass on the reference before evaluating candidate; otherwise report `ERROR: ORACLE_VIOLATED_ON_REFERENCE` as a fixture/contract error.

**Alternatives rejected:** Let PR edit its own threshold/oracle and receive a green result; freeze entire source+candidate contract so legitimate new-schema PRs become untestable; interpret candidate data as expected truth.

**Consequence:** Valid schema changes have one auditable approval gate. `APPROVE_BINDING` does **not** approve the migration, change a check result, or deploy anything. Binding governance and GitHub status must be end-to-end tested.
