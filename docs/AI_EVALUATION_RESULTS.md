# AI evaluation v1: what the first live run showed

**Status:** the frozen protocol ([AI_EVALUATION.md](AI_EVALUATION.md)) ran once, on 2026-11-19, at commit `5254629`. Recorded spend: **1.651932 USD** of the 15.10 USD the owner authorized. Nothing was rerun, and nothing will be: the outputs are frozen as `ai-eval-v1`.

**The short version:**

- **The Failure Investigator answered in 1 of 18 investigations.** The other 17 were stopped by the loop's limit of 12 tool calls before they gave any answer. The rules named the root cause in 9 of the 18 cases; the model in 1, a case the rules missed.
- **Those 17 stops share one cause, and it is the loop, not the diagnosis.** The model was never told it had a limit and was never asked for an answer when it reached it. So this run does not measure how well the model diagnoses migration failures. It measures that the investigator, as built, almost never finishes.
- **The Change Analyst's model arm did not run at all.** Its cap for a whole analysis (0.15 USD) was below the most a single turn could cost (0.194055 USD), so no request was sent in any of the 34 cases. It spent nothing and measured nothing.
- **What the run does establish:** the bounds held. No turn was refused, cut off or failed; no cap was passed; the one answer cited only evidence that exists and stated nothing its evidence did not show.
- **Whether a live model improves on the rules is still not established.** As shipped, the investigator is not usable, and nothing here supports turning it on.

Every number below is recounted from the frozen files by code: [`AI_EVAL_AUDIT.md`](../benchmarks/results/AI_EVAL_AUDIT.md). Each investigation, turn by turn, is in [`AI_EVAL_TRACES.md`](../benchmarks/results/AI_EVAL_TRACES.md). The harness's own report is [`ABLATION.md`](../benchmarks/results/ABLATION.md).

## What ran

