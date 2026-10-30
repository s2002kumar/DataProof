# ADR-0003: Full-stack previews with restricted hosted execution

**Status:** Accepted (2026-10-30). Amended by ADR-0006: the Fargate design is the proposed hosted target; local Docker is the first execution backend and cloud provisioning is approval-gated.

**Decision:** Support configured FastAPI/PostgreSQL/frontend application profiles with versioned manifests, exact commits and allowed migration roots. Run reference and candidate as isolated ephemeral environments. Local Docker for trusted development; two restricted AWS Fargate tasks with protected pinned runtime/dependency images and isolated, offline build/execution of exact-SHA source overlays (ADR-0004). No arbitrary public GitHub-repo build/run API and no production database copy/credentials.

**Why:** Keeps a live, reviewable full-stack preview while preventing a public code-execution service from being exposed as a demo.

**Alternatives rejected:** Arbitrary PR Dockerfile execution on shared web host; Docker-in-Docker on Fargate; RDS clone of sensitive production customer data; production API comparison; indefinitely running previews.

**Consequences:** App profile support is not universal. Need authenticated preview gateway, quotas, TTL, prebuilt image provenance, cleanup and negative security tests.
