# ADR-0004: Run PR-head source under immutable, credential-less runtime

**Status:** Adopted for design; implementation must validate security assumptions (2026-10-29).

**Context:** A complete FastAPI + Next.js candidate preview must reflect the PR head code. Simply selecting a prebuilt image with unchanged app code cannot show new routes, and `next start` cannot serve newly edited Next.js source without a corresponding build artifact. Building user-supplied Dockerfiles with PR-accessible push credentials is unacceptable.

**Decision:** Use a protected, immutable Fargate runtime/dependency image (digest and dependency-lock digest in trusted `dataproof.yml`). Trusted ingestion fetches the exact base/head SHA archive and stages digest-verified source as a run-scoped artifact. Separate reference and candidate Fargate tasks, each containing app services and PostgreSQL sidecar, consume that artifact without GitHub installation token or AWS **task role**. Candidate source overlay runs under unprivileged processes; `uvicorn` imports pinned-dependency Python and `next build` compiles pinned-dependency TypeScript **offline inside the isolated task**, then `next start` serves that build. No user-controlled Dockerfile or package installation runs. The host/control API never imports untrusted code.

Changes to any `Dockerfile`, runtime/dependency manifest, lockfile or outside `allowed_change_roots` must be rejected as `NOT_EXECUTED: DEPENDENCY_CHANGE_REQUIRES_IMAGE_APPROVAL`, requiring a separate trusted protected-branch build/digest approval. Fork PRs are not executed. A **trusted source/evidence broker** obtains archives and seals `pg_dump` snapshots; no network token or cloud credential is given to untrusted app processes. No NAT egress; VPC endpoint/bucket policies prevent unauthorized external bucket access. Execution role for ECR/CloudWatch is separate from absent app task IAM role. An independent scheduled reconciler enforces TTL.

**Alternatives rejected:** (1) Run PR Dockerfile/BuildKit/Kaniko from untrusted PR in an environment with push credentials; (2) let the control plane check out/import PR source; (3) use `next start` against stale prebuilt output; (4) enable Docker-in-Docker in Fargate.

**Consequence:** Supported PRs cannot introduce arbitrary new dependencies; preview compilation has measured latency and CPU cost. Verify offline Next build behavior with the chosen Next.js and pnpm versions in real tasks before marking this secure or working. Never claim arbitrary repo support.
