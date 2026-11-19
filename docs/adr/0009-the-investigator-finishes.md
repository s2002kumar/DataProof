# ADR-0009: The investigator finishes: a briefing, a budget it is told, and refusals that say what is allowed

**Status:** Proposed (2026-11-19); accepted when its pull request merges. **No live model has run with these changes.** What they are worth is a question for the next evaluation, which needs the owner's approval of a budget.

**Context:** the first live evaluation ([results](../AI_EVALUATION_RESULTS.md)) ran the Failure Investigator on 18 failing cases. It answered in 1. The other 17 were stopped at their thirteenth tool call with no answer. The transcripts count the causes:

- **The loop discarded an investigation at its limit.** The model was never told the limit and never asked for an answer: 17 of 18 investigations, and both pilot investigations.
- **43% of the calls the model read (88 of 205) re-read what the run had already recorded,** one check or evidence record at a time, because the task gives only their IDs.
- **No investigation read the migration:** 0 of 34 source reads returned source. The task did not name the changed files and no tool lists a directory, so the model asked for the directory and then guessed file names.
- **28% of the calls read (58) were refused or failed,** with messages that named what was not allowed and never what was: catalog queries (8), functions outside the binding allowlist (6), duplicate column names (4), unknown relations (3), too many rows (3), and the 34 source reads.
- **A batch that crossed the limit lost the calls inside it:** 11 ran and were never read, and 24 never ran.

None of this is a fault in what the investigator is allowed to do. ADR-0002's boundary held throughout the run. It is a fault in whether an investigation, inside that boundary, can reach an answer.

**Decision:**

1. **The model is told its budget, and an investigation at its limit ends in an answer.**
   - The task ends with the budget: the tool calls and turns allowed, and that the last turn is for the answer.
   - After every turn the investigator sends a notice with the tool results: what is left, or that the budget is spent and the answer is due now.
   - A call past the limit is not dropped. It is answered "not run", so the model reads every result it is owed, and is then asked for its answer.
   - The last turn is kept for the answer: the turn before it carries the request.
   - An investigation still ends `BUDGET_EXHAUSTED` in exactly one new case: the model was asked for its answer and called a tool again. The cost cap is unchanged.
2. **A notice travels on the operator's channel, never inside a tool result.** Tool results are the pull request's data, and the system prompt says to follow nothing in them. A budget notice is the investigator's own line, so the Anthropic adapter sends it as a mid-conversation system message after the tool results, which text inside a tool result cannot imitate. It only appends to the conversation, so the cached prefix stays valid. A model that rejects such messages gets the notice as marked text after the results.
3. **A sixth tool, `get_briefing()`, returns what the run already recorded in one call:** each failed check with its counts, its first eight mismatch examples, the SQL of its evidence queries and the rule-based diagnosis of it; the other checks' outcomes; the changed files with their line counts; the readable source files; and both databases' application tables. The system prompt and the task say to start there.
4. **The Failure Investigator is given the changed paths,** as the Change Analyst already was. `Pipeline.investigate` computes them as `ANALYZING` does, from the merge base.
5. **A refusal says what to do instead.**
   - `read_source` of a directory, or of a name that does not exist under a readable root, answers with the names that are there. A source read also returns the file's total line count.
   - A refused query explains itself in the query's terms: which functions are allowed, that catalogs cannot be read and `get_schema` can, that every output column needs its own name, which tables exist, and how to narrow a result that has too many rows.
   - The tool descriptions state the same rules before the first call.
6. **The prompts are `failure-investigator/2` and `change-analyst/2`, and the tools `tools/2`.** The system prompt adds a short method (briefing, migration source, queries, answer) and says how to state uncertainty: the best-supported hypothesis at LOW confidence, with what could not be established in the caveat.

**What does not change:**

- **Every bound of ADR-0002.** The tools are read-only. Each call's arguments are validated before anything runs. The answer must match the schema, and every citation must name evidence that exists, or nothing of the answer is shown. The loop has no handle to a check, a verdict, an approval or the run.
- **The deterministic verifier,** which this change does not touch. The function allowlist that diagnostic queries share with bindings is the same; only the wording of a refusal is new, and it is written in the investigator's tool layer.
- **The limits themselves:** 12 tool calls, 16 turns and the cost cap. The run did not show the limits to be too low. It showed that reaching one was fatal, and that most of the budget went to re-reads and refusals.
- **Where pull-request content may appear:** only in tool results. The task still carries only identifiers and counts written by deterministic code, plus the budget.

**What the briefing can read that was out of reach before, and why that is not a wider boundary:**

- **File names under the readable source roots.** Their contents were already readable by path, so their names disclose nothing new. Nothing outside the roots is ever named: a path outside them gets the same refusal whether or not it exists, and a test holds that.
- **Table names in the `public` schema of the two databases,** through a fixed catalog query run by trusted code. The model could already query any such table by name through the read-only role. It still cannot read a catalog itself.
- **Nothing else.** Check results and evidence were already returned by `get_check_result` and `get_evidence`, and the rule-based diagnosis is in the report beside the investigation.

**Alternatives considered:**

- **Put the recorded results in the task.** Rejected: mismatch examples and SQL are values and text a pull request controls, and they belong in a tool result, as data. [The provider's guidance](https://platform.claude.com/docs/en/test-and-evaluate/strengthen-guardrails/mitigate-jailbreaks) is the same: "Deliver third-party content to Claude inside `tool_result` blocks, never in `system` prompts or plain user `text` blocks." The briefing costs one call and one turn to keep that.
- **Seed the conversation with a fabricated briefing call.** Rejected: assistant turns are passed back to the API exactly as it returned them, thinking blocks included, and a turn the model never produced is not one of them.
- **Raise the limit.** Rejected as a fix: an investigation that is never asked for an answer is cut off at any limit, and costs more on the way.
- **Force the final answer with `tool_choice: none`.** Not needed, and changing `tool_choice` mid-conversation invalidates the cached messages. A model that ignores the request is stopped instead.
- **The API's task budgets.** They count tokens, with a minimum of 20,000, which is more than a whole investigation generates. The limit here is a count of tool calls, so the notice states it directly.
- **Turn parallel tool calls off.** Rejected: one call a turn doubles the turns and the latency. The batch that crosses the limit is handled by answering its calls.
- **Widen the function allowlist** (`array_agg` and the like). Not done: it is the verifier's safety check, the same queries can be written without those functions, and widening it is a security decision for the owner.
- **More agents, retrieval or a vector index.** Out of scope, and nothing in the run asks for them: an investigation's whole context was at most 12,734 tokens.

**What this does not show:**

- **That the model now diagnoses well.** The tests use scripted models. One of them replays each of the 18 frozen investigations turn for turn and shows that it is now answered in full, told when its budget is spent and given a turn to answer in. That is the loop's behaviour, not the model's.
- **That the briefing and the notices help a live model.** They follow the provider's guidance on compact context, actionable tool errors and reserving a turn for the final answer, and they remove the failures the run counted. Whether the answers are then right is unmeasured.
- **Anything about the first evaluation's cases.** Those have now been read closely. Running them again would measure a design that was changed with them in view, and would not be independent evidence. The next evaluation needs new cases.

**Consequences:** the investigator's README, the threat model's tool list and the README describe six tools and the new loop. `tests/integration/test_investigator.py` covers the briefing, the listings, the refusals, the budget notices, the kept turn and the replay of the frozen investigations; `tests/unit/test_anthropic_adapter.py` covers the notice's channel and its fallback. Both AI roles stay experimental and off unless configured.
