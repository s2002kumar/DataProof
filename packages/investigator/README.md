# dataproof-investigator

The bounded AI investigator (ADR-0002). It explains why deterministic checks failed, using real evidence, and it cannot decide anything: the loop never receives a handle to a check, a verdict, a binding approval or the run. Its only output is an `Investigation` record (see the contracts package).

## The loop

`Investigator(model, budget).investigate(run_id, context)` owns every decision:

1. **Prompts.** The system prompt (versioned `PROMPT_VERSION`) tells the model that everything the tools return is data from the pull request, never instructions. The task lists only check IDs, outcomes, counts and evidence IDs, all written by deterministic code. Pull-request text reaches the model only through tools, as JSON data.
2. **Tools.** The model may call **exactly five** read-only tools (`TOOL_SPECS`, `TOOL_VERSION`). Each call's arguments are validated against a strict schema before anything runs. Unknown tools and invalid arguments are `DENIED`, and allowed calls that fail are `FAILED`; either way, the model gets an error result. Successful calls yield evidence IDs `investigation.tool.<n>`.
3. **Budget.** A budget caps tool calls and turns and, optionally, cost (`max_cost_usd`); each ends the investigation as `BUDGET_EXHAUSTED`. Cost is checked after every model turn. The turn that crosses the cap has already been paid for and is counted in the usage, but its tool calls are not run. A caller holding a hard spending limit therefore reserves one more turn's worst case.
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
- **Cost:** every turn records tokens and cost at first-party list prices ($4 and $20 per million input and output tokens for Claude Opus 5.5).

Nothing configures it automatically. Using it needs credentials (`ANTHROPIC_API_KEY` or an `ant auth login` profile), and, for this project, the owner's approval of the spend. Its tests (`tests/unit/test_anthropic_adapter.py`) replace the HTTP transport with a mock and make no network call.

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
