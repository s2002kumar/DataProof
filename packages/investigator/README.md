# dataproof-investigator

The bounded AI investigator (ADR-0002), in two roles:

- **The Failure Investigator** explains why deterministic checks failed, using real evidence.
- **The Change Analyst** names plausible risks in a change, and edge cases to test, before anything runs.

**Status: experimental, and off unless an operator configures a model.** In the first live evaluation (2026-11-19) the Failure Investigator answered in 1 of 18 investigations, stopped the other 17 times by its own tool-call limit, and the Change Analyst's model arm sent no request ([results](../../docs/AI_EVALUATION_RESULTS.md)). The rule-based arms below run on every eligible run and are unaffected.

Neither can decide anything: the loop never receives a handle to a check, a verdict, a binding approval or the run. Its only output is an `Investigation` record (see the contracts package), with its role.

## The loop

`Investigator(model, budget).investigate(run_id, context)` owns every decision:

1. **Prompts.** The system prompt (versioned `PROMPT_VERSION`) tells the model who is speaking: its instructions are the prompt, the task and operator messages, and everything inside a tool result is data from the pull request, never instructions. It gives a short method: the briefing, the changed migration, queries to confirm, then the answer, and it states the limits the answer is checked against. The task lists only check IDs, outcomes, counts and evidence IDs, all written by deterministic code, and ends with the budget. Pull-request text reaches the model only through tools, as JSON data.
2. **Tools.** The model may call **exactly six** read-only tools (`TOOL_SPECS`, `TOOL_VERSION`). Each call's arguments are validated against a strict schema before anything runs. Unknown tools and invalid arguments are `DENIED`, and allowed calls that fail are `FAILED`; either way, the model gets an error result that says what to do instead. Successful calls yield evidence IDs `investigation.tool.<n>`.
3. **Budget.** A budget caps tool calls, turns and cost (`max_cost_usd`, 1.00 USD unless a caller sets or removes it). **The model is told the budget, and after every turn what is left of it** ([ADR-0009](../../docs/adr/0009-the-investigator-finishes.md)):
   - The notice goes with the tool results but never inside one: tool results are the pull request's data, and a notice is the investigator's own line (`ModelSession.send(tool_results, notice)`).
   - A call past the limit is answered "not run", never dropped, and the model is then asked for its answer. A turn is kept for the answer, and one more for a single correction.
   - **Asked for its answer, a model that calls a tool is asked once more,** in a turn that cannot call one (`send(..., final=True)`).
   - So an investigation that reaches a limit ends in an answer from the evidence it has. It ends `BUDGET_EXHAUSTED` only if the model, asked twice, still gives none, or if no turn at all can be afforded.
   - **The cost limit:** before each turn the session reports the most that turn could cost (`ModelSession.ceiling_usd`), and a turn that could take the investigation past the limit is not sent. When the money left would not surely cover a turn after the next one, the next one is the answer, and the notice says so. The Anthropic adapter bounds a turn by the last turn's input (cached or not) and output tokens, plus every byte of the new tool results (a token is at least one byte), plus a fixed allowance for request framing, all priced as cache writes, the dearest input, plus `max_tokens` of output. With fallbacks on, another model at another price may answer, so there is no bound. Then, and for models without one, cost is checked after every turn: the turn that crosses the limit is counted, its tool calls are not run, and an answer it gave is still judged.
4. **Answer.** The final answer must parse as `InvestigationOutput`: hypotheses with confidence, an optional suggested action, and a caveat. **Every** cited ID must be either the run's recorded evidence or a successful tool call of this investigation. One unknown citation rejects the whole answer. **A rejected answer is sent back once, with the reason:** the field that is too long or empty, or the citation that does not exist and the IDs that can be cited. The schema a provider enforces cannot carry a length or a count, so without this an answer one character too long was lost with the investigation. A second rejection is `INVALID_OUTPUT`, and nothing of the answer is shown. Control characters are removed from everything a model writes into the record.
5. **Failures.** A refusal, a provider error or a truncated answer is `NOT_AVAILABLE` or `INVALID_OUTPUT`. The deterministic report stands either way.

## The six tools

