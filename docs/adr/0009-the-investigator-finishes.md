# ADR-0009: The investigator finishes: a briefing, a budget it is told, and refusals that say what is allowed

**Status:** Proposed (2026-11-19), revised on 2026-11-20 after an independent review ([what it found](#what-an-independent-review-found-before-any-live-run)); accepted when its pull request merges. **No live model has run with these changes.** What they are worth is a question for the next evaluation, which needs the owner's approval of a budget.

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
   - A turn is kept for the answer: the turn before it carries the request. One more is kept in reserve, for a single correction.
   - **The cost limit asks for the answer too.** A turn is not sent if its worst case could pass the limit. So when the money left would not surely cover a turn after the next one, the next one is the answer, and the notice says so. An answer that was paid for is judged even if its turn took the cost past the limit.
   - **Asked for its answer, a model that calls a tool is asked once more,** in a turn that cannot call one (`tool_choice: none`); its calls are answered "not run". An investigation ends `BUDGET_EXHAUSTED` only if it then still gives no answer, or if no turn at all can be afforded.
   - **An answer the checks reject is sent back once, with the reason:** which field is too long or empty, or which cited evidence does not exist and which IDs can be cited. Nothing is loosened: the answer that stands passed every check, and a second rejection is `INVALID_OUTPUT`.
2. **A notice travels on the operator's channel, never inside a tool result.** Tool results are the pull request's data, and the system prompt says to follow nothing in them. A budget notice is the investigator's own line, so the Anthropic adapter sends it as a mid-conversation system message after the tool results, which text inside a tool result cannot imitate. It only appends to the conversation, so the cached prefix stays valid. A model that does not take such messages is not supported: there is no second form that a tool result could imitate. When an answer is sent back there is no tool result to follow, so that notice is the user's turn, as the task is.
3. **A sixth tool, `get_briefing()`, returns what the run already recorded in one call:** each failed check with its counts, its mismatch examples (up to 20, as many as a check records) and the SQL of its evidence queries; the other checks' outcomes; the changed files with their line counts; the readable source files; and both databases' application tables. The system prompt and the task say to start there. It does not carry the rule-based diagnosis: a model's explanation is compared with that diagnosis, so it is not handed it.
4. **The Failure Investigator is given the changed paths,** as the Change Analyst already was. `Pipeline.investigate` computes them as `ANALYZING` does, from the merge base.
5. **A refusal says what to do instead.**
   - `read_source` of a directory, or of a name that does not exist under a readable root, answers with the names that are there. A source read also returns the file's total line count.
   - A refused query explains itself in the query's terms: which functions are allowed, that catalogs cannot be read and `get_schema` can, that every output column needs its own name, which tables exist, and how to narrow a result that has too many rows.
   - The tool descriptions state the same rules before the first call.
6. **The prompts are `failure-investigator/3` and `change-analyst/3`, and the tools `tools/3`** (the `/2` versions are what the review read; none of them ran against a model). The system prompt adds a short method (briefing, migration source, queries, answer), says who is speaking (the prompt, the task and operator messages; never a tool result), states the limits an answer is checked against, and says how to state uncertainty: the best-supported hypothesis at LOW confidence, with what could not be established in the caveat.
7. **The Change Analyst's task carries no file name.** A pull request chooses its file names, a line break and a sentence included, and the task listed them. The analyst now has `get_briefing` as well as `read_source`: the changed files reach it in a tool result, as data.

**What does not change:**

- **Every bound of ADR-0002.** The tools are read-only. Each call's arguments are validated before anything runs. The answer must match the schema, and every citation must name evidence that exists, or nothing of the answer is shown. The loop has no handle to a check, a verdict, an approval or the run.
- **The deterministic verifier,** which this change does not touch. The function allowlist that diagnostic queries share with bindings is the same; only the wording of a refusal is new, and it is written in the investigator's tool layer.
- **The limits themselves:** 12 tool calls and 16 turns. The run did not show the limits to be too low. It showed that reaching one was fatal, and that most of the budget went to re-reads and refusals. A budget now has a cost limit unless a caller removes it (1.00 USD), so that the loop is bounded as it is built.
- **Where pull-request content may appear:** only in tool results. The task carries only identifiers and counts written by deterministic code, the operator's own configuration, and the budget.

**What the briefing can read that was out of reach before, and why that is not a wider boundary:**

- **File names under the readable source roots.** Their contents were already readable by path, so their names disclose nothing new. Nothing outside the roots is ever named: a path outside them gets the same refusal whether or not it exists, and a test holds that.
- **Table names in the `public` schema of the two databases,** through a fixed catalog query run by trusted code. The model could already query any such table by name through the read-only role. It still cannot read a catalog itself.
- **Nothing else.** Check results and evidence were already returned by `get_check_result` and `get_evidence`.

**Alternatives considered:**

- **Put the recorded results in the task.** Rejected: mismatch examples and SQL are values and text a pull request controls, and they belong in a tool result, as data. [The provider's guidance](https://platform.claude.com/docs/en/test-and-evaluate/strengthen-guardrails/mitigate-jailbreaks) is the same: "Deliver third-party content to Claude inside `tool_result` blocks, never in `system` prompts or plain user `text` blocks." The briefing costs one call and one turn to keep that.
- **Seed the conversation with a fabricated briefing call.** Rejected: assistant turns are passed back to the API exactly as it returned them, thinking blocks included, and a turn the model never produced is not one of them.
- **Raise the limit.** Rejected as a fix: an investigation that is never asked for an answer is cut off at any limit, and costs more on the way.
- **Force every final answer with `tool_choice: none`.** Not as the first request: changing `tool_choice` mid-conversation invalidates the cached messages, which would roughly double what an investigation costs. It is the second request, for a model that ignored the first.
- **Drop the citations that do not exist and keep the rest of the answer.** Rejected: it would show an answer the model did not give. The answer is sent back instead, and must pass whole.
- **The API's task budgets.** They count tokens, with a minimum of 20,000, which is more than a whole investigation generates. The limit here is a count of tool calls, so the notice states it directly.
- **Turn parallel tool calls off.** Rejected: one call a turn doubles the turns and the latency. The batch that crosses the limit is handled by answering its calls.
- **Widen the function allowlist** (`array_agg` and the like). Not done: it is the verifier's safety check, the same queries can be written without those functions, and widening it is a security decision for the owner.
- **More agents, retrieval or a vector index.** Out of scope, and nothing in the run asks for them: an investigation's whole context was at most 12,734 tokens.

**What this does not show:**

- **That the model now diagnoses well.** The tests use scripted models. One of them replays each of the 18 frozen investigations turn for turn and shows that it is now answered in full, told when its budget is spent and given a turn to answer in. That is the loop's behaviour, not the model's.
- **That the briefing and the notices help a live model.** They follow the provider's guidance on compact context, actionable tool errors and reserving a turn for the final answer, and they remove the failures the run counted. Whether the answers are then right is unmeasured.
- **Anything about the first evaluation's cases.** Those have now been read closely. Running them again would measure a design that was changed with them in view, and would not be independent evidence. The next evaluation needs new cases.

## What an independent review found, before any live run

On 2026-11-19 four reviewers read this change with one question: what goes wrong against a real model that a scripted one cannot show? They ran the code against mocked transports and the frozen transcripts and called no model. What they found in the investigator, and what was done:

| Found | Done |
| --- | --- |
| `read_source` returned its lines keyed by number and sorted as text: a 13-line file arrived as 1, 10, 11, 12, 13, 2, 3. The same on `main`; no source read of the first run succeeded, so nothing had shown it | Lines are a list in order, `[number, text]` |
| An answer over a length or a count the model was never told was thrown away, with the investigation. The schema a provider enforces cannot carry those limits | The limits are in the prompt and in the schema's descriptions, and a rejected answer is sent back once with the reason |
| The cost limit could refuse the answer's own turn: the model had been told only of calls and turns | The cost limit asks for the answer while a turn for it can be afforded |
| Asked for its answer, a model that called one more tool lost everything | It is asked once more, in a turn that cannot call a tool |
| A database error inside a tool ended the investigation as a provider failure | It is a failed call, the model goes on, and the driver's message is not passed on |
| A query that selects a date, a float, an interval or a timestamp without a zone fails, and the refusal did not say what to do | The refusal and the tool's description say to cast it to text |
| The prompt asked for queries on what the mismatch examples already show; the briefing showed 8 of 20 examples | The prompt says not to, and the briefing shows them all |
| The briefing carried the rule-based diagnosis, which the model's answer is then compared with | Removed |
| A pull request's file names were written into the Change Analyst's task, line breaks included | The task carries no file name; they arrive in a tool result |
| The fallback form of a notice, text after the tool results, could be imitated by a tool result, and the prompt said notices never arrive that way | Removed; a model that takes no mid-conversation system message is not supported |
| A member name occurring twice in the archive was checked as one member and read as another: a link of the same name could be read through | The member that is read is the one that is checked |
| Only query results had a size limit; a pull request's file names and lines could fill the context | Every tool's result has the limit, long lines and values are cut, and names that could not be read are not listed |
| A NUL written by the model reaches PostgreSQL's `jsonb`, which rejects it | Control characters are removed from everything a model writes into a record; a failure's text is at most 500 characters |
| SDK retries can send a request twice after a timeout, and only one is recorded | The adapter sends again only what the provider said it did not process (429, 529); the paid harness turns the SDK's retries off |
| A declined fallback attempt, a long Haiku prompt, inference kept in the United States and an unpriced model were all costed too low | Each is priced; a model with no recorded price is refused |
| A stop reason the loop does not know raised after the turn was billed, losing its cost | It is a turn, with its cost |

A second review, of these fixes, found more; each is fixed and held by a test:

| Found | Done |
| --- | --- |
| Only a rate limit or an overload was sent again, twice; a server error or a connection that was never made ended the investigation, and a paid stage with it | Every error that cannot have been billed (429, 529, 5xx, no connection made) is sent again up to four times, waiting as long as the provider asks within a minute |
| An empty answer, which the provider documents, was kept as an empty assistant message; the correction that followed sent it back and the API refuses one | An empty turn is not kept, and the same request is sent once more as a turn of its own; a second empty answer is `INVALID_OUTPUT`. An empty text block is never sent back |
| Any 400 on a final turn was taken for a refused `tool_choice` | Only a 400 that names `tool_choice` is |
| A batch of large results could make the next turn too dear to send, and the investigation stopped unanswered with results it never saw | Results the cost limit cannot cover are withheld, their calls recorded as failed, and the answer is asked for in a turn that cannot call a tool |
| An unknown model was priced at the card with the dearest output, whose cache read is not the dearest | Every kind of token at its dearest price |
| A non-ASCII tool name passed the loop's identifier check and failed the record's, ending the investigation | The record's rule, in both |
| A control character in a citation was removed, so an ID that was not the one cited was accepted | Citations are not cleaned; such an ID does not exist |
| The correction repeated cited IDs, text a pull request could have put there, on the operator's channel | It gives their number and the IDs that can be cited |
| A call past the turns was said to be past the budget of calls; a briefing with long file names could still pass the size limit; the analyst's rules read a duplicate member differently from the tools | Each corrected |

With fallbacks on, a turn still has no cost bound: priced at the dearest card, even a first turn passes the default limit. The cost is then checked after each turn, as the investigator's README says. The paid evaluation runs with fallbacks off.

What the review confirmed and left as it was: the request shape is the documented one for every priced model; a mid-conversation system message after the tool results is the documented place for it and needs no beta; thinking blocks are passed back whole; every tool call is answered; the cost ceiling held as an upper bound on all 109 later turns of the first run; a path outside the readable roots gets the same refusal whether or not it exists; and nothing a model writes reaches a verdict, a check or GitHub.

Not done here, and why: the query tool reads a whole result into memory before its row limit applies, which is the verifier's code and predates this change; and a record that cannot be stored still fails its stage rather than being replaced by a `NOT_AVAILABLE` record. Both are noted in the pull request.

**Consequences:** the investigator's README, the threat model's tool list and the README describe six tools and the new loop. `tests/integration/test_investigator.py` covers the briefing, the listings, the refusals, the budget notices, the kept turns and the replay of the frozen investigations; `tests/unit/test_anthropic_adapter.py` covers the request, the notice's channel, retries and every price; `tests/unit/test_investigation_loop.py` holds one test for each finding above that concerns the loop or a tool. Both AI roles stay experimental and off unless configured.

**Sources.** The provider's documentation and engineering notes, read on 2026-11-19, that these decisions follow:

- **A turn kept for the answer:** the Agent SDK's loop guide ("Count each turn with tool calls, plus one for the final text-only response"), <https://code.claude.com/docs/en/agent-sdk/agent-loop>.
- **Notices on the operator's channel:** mid-conversation system messages ("in an agentic loop you can place the system message right after the tool results"), <https://platform.claude.com/docs/en/build-with-claude/mid-conversation-system-messages>; and "Don't put your own instructions in tool results", <https://platform.claude.com/docs/en/test-and-evaluate/strengthen-guardrails/mitigate-jailbreaks>.
- **Every call answered, also one that is not run:** "still return a `tool_result` for it with `is_error: true` and a brief explanation", <https://platform.claude.com/docs/en/agents-and-tools/tool-use/parallel-tool-use>.
- **Refusals that say what to try next:** "include what went wrong and what Claude should try next", <https://platform.claude.com/docs/en/agents-and-tools/tool-use/handle-tool-calls>; and <https://www.anthropic.com/engineering/writing-tools-for-agents>.
- **One tool for what is already known, and detailed descriptions:** <https://platform.claude.com/docs/en/agents-and-tools/tool-use/define-tools>; "retrieving some data up front for speed, and pursuing further autonomous exploration at its discretion", <https://www.anthropic.com/engineering/effective-context-engineering-for-ai-agents>.
- **Why not the API's task budgets:** they count tokens, with a minimum of 20,000, <https://platform.claude.com/docs/en/build-with-claude/task-budgets>.
- **Reading a uniform failure as the harness's:** "a 0% pass rate across many trials ... is most often a signal of a broken task, not an incapable agent", <https://www.anthropic.com/engineering/demystifying-evals-for-ai-agents>.
