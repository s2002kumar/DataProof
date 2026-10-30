# ADR-0002: AI is advisory; deterministic code owns execution and verdict

**Status:** Accepted (2026-10-30).

**Decision:** Change Analyst and Failure Investigator are separate logical AI roles with typed outputs; correction adviser is a mode. A deterministic orchestrator owns workflow transitions, tool authorization and DB operations. AI can request only bounded read-only diagnostic tools, cite evidence, suggest checks and corrections. It cannot run arbitrary shell, write to production, alter approved contracts, mark checks passed, merge or deploy.

**Why:** Retains valuable agent reasoning while preventing a prompt, malformed SQL comment or hallucinated result from controlling privileged effects.

**Alternatives rejected:** Model-generated runnable SQL/seeds in privileged context; natural-language `FLOW_STOP` as authoritative gate; eight autonomous infrastructure agents; AI-defined pass/fail thresholds.

**Consequences:** Need tool registry, permission middleware, evidence schema and injection tests. If AI fails/unavailable the deterministic check report remains valid but diagnosis may show `NOT_AVAILABLE`.
