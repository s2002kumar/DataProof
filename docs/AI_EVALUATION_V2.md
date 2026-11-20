# AI evaluation v2: a proposed protocol

**Status: proposed on 2026-11-19, revised on 2026-11-20 after an independent review of the investigator and of this harness. Not approved, not run. No model has been called for it and no money has been spent.** It becomes a protocol when the owner approves it and its budget; then the commit that holds it, the investigator and the held-out set is recorded here, and nothing in it changes afterwards. The budgets below are the owner's proposed limits, not permission to spend.

It replaces nothing. The [first protocol](AI_EVALUATION.md) stays as it was frozen, with [what its one run showed](AI_EVALUATION_RESULTS.md). This one exists because that run measured the investigator's loop and not its diagnosis.

The harness is [`evaluation.py`](../benchmarks/src/dataproof_benchmark/evaluation.py), its criteria are [`verdicts.py`](../benchmarks/src/dataproof_benchmark/verdicts.py), its gates [`gates.py`](../benchmarks/src/dataproof_benchmark/gates.py) and its launcher [`launcher.py`](../benchmarks/src/dataproof_benchmark/launcher.py) with [`Run-AiEval2.ps1`](../benchmarks/launcher/Run-AiEval2.ps1). Every stage has been dry-run end to end through the launcher with scripted models, at no cost: 154 investigations and analyses, each of which read its briefing and the changed migration.

## Questions

1. **Does the Failure Investigator finish?** In the first run it answered in 1 of 18 investigations.
2. **When it answers, does it explain the failure better than the rule-based diagnosis,** on cases neither it nor its author's changes were fitted to?
3. **Does it state only what its evidence shows,** and what does an investigation cost and take?
4. **Does the Change Analyst point at real risks without alarming on correct changes?** Its model arm has never been called.

Neither role can change a check, a verdict or an approval, in this evaluation or anywhere. The deterministic verifier decides; the question is only whether the advisory text is worth having.

## What the review of this harness found

Before any spend, a reviewer tried to make the harness give wrong numbers. Every finding below is fixed and held by a test in `tests/unit/test_evaluation_v2.py` or `tests/unit/test_evaluation_v2_stages.py`.

| Found | Now |
| --- | --- |
| One fixed answer of 289 characters, naming no cause and reading nothing, met A1 to A4 on every held-out fault: it listed every cause and said "see lines 10-29" | The cause is the owner's reading, blind to the arm. The keyword proxy grades the first hypothesis only, matches the start of a word, and fails an answer that also matches two other cases' rubrics. A location must lie within the faulty statement's lines, give or take two, of the changed file, in an answer stating at most four ranges. That answer now fails both |
| A paid stage could exit 0 with no model output: no case matched, every migration failed, the budget skipped every run, half the runs unanswered | A stage exits 0 only when every investigation it was to run ran, sent its requests and was served by the pinned model. Otherwise 3 (not a measurement), 4 (its stop rule) or 5 (incomplete) |
| Two stages wrote the same results file; a rerun replaced results without a trace | Every stage has its own folder; results are written once and never over a file; a held-out stage cannot be started twice |
| Nothing was written until a stage ended: a crash after 21 paid investigations left nothing | Every turn is on disk as it happens; an interrupted stage is taken up from its record, and nothing it started is sent again |
| Results did not say which code ran, and on this machine the documented command ran another checkout's code | Each results file records the commit, whether the tree was modified, whether the investigator came from this checkout, the hash of the labels, the seed, the output limit, the Python and SDK versions. A live held-out stage refuses a modified tree or a commit not on the remote |
| No code computed A1 to A7 or the outcome, and the Change Analyst's report failed | `eval2-verdict` computes every criterion and the outcome; a criterion that was not measured is undecided, never met |
| The rules arm was scored by keywords its fixed sentences can never contain, and the briefing handed the model the rules' diagnosis | The briefing no longer carries it. The Change Analyst's primary measure is the location both arms can state |
| The development set is the first evaluation's corpus under new names: 22 of its 31 migrations identical but for the revision ID | Said so, below. It is for plumbing and development only |
| A `downgrade` string named its fault in three held-out cases | Every `downgrade` body is replaced; a test holds that no text outside a faulty statement passes its case's rubric |
| A budget running out removed the last cases' repeats | Repeats run in rounds: every case once, then every case again |
| Stage C could not be piloted or stopped | It has a pilot and a stop rule like the investigator's |