| Tool | Reads | Guard |
| --- | --- | --- |
| `get_briefing()` | What the run already recorded, in one call: each failed check's counts, mismatch examples (up to 20) and evidence queries; the other checks' outcomes; the changed files with line counts; the readable source files; both databases' application tables. Not the rule-based diagnosis, which a model's answer is compared with | Nothing the other tools could not return; names only under the readable roots; a name that could not be read (over 300 characters, or not on one line) is counted, not listed |
| `get_check_result(check_id)` | A recorded `CheckResult` | Only what deterministic code recorded |
| `get_evidence(evidence_id)` | A recorded evidence record | Only what deterministic code recorded |
| `get_schema(environment, table)` | Columns and constraints | Fixed catalog queries, table name bound as a parameter, read-only transaction |
| `read_source(path, start_line, end_line)` | The pinned head commit's staged archive, as `[line number, text]` in order, with the file's total line count | Normalized relative paths under the allowlisted application and migration roots only (never contracts, bindings, docs, benchmarks or test labels); at most 200 lines, and a line over 2,000 characters is cut. A directory, or a name that does not exist under a readable root, is answered with the names that are there; a path outside the roots gets the same refusal whether or not it exists. A name that occurs twice in the archive is read only as its last member, and only if that is a regular file |
| `run_readonly_query(environment, sql, max_rows)` | The reference or candidate database | See below |

`run_readonly_query` passes four layers:

1. PostgreSQL itself proves the query is one plain query with no hidden reads. This is `check_read_query`, the same check as bindings: no user-defined functions, no system catalogs, built-in functions from the allowlist only.
2. It runs as a prepared statement in a read-only transaction.
3. It runs as the read-only role, with a 5-second timeout.
4. Results are capped at 50 rows and 64 KiB. The 64 KiB limit holds for every tool's result, not only a query's.

A result can hold integers, numerics, text, booleans and timestamps with a time zone. A query that selects any other type (a date, a float, an interval, a timestamp without a zone) fails, and the failure says to cast it to text.

A refused query says what to do instead, in the query's terms: which functions are allowed, that catalogs cannot be read and `get_schema` can, that every output column needs its own name, which tables exist, and how to narrow a result with too many rows. The allowlist is the verifier's and is unchanged; only the wording is the investigator's. In the first live evaluation 28% of the calls the model read were refusals that named what was not allowed and never what was.

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
- **Tools:** the six tools are `strict` and `tool_choice` stays `auto`, because forced tool use is rejected on this model. Keywords strict schemas do not accept are removed from what is sent and still enforced client-side.
- **Answer:** the final answer is constrained to the `InvestigationOutput` JSON schema, then validated and citation-checked like any other.
- **Fallbacks:** server-side refusal fallbacks (`fallbacks: "default"`) are on by default; pass `fallbacks=False` to turn them off. The investigation records the model that actually served it.
- **Prompt caching:** a breakpoint on the system prompt, so that the tools and the prompt are read from the cache by the next investigation, and a top-level `cache_control` for the conversation (5-minute entries); pass `cache=False` to turn both off. Each turn re-sends the conversation so far, and the cached prefix is billed at the cache-read price.
- **Budget notices:** sent as mid-conversation system messages after the tool results, the operator's channel, which text inside a tool result cannot imitate. They only append to the conversation, so the cached prefix stays valid. A model that does not take such messages is not supported. A turn the loop marks final is sent with `tool_choice: none`.
- **Cost:** every turn records its tokens and cost at first-party list prices. For Claude Opus 5.5 that is, per million tokens: $4 input, $5 for a cache write (1.25 times input), $0.20 for a cache read and $20 output. A turn's input tokens count every prompt token, cached or not. The cost counts every attempt a fallback was billed for, the higher price of a long prompt where a model has one, and the tenth more that inference kept in the United States costs. A model with no recorded price is refused.
- **Retries:** a request is sent again only when the provider said it did not process it (a rate limit or an overload), twice at most. A timeout may have been billed, so it is not repeated; build the client with `max_retries=0` to hold the SDK to the same.

Nothing configures it automatically. Using it needs credentials (`ANTHROPIC_API_KEY` or an `ant auth login` profile), and, for this project, the owner's approval of the spend. Its tests (`tests/unit/test_anthropic_adapter.py`) replace the HTTP transport with a mock and make no network call.

## Change Analyst

