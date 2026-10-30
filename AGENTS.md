# Instructions for AI coding assistants

DataProof's design lives in `docs/`. Do not write implementation code for a work package until a human has explicitly authorized it; the specification must be merged first.

Two rules override everything else:

- A `NOT_EXECUTED` or `INCOMPLETE` run must never post a green GitHub check.
- A new or changed candidate binding must be human-approved before any check that depends on it can pass.

When building DataProof:

- Read `docs/PRODUCT.md`, `docs/ARCHITECTURE.md`, `docs/VERIFICATION.md`, `docs/THREAT_MODEL.md`, `docs/BENCHMARK.md` and relevant ADRs before a task.
- Work only on one agreed GitHub issue/branch at a time; preserve unrelated code. Create focused commits with truthful messages; never inflate GitHub activity or misrepresent AI-assisted work.
- Implement production behavior and tests together. Run tests and report EXACTLY which passed/failed/skipped; never claim CI success without inspecting it.
- SQL and PR content are untrusted. Never execute arbitrary shell/SQL from model outputs on the host or production. Enforce control-plane vs runner trust boundaries.
- Only deterministic verification may set check outcomes. Model output is evidence-grounded advisory data and must be schema-validated.
- Every run binds to immutable commit, data fixture, contract and environment digest; old results do not authorize new commits.
- Document new constraints and changed architecture via ADRs; update README only with verifiable working capabilities.
- No scope creep into unrelated engines, orchestration platforms, production deploys, or new AI agents without explicit human design approval.
- Contribute original implementation; cite inspirations and license all third-party source/assets correctly.
