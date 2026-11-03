# dataproof-investigator

The bounded AI investigator (ADR-0002), in two roles:

- **The Failure Investigator** explains why deterministic checks failed, using real evidence.
- **The Change Analyst** names plausible risks in a change, and edge cases to test, before anything runs.

Neither can decide anything: the loop never receives a handle to a check, a verdict, a binding approval or the run. Its only output is an `Investigation` record (see the contracts package), with its role.

## The loop

`Investigator(model, budget).investigate(run_id, context)` owns every decision:

1. **Prompts.** The system prompt (versioned `PROMPT_VERSION`) tells the model that everything the tools return is data from the pull request, never instructions. The task lists only check IDs, outcomes, counts and evidence IDs, all written by deterministic code. Pull-request text reaches the model only through tools, as JSON data.
2. **Tools.** The model may call **exactly five** read-only tools (`TOOL_SPECS`, `TOOL_VERSION`). Each call's arguments are validated against a strict schema before anything runs. Unknown tools and invalid arguments are `DENIED`, and allowed calls that fail are `FAILED`; either way, the model gets an error result. Successful calls yield evidence IDs `investigation.tool.<n>`.
3. **Budget.** A budget caps tool calls and turns and, optionally, cost (`max_cost_usd`); each ends the investigation as `BUDGET_EXHAUSTED`. Before each turn the session reports the most that turn could cost (`ModelSession.ceiling_usd`), and a turn that could take the investigation past the cap is not sent. The Anthropic adapter bounds a turn by the last turn's input (cached or not) and output tokens, plus every byte of the new tool results (a token is at least one byte), plus a fixed allowance for request framing, all priced as cache writes, the dearest input, plus `max_tokens` of output. With fallbacks on, another model at another price may answer, so there is no bound. Then, and for models without one, cost is also checked after every turn: the turn that crosses the cap is counted, but its tool calls are not run.
4. **Answer.** The final answer must parse as `InvestigationOutput`: hypotheses with confidence, an optional suggested action, and a caveat. **Every** cited ID must be either the run's recorded evidence or a successful tool call of this investigation. One unknown citation makes the whole answer `INVALID_OUTPUT`, and nothing of it is shown.
5. **Failures.** A refusal, a provider error or a truncated answer is `NOT_AVAILABLE` or `INVALID_OUTPUT`. The deterministic report stands either way.

## The five tools

| Tool | Reads | Guard |
| --- | --- | --- |
| `get_check_result(check_id)` | A recorded `CheckResult` | Only what deterministic code recorded |
| `get_evidence(evidence_id)` | A recorded evidence record | Only what deterministic code recorded |
| `get_schema(environment, table)` | Columns and constraints | Fixed catalog queries, table name bound as a parameter, read-only transaction |
| `read_source(path, start_line, end_line)` | The pinned head commit's staged archive | Normalized relative paths under the allowlisted application and migration roots only (never contracts, bindings, docs, benchmarks or test labels); at most 200 lines |
| `run_readonly_query(environment, sql, max_rows)` | The reference or candidate database | See below |

`run_readonly_query` passes four layers:

1. PostgreSQL itself proves the query is one plain query with no hidden reads. This is `check_read_query`, the same check as bindings: no user-defined functions, no system catalogs, built-in functions from the allowlist only.
2. It runs as a prepared statement in a read-only transaction.
3. It runs as the read-only role, with a 5-second timeout.
4. Results are capped at 50 rows and 64 KiB.

## In a run

The runner moves a run from `VERIFYING` to `INVESTIGATING` only when a required check failed or errored. The model-off diagnosis always runs there. When the pipeline is given an `Investigator`, the model also investigates the **live** environments through the read-only role, and the head commit's source under `ProjectConfig.investigator_source_roots`. Both investigations are written to `run_investigations` and listed in the report as `advisory`. The report's verdict and checks are computed from the checks alone.

Earlier, in `ANALYZING`, the Change Analyst's records are written the same way, with the `CHANGE_ANALYST` role: the risk rules on every run that passed the change policy, and the model when the pipeline is given an `analyst` (`change_analyst(model)`). The report lists every record with its role.

## Models

- `InvestigatorModel` / `ModelSession` is the provider interface. An adapter keeps its own conversation state.
- `ScriptedModel` plays a fixed script of turns and makes no network call. Tests use it, including hostile scripts.
- **No paid model is called without the owner's approval** (ADR-0006 owner decision).

### Claude (opt-in)

`AnthropicModel(client)` (install the `anthropic` extra: `dataproof-investigator[anthropic]`) runs the investigation on Claude Opus 5.5 (`claude-opus-5-5`) through the Anthropic SDK's Messages API:

- **Thinking:** adaptive, at `high` effort. Every assistant turn is passed back unchanged, thinking blocks included.
- **Tools:** the five tools are `strict` and `tool_choice` stays `auto`, because forced tool use is rejected on this model. Keywords strict schemas do not accept are removed from what is sent and still enforced client-side.
- **Answer:** the final answer is constrained to the `InvestigationOutput` JSON schema, then validated and citation-checked like any other.
- **Fallbacks:** server-side refusal fallbacks (`fallbacks: "default"`) are on by default; pass `fallbacks=False` to turn them off. The investigation records the model that actually served it.
- **Prompt caching:** automatic, through a top-level `cache_control` (5-minute entries); pass `cache=False` to turn it off. Each turn re-sends the conversation so far, and the cached prefix is billed at the cache-read price.
- **Cost:** every turn records its tokens and cost at first-party list prices. For Claude Opus 5.5 that is, per million tokens: $4 input, $5 for a cache write (1.25 times input), $0.20 for a cache read and $20 output. A turn's input tokens count every prompt token, cached or not.

