# AI evaluation v2: a proposed protocol

**Status: proposed on 2026-11-19. Not approved, not frozen, not run. No model has been called for it and no money has been spent.** It becomes a protocol when the owner approves it and a budget; then the commit that holds it, the investigator and the held-out set is recorded here, and nothing in it changes afterwards.

It replaces nothing. The [first protocol](AI_EVALUATION.md) stays as it was frozen, with [what its one run showed](AI_EVALUATION_RESULTS.md). This one exists because that run measured the investigator's loop and not its diagnosis, and because its cases have since been read closely.

The harness is [`benchmarks/src/dataproof_benchmark/evaluation.py`](../benchmarks/src/dataproof_benchmark/evaluation.py). Every path below has been dry-run with scripted models, at no cost.

## Questions

1. **Does the Failure Investigator finish?** In the first run it answered in 1 of 18 investigations.
2. **When it answers, does it explain the failure better than the rule-based diagnosis,** on cases neither it nor its author's changes were fitted to?
3. **Does it state only what its evidence shows,** and what does an investigation cost and take?
4. **Does the Change Analyst name real risks without alarming on correct changes?** Its model arm has never been called.

Neither role can change a check, a verdict or an approval, in this evaluation or anywhere. The deterministic verifier decides; the question is only whether the advisory text is worth having.

## What is different from the first protocol, and why

| The first run showed | So this protocol |
| --- | --- |
| 17 of 18 investigations stopped by the loop, scored as the model's misses | Grades **answered** on its own. A cause or a location is graded only on an answer |
| A stage that called no model passed its gate and reported zeros | Refuses a cap no turn could meet before a run starts. A model arm that sent no request is reported as **not called**, never as a score, and the command exits non-zero |
| A dry run that could not see a price | Dry-run models report the least a live turn can cost, so the same cap applies to them |
| A pilot that did not answer, followed by the full run | **A pilot that does not answer stops the evaluation** |
| One investigation a case | Three on the held-out set, with Wilson intervals and a verdict by case |
| A keyword rubric alone | Adds a grader that is not a keyword: **the faulty statement's lines**. And the owner reads every answer |
| Cases written by the author with the system in view, since read closely | A development set of new instances, and a held-out set on another schema, mostly from documented incidents, blinded |

## Arms

| Arm | What it is | Where it runs |
| --- | --- | --- |
| Rules | `diagnose`, the rule-based diagnosis every failing run gets. Free and deterministic | Every case |
| Improved | The Failure Investigator after [ADR-0009](adr/0009-the-investigator-finishes.md): `failure-investigator/2`, `tools/2` | Development, then held-out |
| Old | The investigator package exactly as it was in the first run (`failure-investigator/1`, `tools/1`), from a checkout of commit `fe75bce`, run by this same harness on the same cases | Held-out, once |
| Change Analyst | `change-analyst/2` against the static risk rules | Held-out and development |

