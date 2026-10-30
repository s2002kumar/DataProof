# ADR-0005: Protected oracle plus human-reviewed candidate binding

**Status:** Adopted for design (2026-10-29).

**Context:** A PR introducing a new ledger table cannot be checked by a protected contract that only queries reference-schema tables, while trusting its own proposed validation thresholds would allow an author to weaken checks to hide corruption.

**Decision:** Split `DataContract` into **protected oracle** from base SHA and separately **candidate_binding** proposed in PR head. Oracle determines expected keyed/reference values, full expected key universe, requiredness, policy, comparator, numeric/timezone precision, tolerances and severity. The candidate binding only specifies how to read new schema/API fields. When introduced or changed, it triggers `AWAITING_BINDING_REVIEW` and `INCOMPLETE` with a non-green GitHub status. Human `APPROVE_BINDING` is a separate decision bound to actor, head SHA and binding digest. Approval starts a new verification attempt with new config digest and preserves old evidence. Candidate read SQL executes under restricted read-only DB privileges, with timeouts and row/byte caps, and cannot shrink the protected key universe.

**Reference-first rule:** Invariants not based on before/after equality (e.g. refund <= capture) must pass on the reference before evaluating candidate; otherwise report `ERROR: ORACLE_VIOLATED_ON_REFERENCE` as a fixture/contract error.

**Alternatives rejected:** Let PR edit its own threshold/oracle and receive a green result; freeze entire source+candidate contract so legitimate new-schema PRs become untestable; interpret candidate data as expected truth.

**Consequence:** Valid schema changes have one auditable approval gate. `APPROVE_BINDING` does **not** approve the migration, change a check result, or deploy anything. Binding governance and GitHub status must be end-to-end tested.