Nothing configures it automatically. Using it needs credentials (`ANTHROPIC_API_KEY` or an `ant auth login` profile), and, for this project, the owner's approval of the spend. Its tests (`tests/unit/test_anthropic_adapter.py`) replace the HTTP transport with a mock and make no network call.

## Change Analyst

The original design gives the AI two roles (ADR-0002, ARCHITECTURE "AI investigator"): an advisory Change Analyst before execution, and the Failure Investigator after checks. The Change Analyst is the same bounded loop with a different role, not a separate agent framework. It runs while the run is `ANALYZING`, after the change policy has passed and before any environment exists, and both of its arms are recorded with the `CHANGE_ANALYST` role:

- **`risk_rules(run_id, files, migration_roots)`, on every eligible run:** deterministic static risk rules (`RULES`, model `dataproof:risk-rules-v1`) over the changed Python files under the readable roots.
  - In a migration, only `upgrade()` is read; `downgrade()` undoes it.
  - Each rule matches a pattern known to go wrong in data migrations: a join or an aggregate in a backfill, rounding or fewer decimal places, a time-zone conversion, a filtered backfill, repeated inserts, a row-level patch by hard-coded key, a key column without a reference or uniqueness constraint, de-duplication, NULL and empty-string coercion, and dropped data.
  - In application code it also matches rounding calls (`round`, `quantize`, `floor`, `trunc`).
  - Each hypothesis cites the lines it matched (`source:<path>:L<start>-L<end>`, at the head commit), and the suggested action lists the rules' edge cases. No match is `NOT_AVAILABLE`, not a finding.
- **`change_analyst(model)`, only when a model is configured:** the investigation loop with its own prompt (`change-analyst/1`), a task that lists the readable changed paths, and **one tool, `read_source`**.
  - There is no database to query yet, and any other tool call is `DENIED`.
  - Every hypothesis must cite its own `read_source` evidence, so an analysis that cites anything else is `INVALID_OUTPUT` and is not shown.
  - Each hypothesis is one risk with where it is and the edge case that would show it. The suggested action lists edge cases worth a contract or a fixture row, and LOW confidence means "looks safe, but check".
  - Default budget: 8 tool calls, 10 turns.

**What it is worth, and what it is not:**

- **Rules:** the [change-analysis benchmark](../../benchmarks/README.md#change-analysis) anticipates 21 of 22 corpus faults, but also alarms on 6 of 12 clean controls. A rule cannot tell a correct ranged backfill from one that skips its boundary. The rules were written with the corpus in view, so that recall is an optimistic bound.
- **The model arm** is built and tested with scripted models only. Whether it is more precise than the rules waits for an approved model budget.
- **Either way:** nothing it says changes a check or the verdict, and a change that passes analysis can still fail its contracts.

## Model-off baseline

`diagnose(run_id, checks)` is the deterministic diagnosis that investigations are measured against (gate G3). It is also shown when no model is configured. It recognizes these mismatch shapes and cites the check's evidence:

- exact multiples: a join that repeats rows;
- values rounded to whole units: lost scale;
- whole-hour shifts: a time-zone conversion of instants;
- values moved between keys: matching by a non-unique order;
- missing or extra keys.

## Tests

`tests/integration/test_investigator.py` runs against real PostgreSQL, through a role with `pg_read_all_data` and read-only sessions.

- **Investigation of F1:** reads the check, the query result showing 99.98, the migration source and the ledger schema, and cites them.
- **Refused queries:** writes, multi-statement strings, data-modifying CTEs, `pg_read_file`, catalogs, `COPY`, `dblink`, `pg_sleep` and `CREATE TABLE AS` are all refused, and the data stays unchanged.
- **Refused source reads:** paths outside the roots, path traversal and oversized line ranges are refused.
- **Refused calls:** unknown tools and smuggled arguments are refused.
- **Prompt injection:** a model that obeys an instruction injected into the migration source is denied `approve_binding`, and its uncited "all checks PASS" answer is rejected. The checks remain unchanged.
- **Loop limits:** invalid and uncited answers, the budget, refusals and provider failures each end with the right status.
- **Baseline:** it names F1, F3, the decimal fault and the timestamp fault.

`tests/unit/test_change_analyst.py` covers the Change Analyst, offline:

- **Rules:** each rule matches its pattern and not a plain copy. Two inserts into one table, or a self-copy, are repeated inserts. Only `upgrade()` is analyzed, rounding in changed application code is found, and matches cite their lines with valid evidence IDs. No match is not a finding.
- **Reads:** only changed files under the readable roots are read, never bindings or documentation.
- **The model arm:** it is offered `read_source` alone. Database queries, check results, approvals and reads of bindings are denied, and an analysis citing anything but its own reads is not shown.

`tests/integration/test_pipeline.py` (Docker) runs a scripted Change Analyst on F1 in a real run. The rules see F1's join and sum, the model reads the migration and is denied a database query, and the verdict stays `BLOCKED`.
