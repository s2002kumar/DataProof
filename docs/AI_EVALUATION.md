# AI evaluation protocol: Failure Investigator against the model-off baseline

**Status:** prepared and frozen. Every path has been dry-run with a scripted model at no cost. **No paid model has been called.** A live run waits for the owner to approve a budget of at most 10 USD (#7).

This protocol is written before any live result exists, so the result cannot shape the method. The harness is [`benchmarks/src/dataproof_benchmark/ablation.py`](../benchmarks/src/dataproof_benchmark/ablation.py), and its [README section](../benchmarks/README.md#ai-ablation-b3) describes the plumbing.

## Question

When a migration's data contracts fail, does the bounded Failure Investigator explain the root cause better than the deterministic, rule-based diagnosis every run already gets? And at what cost, latency and risk of unsupported claims?

Neither arm can change a check, a verdict or an approval. The question is only whether the advisory explanation is worth having.

## Arms

| Arm | What it is | Inputs |
| --- | --- | --- |
| Model-off | `dataproof_investigator.diagnose`: rules over each failed check's bounded mismatch examples | The run's check results and evidence records |
| Model | The Failure Investigator, exactly as a run's `INVESTIGATING` stage runs it | The same check results and evidence, plus the five read-only tools: the live reference and candidate databases through a role that cannot write, and the head commit's allowlisted source |

Both arms see the same failed checks of the same migrated databases, in the same process.

## Cases

- Every database-tier case of the [benchmark corpus](../benchmarks/README.md#corpus) whose data contracts fail or error: 18 of the 31 change sets. Clean controls and the two faults no declared contract can observe have nothing to explain, so they are skipped and listed.
- The corpus is generated from a frozen seed by the author of the system under test. It is **not independently authored**, which is a limit on every result here.
- Labels (fault family, affected events) live only in the evaluator manifest. The harness reads them only after every case has been investigated, and no file either arm can read names a fault.

## Frozen settings

| Setting | Value | Why |
| --- | --- | --- |
| Model | `claude-opus-5-5`, pinned, no server-side fallbacks | The investigator's default model. Without fallbacks every turn has a known price, so the per-case cap is a hard limit |
| Effort | `high` | The adapter's default |
| Output tokens a turn | at most 8,000 | Bounds each turn's cost |
| Prompt caching | automatic, 5-minute entries | A turn re-sends the conversation so far; caching bills the repeated prefix at the cache-read price |
| Per case | at most 0.50 USD, 12 tool calls, 16 turns | A turn is never sent if its worst case could pass 0.50 USD |
| Prompt and tools | `failure-investigator/1`, `tools/1` | Recorded with every investigation |
| Samples | one investigation a case | The budget allows one; see Limits |

## Budget and pilot

The owner approves a budget of at most 10 USD. The harness refuses `--budget-usd` above 10, needs `ANTHROPIC_API_KEY`, and starts a case only while the 0.50 USD cap still fits in what is left. The worst case for 18 cases is 9 USD.

1. **Pilot, at most 1 USD:** `case-104` (join multiplication, which the rules explain) and `case-118` (wrong column mapping, three failing checks, which the rules do not), with `--budget-usd 1`. The pilot checks the plumbing against the real API, prompt caching included (cache reads must be above zero from the second turn).
2. **Rule fixed in advance:** if a pilot investigation ends `BUDGET_EXHAUSTED` before answering, or is cut off at the token limit, the main run uses effort `medium`; nothing else changes. Otherwise the settings above stand.
3. **Main run, at most 9 USD:** all 18 cases with the settings the rule gives, `--budget-usd 9`. The pilot is reported separately and is not part of the main results.

**Commands,** run from `benchmarks/` with `ANTHROPIC_API_KEY` set and an admin DSN for a throwaway PostgreSQL:

```bash
# Pilot: at most 1 USD
uv run python -m dataproof_benchmark ablation --database <admin DSN> --live --budget-usd 1   --cases case-104 case-118 --out results/ablation-pilot-results.json
# Main run: at most 9 USD; add --effort medium only if the pilot rule calls for it
uv run python -m dataproof_benchmark ablation --database <admin DSN> --live --budget-usd 9
uv run python -m dataproof_benchmark ablation-report
```

**Expected cost, an estimate:**

- The prompt (system, tools and answer schema) is about 3.4 KB, and in the dry run a check result was 2 to 5 KB.
- So a typical investigation of six to eight turns should cost roughly 0.15 to 0.40 USD, mostly output (thinking) tokens, and the main run roughly 3 to 7 USD.
- Only the hard limits are guaranteed: 0.50 USD a case and the approved budget.

## Measures

Every measure is computed by code frozen in `ablation.py` before the live run.

| Measure | Definition |
| --- | --- |
| Completed | The investigation ended `COMPLETED`: a valid answer whose every citation names real evidence |
| Root cause (any) | The hypotheses or the suggested action use the fault family's rubric terms; every term group must match |
| Root cause (first) | The same rubric, on the first hypothesis alone, which is a stricter test of ranking |
| Other rubrics | How many other families' rubrics the texts also match. This is informational: high values suggest hedging across causes |
| Localization | How many of the labelled affected event IDs the hypotheses name |
| Citation validity | Of every evidence ID the model's final answer cites, how many exist (the run's evidence, or one of its own successful tool calls). It is counted from the raw answer, so it is measured even when the investigator rejects the answer |
| Unsupported specifics | Numbers with three or more digits, decimal amounts and `snake_case` identifiers in a hypothesis that appear in none of the evidence it cites. This is a mechanical proxy for unsupported claims: true but uncited details count, and wrong statements without specifics do not |
| Latency | Wall-clock time of each diagnosis, for both arms |
| Cost | Input, cache-write, cache-read and output tokens at the pinned prices, per investigation and in total |
| Tool calls | By outcome: allowed, denied (refused before running) and failed |

What is published:

- the summary, the per-case scores, and every hypothesis verbatim, in `results/ABLATION.md`;
- every investigation's transcript (each turn's text, tool calls, token counts and cost, with tool results truncated and hashed) in `results/ablation-transcripts/`.

## What would count

- **The model adds value if** it names the root cause in clearly more cases than the model-off arm (whose rules cover only some shapes of difference), with citation validity near 100% and few unsupported specifics, at a cost per failing run a team would pay.
- **It does not if** it matches the model-off arm, or wins only by hedging across many causes, or relies on unsupported specifics.

With 18 cases and one sample each, a difference of one or two cases is noise. The report says so rather than ranking on it.

## Review before the run

Reviewing the investigator, the Anthropic adapter and the harness for this evaluation found five problems. Each is fixed and tested:

- **No prompt caching.** Every turn paid full price for the whole conversation so far. Caching is now on.
- **Cache-blind accounting.** With caching, the API reports cached input separately. A cost or ceiling that counted only `input_tokens` would undercount, and the per-case cap would stop being a hard limit. Cost, context and ceilings now count every kind of input token. A test shows that a mostly cached context still stops a turn that could pass the cap.
- **Rejected answers were invisible.** An answer citing evidence that does not exist is rejected whole and not shown, as it should be. But that hid how often it happens. Citation validity is now measured on the raw answer.
- **No measure of unsupported claims.** Unsupported specifics is new, as defined above.
- **Model-off latency was recorded as zero, not measured,** and latency and cost had no spread. Both arms are now timed, with median, 90th percentile and maximum.

## Limits

- **Keyword rubric:** a negation ("not a time-zone problem") still matches. Every hypothesis is kept verbatim, so a reader can check each score.
- **One sample per case:** model outputs vary between runs, so the model arm is not reproducible exactly. The model-off arm is.
- **Author-generated corpus,** as above.
- **Unsupported specifics** is a proxy: see its definition.
- **Not measured:** whether a reviewer, given the explanation, finds and fixes the fault faster. That needs people.