| Stage | Settings | Cases | Outcome | Recorded cost, USD |
| --- | --- | --- | --- | --- |
| Pilot | `claude-opus-5-5`, effort high, cap 1 USD | `case-104`, `case-118` | Both `BUDGET_EXHAUSTED` (tool calls). Its gate passed, and the pilot rule set the main run's effort to medium | 0.176161 |
| Main | effort medium, cap 9 USD, at most 0.50 USD, 12 tool calls and 16 turns a case | 18 failing cases (13 of the 31 change sets have no failing contract and were skipped, as the protocol says) | 1 `COMPLETED`, 17 `BUDGET_EXHAUSTED` | 1.475771 |
| Change Analyst | effort high (the adapter's default), cap 5.10 USD, at most 0.15 USD a case | 34 change sets | 34 `BUDGET_EXHAUSTED` before the first turn; 0 requests | 0 |
| **Total** | | | | **1.651932** |

- **All three stages finished** with exit code 0 and passed their gates. The stage times, commands and gate verdicts are in [`ai-eval-v1.manifest.json`](../benchmarks/results/ai-eval-v1.manifest.json).
- **The spend reconciles four ways** in every stage: the sum of each turn's recorded cost, the sum of each case's, the stage summary, and each turn recomputed from its token counts at the list prices. They agree to the last digit. These are the harness's records of what the API reported; the provider's usage page is the only independent record of what was billed.
- **The pinned model served every one of the 129 turns.** There were no refusals, no answers cut off at the token limit and no API errors.

## Scorecard

### 1. Completion and budget exhaustion

| | Pilot (effort high) | Main (effort medium) | Change Analyst |
| --- | --- | --- | --- |
| Investigations | 2 | 18 | 34 |
| Completed | 0 | 1 (6%) | 0 |
| `BUDGET_EXHAUSTED` | 2 | 17 (94%) | 34 |
| Stopped by the tool-call limit | 2 | 17 | 0 |
| Stopped by cost before the first turn | 0 | 0 | 34 |
| Stopped by the turn limit, a refusal, the token limit or an error | 0 | 0 | 0 |

No investigation came near its cost cap: the dearest cost 0.112 of the 0.50 USD allowed, and none used more than 8 of its 16 turns.

### 2. Root cause, and the first hypothesis

By the frozen keyword rubric, on the main run:

| | Rules | Model |
| --- | --- | --- |
| Root cause named, any text | 9 of 18 | 1 of 18 |
| Root cause in the first hypothesis | 9 of 18 | 1 of 18 |
| Of the investigations that answered | 9 of 17 | 1 of 1 |

The model's 17 misses are 17 investigations with no answer, not 17 wrong answers. One answer is not a rate.

### 3. Against the rules

| Who named the root cause | Cases | Which |
| --- | --- | --- |
| The model only | 1 | `case-121` |
| The rules only | 9 | `case-104`, `105`, `110`, `112`, `114`, `119`, `123`, `125`, `130` |
| Both | 0 | |
| Neither | 8 | `case-107`, `109`, `111`, `118`, `124`, `126`, `129`, `131` |

The rules also cost nothing and took at most 6 ms. The model arm took 13.6 to 23.1 seconds and 0.060 to 0.112 USD an investigation (mean 0.082) and, 17 times in 18, returned nothing.

### 4. Citations and unsupported claims

- **The one answer:** 14 of 14 citations exist, and 0 of its 16 specifics (numbers and identifiers) are missing from the evidence it cites.
- **The rules:** 79 of 79 citations exist, and 0 of 34 specifics are unsupported.
- **No answer was rejected** for a bad citation or a schema error, because only one answer was ever given. Citation and schema failure rates are therefore unmeasured, not zero.
- **"Also matched another family's rubric"** counts 1 for each arm. The model's is the negation the protocol warned of: its answer says "No rows were lost". The rules' is real: on `case-105` (a retry) their second statement blames a join.

### 5. Tool calls

Main run, 18 investigations:

| | Calls |
| --- | --- |
| Asked for | 240, in 118 turns |
| Run | 216 (exactly 12 in every investigation) |
| Read by the model | 205 |
| Run, but the investigation ended before the model read the result | 11 |
| Never run | 24 |
| Of those read: re-reads of what the run had already recorded (`get_check_result`, `get_evidence`) | 88 (43%) |
| Of those read: refused or failed | 58 (28%): 39 denied, 19 failed |
| Of those read: returned something new (a schema or a query result) | 59 (29%) |
| `read_source` calls answered, and how many returned source | 34, and 0 |
| Exact repeats of an earlier call | 0 |
| Refusals by the model, API errors, answers cut off | 0 |

Why the 58 calls were refused or failed:

| Cause | Calls | Investigations |
| --- | --- | --- |
| `read_source` of a directory, to find the migration: there is no way to list files | 18 | 17 |
| `read_source` of a guessed file name that does not exist | 16 | 12 |
| A query listing tables through the catalog | 8 | 6 |
| A query using a function outside the binding allowlist (`array_agg`, `to_jsonb`, `split_part`, `current_setting`) | 6 | 5 |
| A query with two output columns of the same name | 4 | 4 |
| A query of a guessed relation that does not exist | 3 | 3 |
| A query returning more rows than `max_rows`: failed instead of being cut short | 3 | 3 |

### 6. Latency and tokens

Main run, an investigation:

| | Minimum | Median | Maximum |
| --- | --- | --- | --- |
| Latency | 13.6 s | 18.6 s | 23.1 s |
| Cost, USD | 0.0600 | 0.0818 (mean 0.0820) | 0.1120 |
| Turns | 4 | 7 | 8 |
| Largest prompt, tokens | 6,051 | 8,077 | 12,734 |
| Output tokens, whole investigation | 1,416 | 1,786 | 2,261 |
| Output tokens in one turn, thinking included | 81 | 254 | 961 |

- **Tokens, all 18:** 717,899 input, of which 572,415 were cache reads, 145,212 cache writes and 272 uncached; 31,707 output. Prompt caching worked: 80% of input was read from the cache.
- **By case:** the table is in [section 5 of the audit](../benchmarks/results/AI_EVAL_AUDIT.md#5-each-investigation-use-and-how-it-ended).

### 7. The Change Analyst

- **Fault recall of the model arm: not measured.** The results file records 0 of 22 faults anticipated. That is the absence of an answer.
- **False alarms on clean controls: not measured.** The file records 0 of 12 alarmed, for the same reason. It must never be quoted as "no false alarms".
- **The rules arm,** which ran: 21 of 22 faults anticipated, 6 of 12 clean controls alarmed, 45 risks flagged, the same as the committed [`CHANGE_ANALYSIS.md`](../benchmarks/results/CHANGE_ANALYSIS.md).

### 8. Where the AI added value, where the rules were better, where neither sufficed

- **AI added value: one case.** On `case-121` (a column declared `NUMERIC(18,1)`, so every amount lost a decimal place) the rules said only "Values differ in ways these rules do not recognize". The model compared the two column types, counted the reference amounts with a second decimal digit (384 of 435), matched that to the check's 384 mismatches, ruled out lost rows, explained the two downstream failures and proposed the fix. It is the kind of answer the feature exists for. It also arrived by luck: it was the only investigation whose twelfth call happened to be its last.
- **Rules better: nine cases.** They recognize exact multiples, whole-hour shifts, values moved between keys and missing keys, instantly and for free. In all nine the model gave no answer.
- **Neither: eight cases.** The rules said "do not recognize" (or, on `case-107`, nothing), and the model was stopped. These are the cases a working investigator is for: one mislabelled event, one orphaned order, one value changed by 0.02.

## Why the investigations stopped

In order of how much each cost. None of these is a guess about the model: each is counted in the transcripts.

### 1. The loop ends an investigation at its thirteenth call, and never asks for an answer

- **What the code does:** `Investigator._run` raises on the call after the twelfth, and the investigation is recorded as `BUDGET_EXHAUSTED` with no hypotheses. Whatever the model had found is discarded.
- **The model is not told.** Neither the system prompt nor the task names a limit, and no tool result counts down. In 14 of the 17 stops the model asked for two or three calls in its last turn, as it had throughout.
- **Evidence:** all 19 stops (2 pilot, 17 main) carry the same failure, "more than 12 tool calls". None was near the cost or turn limit. See "How it ended" in the audit, and for example `case-104` turn 7 in the traces: at ten calls it asked for three schemas, and was stopped.
- **What it means:** the investigator has no way to finish except by chance. `case-121` finished because its twelfth call was a failed read, after which it chose to answer.

### 2. No investigation ever read the migration it was explaining

- **What happened:** 17 of the 18 investigations tried to read the migration source. All 34 of their `read_source` calls that were answered were refused or failed. The eighteenth (`case-119`) was stopped at turn 4, before it asked.
- **Why:** the task says only "Readable source roots: ...". It does not name the changed files, and no tool lists a directory. So the model asked for the directory (denied: "not a normalized relative path"), then guessed names such as `0002_ledger_entries.sql`, `002_ledger_entries.sql` and `README.md` (each "does not exist"). The real file is an Alembic revision under `sample-app/migrations/versions/`.
- **This is the product, not only the harness.** `Pipeline.investigate` builds the same tool context without the changed paths. The Change Analyst's task does list them; the Failure Investigator's does not.
- **What it cost:** 34 of the 205 calls read, and the one piece of evidence that turns "what changed in the data" into "which statement did it".

### 3. Nearly half the calls re-read what the run had already recorded

- **What happened:** the task gives check IDs and evidence IDs, and nothing of their content. Every investigation therefore began by fetching each failed check and its evidence one call at a time: 3 to 8 of its 12 calls, 88 of the 205 read (43%). In the pilot, at effort high, it was 14 of 21.
- **Why it is built so:** deliberately. Pull-request content reaches the model only inside tool results, as data, never in the task. That boundary is sound, and [the provider's guidance](https://platform.claude.com/docs/en/test-and-evaluate/strengthen-guardrails/mitigate-jailbreaks) asks for the same ("Deliver third-party content to Claude inside `tool_result` blocks"). The cost is a design that spends a quarter to two thirds of a twelve-call budget before the investigation starts.

### 4. Refusals that say what is not allowed, but not what is

- **Catalog queries (8 calls):** the model tried to list tables through `information_schema` or `pg_tables`, and was told "function not allowed in bindings: current_database()". Nothing tells it that `get_schema` is the only way to see a table, or which tables exist.
- **Functions (6 calls):** `array_agg`, `to_jsonb`, `split_part` and `current_setting` are outside the allowlist that diagnostic queries share with bindings. The refusal names the function and never the allowlist.
- **Duplicate column names (4 calls):** `SELECT e.order_id, o.*` is refused with "column \"order_id\" specified more than once", which reads like a SQL error and is really a property of how the query is checked.
- **Row limits (3 calls):** a query returning more rows than `max_rows` fails outright, so a call is spent learning the row count.
- **The allowlist itself is not at fault.** It is the verifier's safety check and stays as it is. The messages are the defect.

### 5. A batch that crosses the limit loses the calls inside it

When a turn asks for three calls with two left, the first two run, the third raises, and all three are lost with the investigation: 11 calls ran and were never read, and 24 never ran.

### 6. The pilot rule answered a different problem

The pilot ended `BUDGET_EXHAUSTED`, and the rule fixed in advance lowered effort to medium "if a pilot investigation ends `BUDGET_EXHAUSTED` before answering". The rule was written with cost in mind. The pilot's exhaustion was a count of tool calls, and lower effort did not keep investigations under it: at medium, 17 of 18 reached the same limit. The rule was followed exactly; it was the wrong rule for what the pilot showed.

### 7. The Change Analyst's cap was below the price of one turn

- **The arithmetic:** a turn is not sent if its worst case could pass the cap. The worst case prices 8,000 output tokens at 20 USD a million (0.16 USD) plus the prompt as a cache write (0.034 USD): 0.194055 USD, against a cap of 0.15 USD for the whole analysis.
- **Why nothing caught it:** the dry run uses a scripted model, which reports no cost ceiling, so the check was never exercised at real prices. The stage's gate checked spend, errors and the serving model, and passed a run of zero turns.
- **Whose error:** the harness's and its gate's, both written before the run. The guard did what it was built to do.

### What was not a cause

- **Not context growth or verbose tool output:** the largest prompt of any investigation was 12,734 tokens.
- **Not excessive reasoning:** a turn's output, thinking included, had a median of 254 tokens.
- **Not repeated calls:** none of the 240 repeats an earlier one.
- **Not cost:** no case passed a quarter of its cap.
- **Not citations, the answer schema, refusals or the provider:** none failed, though one answer cannot show they would not.

## Case by case

What the model's own queries had returned when each investigation ended. This column is a reading of the transcripts, not a score: it says what was retrieved, and claims nothing about what the model would have concluded. Turn numbers refer to the [traces](../benchmarks/results/AI_EVAL_TRACES.md).

| Case | Fault | The rules said | What the model's queries had returned | How it ended |
| --- | --- | --- | --- | --- |
| `case-104` | Join multiplication, all events | Exact multiples (2x, 3x, 4x): a join that repeats rows. **Named** | Ledger amounts exactly twice the legacy amount for the events sampled (turn 6) | Stopped at turn 7 asking for three schemas |
| `case-105` | Retry: refunds inserted again | Extra keys; also "a join". **Named,** with a wrong second statement | 160 extra refunds with event IDs above 1,000,000, each identical to an original (turns 3 and 4) | Stopped at turn 7 after two failed source reads |
| `case-107` | One event lost, one duplicated, equal amounts | Nothing (`NOT_AVAILABLE`) | 435 rows but 434 distinct events; event 1014 present twice (turns 3 and 4) | Stopped at turn 7 guessing file names |
| `case-109` | Time taken from the order, not the event | "Do not recognize" | All 435 ledger rows carry their order's `placed_at` as the event time (turn 5) | Stopped at turn 8 guessing file names |
| `case-110` | A batch boundary skipped | Missing keys: rows lost. **Named** | Order 205's two events are absent from the ledger (turn 3) | Stopped at turn 5 |
| `case-111` | One currency's amounts floored | "Do not recognize" | 384 reference amounts have hundredths; 185 candidate amounts do (turn 4) | Stopped at turn 5 asking for the split by currency |
| `case-112` | One currency's time zone | Whole-hour shifts (-2, -1). **Named** | Shifts of -2 h, -1 h and none, counted by kind (turn 6) | Stopped at turn 8 |
| `case-114` | Local time read as UTC | Whole-hour shifts (4, 5). **Named** | Five ledger instants equal the reference instants re-read as New York local time (turn 5) | Stopped at turn 6 checking all rows |
| `case-118` | One capture recorded as a refund | "Do not recognize" | Event 1116 is a capture in the reference and a refund in the ledger (turn 3) | Stopped at turn 7 guessing file names |
| `case-119` | Migration stops at a date | Missing keys: rows lost. **Named** | The 20 missing events' rows (turn 3); eight of its eleven calls were re-reads | Stopped at turn 4 |
| `case-121` | Amounts kept to one decimal place | "Do not recognize" | Column types `NUMERIC(18,1)` against `NUMERIC(18,2)`; 384 of 435 amounts affected | **Answered. Named,** with 14 valid citations |
| `case-123` | Join multiplication, refunds only | Exact multiples. **Named** | Per refund, how many events of its kind its order has (turn 4); three calls were refused attempts to list tables | Stopped at turn 7, on a fourth |
| `case-124` | One capture pointed at an order that does not exist | "Do not recognize" | Event 1395 is on order 282 in the reference and on order 1000282 in the ledger (turns 3 and 4) | Stopped at turn 8 guessing file names |
| `case-125` | Two captures' amounts exchanged | Values moved between keys. **Named** | Events 1296 and 1297 carry each other's amounts (turn 2) | Stopped at turn 7 after four failed source reads |
| `case-126` | One event lost, one duplicated, different amounts | "Do not recognize" | Event 1157 is missing and 1158 is present twice (turns 3 and 4) | Stopped at turn 5 |
| `case-129` | One refund pointed at an order that does not exist | "Do not recognize" | Event 1250's ledger row is on order 1000211, which has no order row (turn 2) | Stopped at turn 5 guessing file names |
| `case-130` | Two refunds' amounts exchanged | Values moved between keys. **Named** | Refunds 1111 and 1112 carry each other's amounts (turn 2) | Stopped at turn 8 guessing file names |
| `case-131` | A fix-up applied twice | "Do not recognize" | Event 1394 is 181.22 in the reference and 181.24 in the ledger (turn 3) | Stopped at turn 8 guessing file names |

By this reading, in 15 of the 17 stopped investigations the queries had already singled out the affected rows or the pattern, usually between the second and the fifth turn. The exceptions are `case-119`, which spent eight calls on re-reads and had one query answered, and `case-123`, which spent four on listing tables. In nine of the 17, the last turn was another attempt to find the migration file.

## What the evaluation itself got wrong

The protocol was frozen before the run so that the result could not shape the method. That held. These are faults in the method, found by the run:

- **A stop by the loop is scored as a miss by the model.** "Root cause 1 of 18" puts 17 investigations that were cut off in the same column as a wrong answer. They belong in a separate column.
- **"No answer" is recorded as "no alarm".** The Change Analyst's 12 clean controls read `alarmed: false` for a model that was never called.
- **The dry run could not see a price.** A scripted model has no cost ceiling, so neither the per-case cap nor the tool-call limit was ever tried against a model that behaves like one. One scripted investigation that made thirteen calls, and one with a real ceiling, would have shown both failures for nothing.
- **A gate passed a stage that did nothing.** It did not require a single turn.
- **One sample a case.** With 18 cases and one run each, a difference of several cases is noise. The protocol said so; the next one needs repeats.
- **A keyword rubric,** which matched a negation in the only answer it scored.
- **A corpus written by the author** of the system and of the rules, as the protocol also said.

## What can be concluded

- **Established:** as built, the Failure Investigator does not finish (1 of 18), for causes in its loop, its task and its tools that are counted above. The Change Analyst's evaluation harness could not run its model. The bounds held throughout, and an investigation costs about 0.08 USD and 19 seconds.
- **Not established, either way:** whether the model diagnoses these failures well, whether it beats the rules, how often it cites badly or states what its evidence does not show, and anything at all about the Change Analyst's model.
- **Suggested by one answer and seventeen transcripts, and no more than suggested:** that the model finds the affected rows quickly and can explain a fault the rules do not recognize. Seventeen stops with one cause are one finding, not seventeen.

## What follows

- **The product:** both AI roles stay off unless an operator configures them, and are experimental. The deterministic verifier and the rule-based diagnosis are unaffected by anything here. See [STATUS.md](STATUS.md).
- **The fixes,** in order of what each cost above: tell the model its budget and ask for an answer before it runs out; give the investigator the changed paths and a way to see what files exist; hand over the recorded results in one call; make refusals say what is allowed; and make a cap that cannot be met an error before a run starts, not a result after it. The investigator's part of this is [ADR-0009](adr/0009-the-investigator-finishes.md).
- **The next evaluation** needs new cases, since these have now been read closely: a development set and a held-out set, repeats, a column for investigations that were stopped, and acceptance criteria written before it runs. It needs the owner's approval of a new budget.

## Reproducing this report

Nothing here needs an API key, a database or Docker.

```bash
# The frozen outputs are what the run wrote: verify their SHA-256 digests
uv run python -m dataproof_benchmark ai-eval-audit --check
# Recount everything: writes results/AI_EVAL_AUDIT.md and results/AI_EVAL_TRACES.md
uv run python -m dataproof_benchmark ai-eval-audit
# The harness's own report: writes results/ABLATION.md
uv run python -m dataproof_benchmark ablation-report
```

- **The frozen files** are the three result files and the twenty transcripts under [`benchmarks/results/`](../benchmarks/results/), listed with their digests in `ai-eval-v1.manifest.json`. `tests/unit/test_ai_eval_baseline.py` fails if one changes, if a committed report is not what the code generates, or if the spend stops reconciling.
- **The transcripts are published as written.** A tool result is kept to 2,000 characters with the whole result's hash. They hold only the synthetic corpus's data and SQL; they were scanned for keys, local paths, host names and connection strings, and the test repeats the scan.
- **The model arm cannot be reproduced exactly:** a model's output varies from run to run, and this run is not to be repeated. The rules arm reproduces exactly.
- **One change to the harness since the run:** the report renderer wrote two blank lines after a skipped case, which the Markdown lint rejects. It now writes one. Every non-blank line of `ABLATION.md` is what the renderer at `5254629` writes.