## Arms

| Arm | What it is | Where it runs |
| --- | --- | --- |
| Rules | `diagnose`, the rule-based diagnosis every failing run gets; for the Change Analyst, the static risk rules. Free and deterministic | Every case |
| Improved | The Failure Investigator after [ADR-0009](adr/0009-the-investigator-finishes.md) and its review: `failure-investigator/3`, `tools/3` | Development, then held-out |
| Old | The investigator package as it was in the first run (`failure-investigator/1`, `tools/1`), from a checkout of `fe75bce`, run by this same harness on the same cases | Held-out, once |
| Change Analyst | `change-analyst/3` | Development, then held-out |

- **Model and settings:** `claude-opus-5-5`, pinned, no fallbacks, effort medium, adaptive thinking, at most 8,000 output tokens a turn for an investigation and 4,000 for an analysis; prompt caching with a breakpoint on the system prompt and automatic caching for the conversation; the SDK's own retries off (the adapter resends only what the provider said it did not process). The same for the improved and the old arm.
- **The old arm** shows whether the changes matter on cases the old investigator has not seen either. A reviewer expects its result: its task names no file and it cannot list a directory, so it is unlikely to find the migration. **It is worth running only if that expectation is to be on the record; skipping it saves its 2.00 USD.** That is the owner's choice.
- **Optional arms,** each needing its own approval: the improved investigator on `claude-sonnet-5-5` or `claude-haiku-5-5`, and an independent open-weight baseline (see [Qwen](#an-independent-baseline-qwen)).

## Sets

| Set | What it is | Used for |
| --- | --- | --- |
| First evaluation's corpus | 31 change sets, seed `20261102` | **Retired.** Never run again |
| Development | The same generator with seed `20261119`: 31 change sets on the commerce schema, 20 faults in ten families and 11 clean controls; 18 have a failing check. **Not new cases:** 22 of the 31 migrations are a first-evaluation case but for the revision ID, and the rest differ only in literal values | Checking the plumbing against the real API, and any change to prompts or tools. **Its scores are never a headline,** and it cannot show overfitting: the held-out stage is the first contact with new cases |
| Held-out | The [subscriptions example](../examples/subscriptions/README.md): 14 change sets on another schema, 11 faults and 3 clean controls. 8 fault patterns come from publicly documented incidents, linked there | The result. **Run once, after the investigator is frozen. Never iterated on** |

What makes the held-out set held out, and what does not:

- **Another schema.** Invoices, subscriptions and plans, where the development set has payment events and a ledger. A test holds that no prompt and no tool description names anything of either schema.
- **Written for another purpose, before the first live run:** the Action's tests. No model has seen it, and ADR-0009 and its review were written from the commerce transcripts and code alone. One reviewer opened two held-out change files while sizing reads; the fix it found (casting a value type) came from the tools' code and applies to every set.
- **Blinded.** A model reads copies of the revisions without comments, docstrings, bare strings or `downgrade` bodies, under a neutral file name, and nothing else of the example: not its README, its change-set names or its bindings. Tests hold each of these.
- **Labels written before any run:** each fault's rubric and the text of its faulty statement are in [`benchmarks/heldout/labels.json`](../benchmarks/heldout/labels.json), dated 2026-11-19 and revised on 2026-11-20, still before any run: one generic term was removed after the review. A results file records the file's hash.
- **Not independent of the author.** The same person wrote the system, the cases and the rubrics. The incidents are other people's; the implementations are not.
- **Small and clustered.** Eleven faults from about seven migrations: three cases are one file with one statement changed, and two share a faulty statement. Repeats of a case are not independent either.

## Measures

Every measure is computed by code written before any live run.

| Measure | Definition |
| --- | --- |
| Answered | The investigation ended `COMPLETED`: a valid answer whose every citation exists. Anything else (stopped, invalid, refused, failed) is not answered, and is graded on nothing further |
| Cause named | **The owner's mark,** given blind to which arm wrote the answer (below), on whether the first hypothesis names the cause the reference gives. The rules' diagnosis of each case is marked the same way, by the same reader |
| Cause, keyword proxy | The case's rubric on the first hypothesis, every group of terms matching the start of a word, and not hedged (the hypothesis does not also match two other cases' rubrics). Reported beside the marks; it decides nothing. It misses a correct paraphrase and can pass a negation |
| Lines stated | The first hypothesis or the suggested action states a line range of the changed migration that lies within the faulty statement's lines, give or take two, in an answer that states at most four ranges. Mechanical, and not a keyword |
| Citation validity | Of every evidence ID the answer cites, how many exist, counted from the raw answer |
| Unsupported specifics | Numbers of three or more digits and `snake_case` names in a hypothesis that appear in none of the evidence it cites. A proxy: it cannot see a small invented number |
| By case | An investigation's case is answered, named or located if the majority of its repeats are |
| Cost, latency, turns, tool calls | Per investigation, from the API's token counts; a gate recomputes every turn's cost from its tokens and the price table |

For the Change Analyst, a risk counts when it is raised at MEDIUM or HIGH confidence. **Called:** it sent a request. **Answered.** **Flagged:** on a fault, a risk states lines within the faulty statement, the measure a rule can meet in its own words; by case, every repeat must flag it. **Anticipated:** the keyword proxy, reported beside it. **Alarmed:** on a clean control, any risk at all; by case, any repeat.

Rates are reported with 95% Wilson intervals, by investigation and by case. **The intervals over 33 investigations treat repeats as independent, which they are not;** the by-case counts over 11 are the honest denominator.

### The owner's reading

`eval2-sheet` writes every held-out answer of the improved and the old arm, and the rules' diagnosis of each case, as numbered items in a fixed shuffled order, each beside its case's reference (the fault, where its pattern comes from, and the faulty statement as the model's file has it). Which arm wrote an item is not shown; the key is written to another folder, to be opened only when the marks are in. The owner marks each item `y` or `n` on four questions in `marks.csv`: does it name the cause; does it point at the faulty statement; would it have told me what to fix; does it state only what its evidence shows. `eval2-verdict` then reads the marks with the key.

## Acceptance criteria, fixed before the run

On the held-out set, for the improved Failure Investigator: 11 cases, 3 investigations each, 33 in all. A criterion that was not measured as specified (a dry run, another set, other repeats, a stage that did not finish, marks not given) is undecided, never met. The rules name the cause in 3 of the 11 by the keyword proxy and state no line.

| | Criterion | Why this number |
| --- | --- | --- |
| A1, finishes | At least 30 of 33 investigations answered | At 30 of 33 the interval's lower end is 76%. Below that it is not a tool |
| A2, grounded | Unsupported specifics at most 5% of those stated, over all answers; undecided if none was stated | A rejected citation already counts against A1 |
| A3, adds to the rules | By the owner's marks, by case: the cause named in at least 7 of 11, at least 4 of them cases the rules miss, and at most 1 case where only the rules name it | The rules name 3. This is a practical bar, not a significance test: at the threshold an exact paired test of 4 against 0 gives p = 0.125 |
| A4, locates | By case, the faulty statement's lines stated in at least 6 of 11 | The rules never state a line. This is what a reviewer cannot get from them |
| A5, affordable | Median at most 0.15 USD and 60 seconds an investigation | About what the first run cost; a reviewer waits for it |
| A6, as configured | No plumbing fault (no request sent, a failed request, a turn served by another model, a tool that could not reach its database) | The first run's void stage |
| A7, the owner finds it actionable | Of the answers the owner marks as naming the cause, at least 4 in 5 marked actionable | Naming a cause is not enough to fix it |

What each outcome means for the product (`verdicts.verdict`):

- **All seven met:** **optional.** It may be described as an optional feature with a measured result on a small held-out set, quoted with its intervals. It stays off unless configured. Turning it on by default is not on the table: that would need cases from someone else's repository.
- **A1 and A2 met, any other not met:** **experimental.** It stays off, and this held-out set is spent; any later claim needs a new one.
- **A1 or A2 not met:** **disabled** in the documentation, and kept only behind configuration, with the result published.
- **A6 not met:** **void.** The stage is not a measurement, and the held-out set is spent with it.

For the Change Analyst, on the held-out set (14 change sets, 2 analyses each, 28), with the development set's 11 clean controls beside the held-out set's 3 (`verdicts.analysis_verdict`):

| | Criterion | Against |
| --- | --- | --- |
| C1, called | All 28 analyses sent a request | The first run: 0 of 34 |
| C2, finishes | At least 25 of 28 answered | |
| C3, flags the faulty statement | By case, at least 6 of 11 faults flagged, more than the rules | The rules flag 5 of 11 |
| C4, quiet on correct changes | Alarmed on at most 1 of the 3 held-out clean controls and at most 3 of the 11 development ones | The rules alarm on 2 of 3 and 6 of 11 |

All four met: optional. C1 not met: void. C2 not met: disabled. Otherwise experimental. These replace the first draft's keyword criterion ("at least 7 of 11 anticipated"), which the rules could not meet in their own words; nothing has run, so nothing was seen before the change.

**Negative results are published as they are,** with the transcripts, as the first run's were.

## Stages, budgets and the launcher

One stage for each run of the launcher. Nothing starts the next stage by itself.

| Stage | What runs | Pool, USD | Expected, USD |
| --- | --- | --- | --- |
| `DevPilot` | The improved investigator on `dev-101` (one failed check) and `dev-109` (three), stop rule 1 | development 2.50, at most 1.00 of it | 0.20 |
| `Dev` | The other 16 failing development cases, once | the rest of development | 1.60 |
| `Heldout` | The 11 held-out faults, three rounds, stop rule 4 | held-out 5.00 | 3.30 |
| `OldArm` | The old investigator on the 11, once (optional, above) | old arm 2.00 | 0.90 |
| `AnalystPilot` | The Change Analyst on `dev-101` (a fault) and `dev-102` (clean), stop rule 1 | analyst development 2.00, at most 1.00 of it | 0.08 |
| `AnalystDev` | The other 29 development changes, once | the rest of analyst development | 1.16 |
| `AnalystHeldout` | The 14 held-out changes, two rounds | analyst held-out 2.00 | 1.12 |
| **Total** | | **13.50** | **about 8.40** |

Expected costs assume about 0.10 USD an investigation (the first run's were 0.06 to 0.11) and 0.04 USD an analysis. They are estimates; the pools are hard limits.

- **Per investigation:** at most 1.00 USD, 12 tool calls and 16 turns (one kept in reserve for a correction); an analysis at most 0.40 USD, 8 tool calls and 10 turns. An investigation starts only while its stage has 0.60 USD left (an analysis 0.30), and is never given more than the stage has left: a stage cannot pass its pool. Below that amount the loop would ask for the answer after a turn or two, and the run would measure the budget.
- **What the loop does near its cost limit:** a turn is not sent if its worst case could pass the limit, and when the money left would not surely cover a turn after the next one, the next one is the answer. The worst case prices a turn's whole context as written to the cache and its output at the full limit: about 0.25 USD for an investigation's turn, whatever it really costs.
- **A pilot first, at the same commit.** No stage of a role starts until that role's pilot passed its gate at the commit being run: every investigation answered and ran, no request failed, the pinned model served every turn, the cache was read, and the spend recomputed from tokens equals the recorded spend.
- **A plumbing fault halts a stage at once** (a request that failed, a turn another model served, a run that sent nothing, a tool that could not reach its database). The rest of its budget would buy nothing that could be reported.
- **A held-out stage runs once, at a commit on the remote, from an unmodified tree, and only if its pool covers it** at the third quartile of what the development stage measured an investigation costs. If it does not, the launcher refuses and says what it would need; raising a pool is the owner's decision.
- **Records:** every stage's folder holds its start record, its journal (each turn as it happened), its results and transcripts, its gate and its seal: every file's SHA-256, chained to the stage before, made read-only. `Status` checks every seal and prints the spend by stage and pool. A stage whose process died is taken up again (`Resume`: only what it never started is sent) or closed as it stands (`Abandon`, charged what it recorded plus the most the request in flight could have cost). Until then nothing else starts.
- **Development may change the investigator,** on the development set's evidence alone. If it does, the investigator is committed again and both pilots run again at that commit before anything held-out.
- **Of the 15.10 USD the owner authorized on 2026-11-03, 1.651932 USD is spent.** That authorization was for the first protocol, which allows no rerun, so this budget is a new decision.

### The key, and the spend

[`Run-AiEval2.ps1`](../benchmarks/launcher/Run-AiEval2.ps1) shows the stage, its commit and its largest possible spend, and asks for `SPEND <amount>` to be typed exactly. Only then does it ask for the API key, with the input hidden. The key goes into the stage process's environment alone, never into the session, a file or a log; it is dropped from memory when the stage has started, and everything the stage printed or wrote is searched for it afterwards. Before any paid request the launcher asks the provider, at no cost, whether the key works and serves the pinned model.

```powershell
# What has been spent, and whether every record is as it was sealed:
powershell -NoProfile -ExecutionPolicy Bypass -File benchmarks\launcher\Run-AiEval2.ps1
# Every stage with scripted models, no key, no spend:
powershell -NoProfile -ExecutionPolicy Bypass -File benchmarks\launcher\Run-AiEval2.ps1 -Stage DryRunAll
# One paid stage, the first:
powershell -NoProfile -ExecutionPolicy Bypass -File benchmarks\launcher\Run-AiEval2.ps1 -Stage DevPilot
```

After the held-out stages:

```bash
uv run python -m dataproof_benchmark eval2-sheet --arm improved <held-out results> --arm old <old arm results> --out <sheet folder> --key <key file elsewhere>
uv run python -m dataproof_benchmark eval2-verdict --results <held-out results> --marks <sheet folder>/marks.csv --key <key file> --analysis <analyst held-out results> --analysis-dev <analyst development results> --out <report.md>
```

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
- **Known risks:** tool-call formats differ by runtime and break, and a JSON schema is not enforced together with tool calls in the runtimes checked, so the final answer needs a second, tool-free call. The earlier Qwen3 models of similar size score 41 to 43 overall on the Berkeley function-calling leaderboard (April 2026), where Claude Opus 4.5 scores 77. So it may simply not finish, and that would be a result too.
- **Recommendation:** decide after the held-out stage. If the improved investigator does not meet A1, a second model is not the next question.

## Limits

- **Small, clustered sets by one author.** An interval from 11 cases is wide, the cases are not independent of each other, and a held-out set by the system's author is weaker than someone else's cases.
- **The owner's reading is the system author's.** It is blind to the arm, not to the system. Nobody else has marked anything.
- **Keyword rubrics remain proxies,** and decide nothing here.
- **Blinding removes help a real pull request might carry.** A migration's comments can be useful or misleading. The evaluation asks what the investigator finds without them.
- **The old arm runs once** on each case, so its rates have wider intervals than the improved arm's.
- **Not measured:** whether a reviewer, given the explanation, finds and fixes the fault faster. That needs people who are not the author.

## Method sources

- **Repeats, and reading transcripts before trusting a score:** Anthropic, "Demystifying evals for AI agents" (2026), <https://www.anthropic.com/engineering/demystifying-evals-for-ai-agents>.
- **Held-out sets against overfitting to a development set:** Anthropic, "Writing effective tools for agents" (2025), <https://www.anthropic.com/engineering/writing-tools-for-agents>.
- **Comparing arms on the same cases, with intervals, and clustered questions:** Miller, "Adding Error Bars to Evals: A Statistical Approach to Language Model Evaluations" (2024), <https://arxiv.org/abs/2411.00640>.
- **Wilson intervals at small counts:** Brown, Cai and DasGupta, "Interval Estimation for a Binomial Proportion", Statistical Science 16(2), 2001.
- **Why the usual error bars mislead at small counts:** "Position: Don't Use the CLT in LLM Evals With Fewer Than a Few Hundred Datapoints" (2025), <https://arxiv.org/abs/2503.01747>.