- **Model and settings:** `claude-opus-5-5`, pinned, no fallbacks, effort medium (the first main run's), at most 8,000 output tokens a turn, automatic prompt caching. The same for the improved and the old arm.
- **The old arm is there for a fair comparison,** on cases the old investigator has not seen either. The harness uses nothing of the investigator package that the old one lacks, so the only difference between the two arms is the package.
- **Optional arms,** each needing its own approval: the improved investigator on `claude-sonnet-5-5` and on `claude-haiku-5-5` (whether a cheaper model does as well), and an independent open-weight baseline (see [Qwen](#an-independent-baseline-qwen)).

## Sets

| Set | What it is | Used for |
| --- | --- | --- |
| First evaluation's corpus | 31 change sets, seed `20261102` | **Retired.** It is never run again: the investigator was changed with its transcripts in view |
| Development | The same generator with seed `20261119`: 31 new change sets on the commerce schema, 20 faults in ten families and 11 clean controls; 18 have a failing check | Checking the plumbing against the real API, and any change to prompts or tools. **Its scores are never a headline** |
| Held-out | The [subscriptions example](../examples/subscriptions/README.md): 14 change sets on another schema, 11 faults and 3 clean controls. 8 fault patterns come from publicly documented incidents, linked there | The result. **Run once, after the investigator is frozen. Never iterated on** |

What makes the held-out set held out, and what does not:

- **Another schema.** Invoices, subscriptions and plans, where the development set has payment events and a ledger. A test holds that no prompt and no tool description names anything of either schema.
- **Written for another purpose, before the first live run:** the Action's tests. No model has seen it, and ADR-0009 was written from the commerce transcripts alone.
- **Blinded.** Its revisions' comments and docstrings name their faults ("integer / integer is integer division"). A model reads copies without them, under a neutral file name, and nothing else of the example: not its README, its change set names or its bindings. Tests hold each of these.
- **Labels written before any run:** each fault's rubric and the text of its faulty statement are in [`benchmarks/heldout/labels.json`](../benchmarks/heldout/labels.json), dated 2026-11-19. A test holds that each rubric passes a true statement of its cause and fails an empty answer.
- **Not independent of the author.** The same person wrote the system, the cases and the rubrics. The incidents are other people's; the implementations are not. No third party has checked any of it.
- **Small.** Eleven faults. Every rate below carries its interval, and the intervals are wide.

## Measures

Every measure is computed by code in `evaluation.py`, written before any live run.

| Measure | Definition |
| --- | --- |
| Answered | The investigation ended `COMPLETED`: a valid answer whose every citation exists. Anything else (stopped, invalid, refused, failed) is not answered, and is graded on nothing further |
| Root cause named | The answer's hypotheses or suggested action match the case's rubric: every group of terms. On answers only. Reported of all investigations too, so that not answering cannot hide |
| Root cause first | The same on the first hypothesis alone |
| Lines stated | The answer states a line range of the changed migration that overlaps the statement at fault and spans fewer than 20 lines. Not a keyword: the label is the statement's text, and its lines are derived from the file the model read |
| Citation validity | Of every evidence ID the answer cites, how many exist, counted from the raw answer |
| Unsupported specifics | Numbers and identifiers in a hypothesis that appear in none of the evidence it cites. A proxy, as before |
| By case | A case's verdict is the majority of its repeats |
| Cost, latency, turns, tool calls | Recorded per investigation from the API's token counts, with tool calls by outcome |
| The owner's reading | Every answer is published verbatim. The owner marks each: would this have told me what to fix? The mechanical graders are proxies, and this is the check on them |

For the Change Analyst: **called** (it sent a turn), **answered**, **anticipated** (a hypothesis of MEDIUM or HIGH confidence matches the fault's rubric), **alarmed** (any such hypothesis on a clean control). The rules are graded by the same rubric on their own statements.

Rates are reported with 95% Wilson intervals. The improved arm is compared with the rules case by case, on the same cases.

## Acceptance criteria, fixed before the run

On the held-out set, for the improved Failure Investigator: 11 cases, 3 investigations each, 33 in all. The rules name the root cause in 3 of the 11, by the same rubric.

| | Criterion | Why this number |
| --- | --- | --- |
| A1, finishes | At least 30 of 33 investigations answered | At 30 of 33 the interval's lower end is 76%. Below that it is not a tool |
| A2, grounded | Unsupported specifics at most 5% of those stated, over all answers | A rejected citation already counts against A1 |
| A3, adds to the rules | By case, the root cause named in at least 7 of 11, at least 4 of them cases the rules miss, and at most 1 case where only the rules name it | The rules name 3. Fewer than 4 more is within what 11 cases can show by chance |
| A4, locates | By case, the faulty statement's lines stated in at least 6 of 11 | The rules never state a line. This is what a reviewer cannot get from them |
| A5, affordable | Median at most 0.15 USD and 60 seconds an investigation; none over its cap | About what the first run cost; a reviewer waits for it |
| A6, as configured | Every turn served by the pinned model; no stage with a model run that sent no request | The first run's void stage |
| A7, the owner agrees | Of the answers the graders pass on A3, the owner agrees with at least 4 in 5 | A keyword rubric matched a negation in the first run |

What each outcome means for the product:

- **All seven met:** the investigator may be described as an **optional** feature with a measured result on a small held-out set, quoted with its intervals. It stays off unless configured. Turning it on by default is not on the table: that would need cases from someone else's repository.
- **A1, A2, A5 and A6 met, A3 or A4 not:** **experimental.** It finishes and is grounded, and nothing shows it is worth its cost. It stays off. This held-out set is then spent; any later claim needs a new one.
- **A1 or A2 not met:** **disabled** in the documentation, and kept only behind configuration, with the result published.

For the Change Analyst, on the held-out set (14 change sets, 2 analyses each): called in 28 of 28; answered in at least 25; by case, at least 7 of 11 faults anticipated; and no more clean controls alarmed than the rules alarm. Three clean controls cannot support a claim about false alarms, so the development set's 11 are analyzed too, and the two are reported apart.

**Negative results are published as they are,** with the transcripts, as the first run's were.

## Stages, budget and stop rules

Each stage has a hard budget. The harness starts a case only while its per-case cap still fits in what is left, so a stage cannot spend more than its budget. Expected costs assume about 0.10 USD an investigation (the first run's were 0.06 to 0.11) and 0.04 USD an analysis.

| Stage | What runs | Hard budget, USD | Expected, USD |
| --- | --- | --- | --- |
| D, development | The improved arm on the 18 failing development cases, once. The first two are the pilot | 2.50 | 1.80 |
| H, held-out | The improved arm on the 11 held-out faults, three times each | 5.00 | 3.30 |
| O, old arm | The old investigator on the same 11, once | 2.00 | 0.90 |
| C, Change Analyst | Held-out, twice each (28 analyses), with 2.00 USD; development, once each (31), with 2.00 USD | 4.00 | 2.40 |
| **Core total** | | **13.50** | **8.40** |
| Optional: cheaper models | The improved arm on the held-out set with `claude-sonnet-5-5` and with `claude-haiku-5-5`, three times each | 4.00 | 1.80 |
| Optional: Qwen, local | An open-weight model on this machine | 0 | 0 |

- **A smaller first step is possible:** stages D and H alone, 7.50 USD at most, answer questions 1 to 3 for the improved arm without the comparison to the old one or the Change Analyst.
- **Per case:** at most 0.50 USD, 12 tool calls and 16 turns an investigation, as in the first run; at most 0.25 USD and 4,000 output tokens a turn for an analysis, which covers two worst-case turns (the first run's 0.15 USD covered none).
- **The pilot stops the evaluation** if either of its two investigations does not answer (`--only <two cases> --stop-after-unanswered 1`), if a request fails or none is sent, if a turn is served by another model, or if nothing is read from the cache from the second turn. The command checks each and exits non-zero. The first run's pilot did not answer, and the run went on by rule.
- **Stage H stops early** once four investigations have not answered (`--stop-after-unanswered 4`): A1 can then no longer be met, and the rest of the money would buy nothing.
- **Development may change the investigator,** on the development set's evidence alone. If it does, the investigator is frozen again at a new commit before stage H, and stage D's numbers stay development numbers.
- **Nothing is rerun.** A stage that has started is never started again.
- **Each row is one command with its own `--budget-usd`,** except stage C, which is two. The harness refuses a budget above 10 USD for one command.

Of the 15.10 USD the owner authorized on 2026-11-03, 1.651932 USD is spent. That authorization was for the first protocol, which allows no rerun, so this budget is a new decision.

## Commands

From `benchmarks/`, with an admin DSN for a throwaway PostgreSQL. Without `--live` each is a dry run with a scripted model, written under `.work/`:

```bash
uv run python -m dataproof_benchmark eval2-investigate --set dev --database <admin DSN>
uv run python -m dataproof_benchmark eval2-investigate --set heldout --database <admin DSN> --reps 3
uv run python -m dataproof_benchmark eval2-analyze --set heldout --reps 2
uv run python -m dataproof_benchmark eval2-report --results <results.json> --out <report.md>
```

A live stage adds `--live --budget-usd <the stage's budget>` and needs `ANTHROPIC_API_KEY`; results go to `benchmarks/results/ai-eval-v2/`.

The old arm is the same command with the investigator package of the first run in front of the current one:

```bash
git worktree add --detach <old checkout> fe75bce
PYTHONPATH=<old checkout>/packages/investigator/src \
  uv run python -m dataproof_benchmark eval2-investigate --set heldout --database <admin DSN>
```

Every results file records the prompt and tool versions that ran, so which investigator an arm used is on the record. A dry run of this on two held-out cases recorded `failure-investigator/1` and `tools/1`.

## An independent baseline: Qwen

A second model family would show whether a result is about this investigator's design or about one vendor's model. It is a research track, not a dependency: nothing here is built, and nothing would be deployed.

| | Local, the proposal | Hosted, the alternative |
| --- | --- | --- |
| Model | Qwen3.5-9B, open weights, Apache-2.0, 262,144-token context | Qwen3.6-35B-A3B, open weights, Apache-2.0 |
| How | `llama.cpp`'s server on this laptop (16 GB RAM, no GPU), 4-bit quantized (about 5.7 GB), thinking off | A third-party host of the open weights, through an OpenAI-compatible endpoint |
| Cost | 0 USD | About 0.20 USD for 30 investigations (an estimate, not a quote) |
| Time | Estimated 7 to 12 minutes an investigation on CPU: an overnight run for the held-out set once. Not measured | Seconds a turn |
| Data | Nothing leaves the machine | Prompts go to a third party. Only the synthetic fixtures would be sent, and the host's retention terms would be checked first |

- **What it needs:** an adapter for an OpenAI-compatible endpoint behind the existing `InvestigatorModel` interface, about 150 lines, with its own tests. That is a new model integration and needs approval.
- **Known risks:** tool-call formats differ by runtime and break (an open issue in Ollama for this model family, and one in `llama.cpp` closed only in November 2026), and a JSON schema is not enforced together with tool calls in the runtimes checked, so the final answer needs a second, tool-free call. The Berkeley function-calling leaderboard (April 2026) has no entry for this model; the earlier Qwen3 models of similar size score 41 to 43 overall there, where Claude Opus 4.5 scores 77. So it may simply not finish, and that would be a result too.
- **Recommendation:** decide after stage H. If the improved investigator does not meet A1, a second model is not the next question.

The sources for these figures, and what was verified on a fetched page against what is an estimate, are in the pull request that proposed this document.

## Limits

- **Small sets, one author.** As above. An interval from 11 cases is wide, and a held-out set by the system's author is weaker than someone else's cases.
- **Keyword rubrics** remain proxies. A1, A4 and the owner's reading do not depend on them.
- **Blinding removes help a real pull request might carry.** A migration's comments can be useful or misleading. The evaluation asks what the investigator finds without them.
- **The old arm runs once** on each case, so its rates have wider intervals than the improved arm's.
- **Not measured:** whether a reviewer, given the explanation, finds and fixes the fault faster. That needs people who are not the author.

## Method sources

- **Repeats, and reading transcripts before trusting a score:** Anthropic, "Demystifying evals for AI agents" (2026), <https://www.anthropic.com/engineering/demystifying-evals-for-ai-agents>.
- **Held-out sets against overfitting to a development set:** Anthropic, "Writing effective tools for agents" (2025), <https://www.anthropic.com/engineering/writing-tools-for-agents>.
- **Comparing arms on the same cases, with intervals:** Miller, "Adding Error Bars to Evals: A Statistical Approach to Language Model Evaluations" (2024), <https://arxiv.org/abs/2411.00640>.
- **Wilson intervals at small counts:** Brown, Cai and DasGupta, "Interval Estimation for a Binomial Proportion", Statistical Science 16(2), 2001.
- **Why the usual error bars mislead at small counts:** "Position: Don't Use the CLT in LLM Evals With Fewer Than a Few Hundred Datapoints" (2025), <https://arxiv.org/abs/2503.01747>.