The original design gives the AI two roles (ADR-0002, ARCHITECTURE "AI investigator"): an advisory Change Analyst before execution, and the Failure Investigator after checks. The Change Analyst is the same bounded loop with a different role, not a separate agent framework. It runs while the run is `ANALYZING`, after the change policy has passed and before any environment exists, and both of its arms are recorded with the `CHANGE_ANALYST` role:

- **`risk_rules(run_id, files, migration_roots)`, on every eligible run:** deterministic static risk rules (`RULES`, model `dataproof:risk-rules-v1`) over the changed Python files under the readable roots.
  - In a migration, only `upgrade()` is read; `downgrade()` undoes it.
  - Each rule matches a pattern known to go wrong in data migrations: a join or an aggregate in a backfill, rounding or fewer decimal places, a time-zone conversion, a filtered backfill, repeated inserts, a row-level patch by hard-coded key, a key column without a reference or uniqueness constraint, de-duplication, NULL and empty-string coercion, and dropped data.
  - In application code it also matches rounding calls (`round`, `quantize`, `floor`, `trunc`).
  - Each hypothesis cites the lines it matched (`source:<path>:L<start>-L<end>`, at the head commit), and the suggested action lists the rules' edge cases. No match is `NOT_AVAILABLE`, not a finding.
- **`change_analyst(model)`, only when a model is configured:** the investigation loop with its own prompt (`change-analyst/3`) and **two tools: `get_briefing`, which lists the changed files, and `read_source`**.
  - The task carries no file name: a pull request chooses its file names, so they reach the analyst in a tool result, as data.
  - There is no database to query yet, and any other tool call is `DENIED`. With nothing changed under the readable roots, the model is not called.
  - Every hypothesis must cite its own `read_source` evidence, so an analysis that cites anything else is `INVALID_OUTPUT` and is not shown.
  - Each hypothesis is one risk with where it is and the edge case that would show it. The suggested action lists edge cases worth a contract or a fixture row. HIGH and MEDIUM confidence mean a risk to hold the change for; LOW means "looks safe, but check".
  - Default budget: 8 tool calls, 10 turns.

**What it is worth, and what it is not:**

- **Rules:** the [change-analysis benchmark](../../benchmarks/README.md#change-analysis) anticipates 21 of 22 corpus faults, but also alarms on 6 of 12 clean controls. A rule cannot tell a correct ranged backfill from one that skips its boundary. The rules were written with the corpus in view, so that recall is an optimistic bound.
- **The model arm** is tested with scripted models. Its one live run (2026-11-19) sent no request, because the benchmark's cap for an analysis was below what one turn could cost, so whether it is more precise than the rules is still unknown ([results](../../docs/AI_EVALUATION_RESULTS.md)).
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
- **The briefing:** one call returns the failed checks, their examples, evidence and rule-based diagnosis, the changed migration with its line count, the readable files and the tables, and names nothing outside the readable roots.
- **Listings and refusals:** a directory or a wrong file name is answered with what is there; a refused query says what is allowed; a missing table, check or evidence ID is answered with the ones that exist.
- **The budget:** the model is told it; a call past the limit is answered "not run" and the answer is asked for; the last turn is kept for the answer; a model that then calls a tool again is stopped.
- **The first live run, replayed:** each of its 18 frozen investigations, turn for turn, is now answered in full and given a turn to answer in. This shows the loop's behaviour, not a model's.
- **Baseline:** it names F1, F3, the decimal fault and the timestamp fault.

`tests/unit/test_change_analyst.py` covers the Change Analyst, offline:

- **Rules:** each rule matches its pattern and not a plain copy. Two inserts into one table, or a self-copy, are repeated inserts. Only `upgrade()` is analyzed, rounding in changed application code is found, and matches cite their lines with valid evidence IDs. No match is not a finding.
- **Reads:** only changed files under the readable roots are read, never bindings or documentation.
- **The model arm:** it is offered `read_source` alone. Database queries, check results, approvals and reads of bindings are denied, and an analysis citing anything but its own reads is not shown.

`tests/integration/test_pipeline.py` (Docker) runs a scripted Change Analyst on F1 in a real run. The rules see F1's join and sum, the model reads the migration and is denied a database query, and the verdict stays `BLOCKED`.
