# THREAT MODEL — Untrusted Code, SQL, AI, GitHub and Cloud

## Assumptions and assets

The repo content, SQL, pull-request body, migration comments and AI tool-visible text are untrusted. The public reviewer UI may be reachable from the internet. Important assets: GitHub installation credentials, cloud IAM, execution host, non-sensitive fixture databases, preview URLs, evidence files, internal control-plane DB, LLM API key and billing budget. No real production secrets or personal data in demo baselines.

## Threat-to-control map

| Threat | Primary controls | Verification gate |
|---|---|---|
| Malicious PR changes `Dockerfile`, dependency manifests or lockfiles | Immutable runtime digest, `allowed_change_roots`, protected configuration; unapproved dependency/runtime changes → `NOT_EXECUTED: DEPENDENCY_CHANGE_REQUIRES_IMAGE_APPROVAL` | PR changing Dockerfile or lockfile is rejected before task creation |
| Candidate Python/Next source performs hostile commands during import/build | Source executes/compiles only inside untrusted task with **no task IAM role**, no NAT/egress, no GitHub/LLM keys, restricted ephemeral DB privileges, bounded CPU/disk/TTL; never on API host | Attempted network egress, host access, and secret discovery all denied |
| SQL exfiltration or cross-run network access | Isolated per-run tasks/networks, restricted egress and IAM, disposable DB credentials, no production connections | External network/other-run DB access denied in tests |
| Container breakout / resource exhaustion | Separate reference and candidate Fargate tasks; CPU/memory/disk/time caps, nonroot processes, no privileged mode/ECS Exec; **independent EventBridge scheduled** TTL reconciler kills tasks even if API is down | Resource exhaustion, API-down orphan cleanup and separation tests |
| Prompt injection in SQL/comments/evidence | Treat content as data; allowlisted diagnostic tools; deterministic policy middleware; output schema validation | Injected instruction cannot change run state, perform write or change verdict |
| Forged GitHub event or stale PR commit | Verify HMAC signature/install/repo, idempotent delivery, exact SHA binding, supersession cancellation | Tampered event rejected; old run cannot approve new SHA |
| Evidence tampering / stale artifacts | Content-addressed/sealed manifests, run-specific prefixes, atomic commit, no read of previous run artifacts | Cross-run evidence/stale-run test |
| Hidden data leak in logs/reports/LLM | Redact credentials and high-risk fields; bounded samples; private artifacts, prompt data minimization | Secret canary absent from output and traces |
| Preview gateway accepts external address (SSRF/open proxy) | Trusted supervisor registers task private IP/port; signed expiring token bound to run+role; gateway rejects all client-supplied upstream hosts | Attempted internal URL, unregistered task or stale token denied |
| Preview URL unauthorized access | Auth gateway, expiration, token binding to run, private network for backend/DB | Unauthorized requester denied |
| AI-generated SQL migration fix unsafe | Suggestion only; human review before new SHA/correction; deterministic replay in fresh environment | No model-controlled production or auto-merge path |
| Cost abuse / abandoned environments | Hard supervisor concurrency limit, rate limits, AWS Budgets **alarm (not a hard cloud spend stop)**, capped task runtime, independent TTL reconciler | Expired tasks cleaned, alerts verified; measured peak spend tracked |

## Trusted boundaries

1. GitHub input -> authenticated control API.
2. Control API -> durable supervisor/job broker.
3. Supervisor -> **untrusted isolated execution plane**. Trusted broker fetches exact GitHub SHA and verifies source digest; candidate runs no unapproved Dockerfile or dependencies; it has no task IAM role, installation token, control-plane DB credential or AWS admin token. Each migrator has only a non-superuser owner role limited to its disposable DB (no `COPY FROM PROGRAM`, `pg_read_file`, `lo_import`, untrusted extensions); read queries use a separate least-privileged role. Execution-role permissions for ECR/logs remain outside task code. S3 access is via trusted broker or narrowly scoped artifact capability, not arbitrary AWS credentials.
4. Evidence store -> AI investigator. Model receives bounded/redacted data; typed tools are **exactly** `get_check_result(id)`, `get_evidence(id)`, `get_schema(env, table)`, `read_source(path, start, end)` (pinned head SHA, **allowlisted application/migration source paths only**; never `benchmarks/`, hidden labels, `docs/`, or test fixture labels), `run_readonly_query(env, sql, max_rows)`. SQL is AST-validated (single permitted `SELECT`/`WITH`, disallow write-CTEs and `COPY`, `pg_read_*`, `lo_*`, `dblink`, `postgres_fdw`); a separate database read-only role, `default_transaction_read_only=on`, statement timeout, restricted `work_mem`, max rows and **max result bytes** supply defense in depth. Each model output must cite actual evidence IDs and is schema-validated; tool/token budgets and pinned model/prompt versions recorded. LLM key is held in investigator service only.
5. Reviewer -> signed/authenticated preview and report. No bypass through raw public container ports.

## Cloud deployment restriction

A public service that builds/runs arbitrary GitHub repositories cannot be considered secure simply because Docker is present. Fork PRs are `NOT_EXECUTED`. Private preview ingress is restricted to authenticated run-bound gateway; AWS private subnets have no NAT and allowed VPC endpoint policies are scoped to project resources. Each environment (reference/candidate) is a distinct task and cannot see the other task database. Runtime dependencies are protected/pinned; a Next.js source change is compiled offline inside the isolated task, never on the control plane. GitHub App asks only `checks:write`, `contents:read`, `pull_requests:read`, `metadata:read` permissions and verifies installation/repo scope. The committed project is a **restricted configured-repository integration and allowlisted public demo**. Broad public multi-tenant execution is out of product scope. AWS task-sidecar isolation, image provenance, exact permissions and cost/rate controls must be demonstrated, not just documented.

## Mandatory security tests

Webhook forgery, stale SHA, injected AI instructions, `DROP`/`COPY`/extension abuse attempts, lockfile/Dockerfile changes denied, malicious app-build/import egress attempt, cross-run DB access, unauthorized signed gateway proxy targets, resource exhaustion, API-down orphan cleanup, task timeout/cancel, replay retry, reference-oracle violation, fail-before-cleanup, evidence-scope bleed and preview-token expiry. Any unmitigated critical escape or secret exposure blocks cloud launch.
