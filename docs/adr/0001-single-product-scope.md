# ADR-0001: One complete DataProof product

**Status:** Proposed for approval (2026-10-28).

**Decision:** Build one PostgreSQL application database migration safety product: GitHub PR analysis, full-stack reference/candidate shadow previews, deterministic data + API behavior checks, bounded AI investigation, verified reproduction/approved correction, evidence dashboard, benchmark, restricted AWS deployment. No public arbitrary DB engines or production cutover.

**Why:** Keeps one end-to-end developer experience (pull request to live preview to evidence) while providing a distinct, measurable focus on silent data corruption. One coherent story is better than two adjacent platforms.

**Alternatives rejected:** (a) Generic Postgres-to-DuckDB/BigQuery diff service; broad warehouse adapters dilute product focus. (b) Pure SQL checker; lacks real execution. (c) Eight agents coordinating through natural language; increases ambiguity and permissions risk. (d) Bare database-only preview; weakens full-stack user experience.

**Consequences:** Full-stack preview raises cost and execution/security complexity; requires supported project manifest, fixed demo app and hosted allowlist. Independent benchmark is mandatory.
