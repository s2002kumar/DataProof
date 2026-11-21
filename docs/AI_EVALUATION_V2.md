# AI evaluation v2: a proposed protocol

**Status: proposed on 2026-11-19, revised on 2026-11-20 after two independent reviews of the investigator and of this harness, and on 2026-11-21 after its development stages ([what they showed](#what-the-development-stage-showed-and-what-changed-before-the-held-out-stage)). The owner has run the development stages, approving each one's spend: 1.0383958 USD in all. Nothing held-out has run.** The commit the held-out stage runs at holds this protocol, the investigator and the held-out set, and nothing in them changes afterwards. The budgets below are the owner's limits; each paid stage still needs its own typed approval.

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

A second review read the launcher, the gates and the graders as they stood after those fixes. Each finding below is fixed and held by a test:

| Found | Now |
| --- | --- |
| One transient provider error, or an error the provider never processed, ended its investigation as an outcome, and the held-out stage it was in was void | The adapter sends again every error that cannot have been billed. One that persists stops the stage with that investigation's record open; taking the stage up again starts it again and charges what it had spent. An error that is the investigation's own (a request the API refuses) is recorded as it is, and voids the stage |
| Taking a stage up again did not charge the request in flight when its process died | It is charged at the most it could have cost: the investigation's cap less what it had spent. A stage whose records already hold a fault cannot be taken up again |
| A dry-run pilot unlocked paid stages; a dry run could finish a paid stage; a dry run's reset removed a paid state | A state folder is paid or dry for good (`state.json`), a record counts only for its own kind, and only a dry state can be removed |
| Nothing checked that the code a stage ran was the checkout it recorded: this machine's environment loads another checkout's packages | A paid stage is refused if any of the project's packages would come from elsewhere, the PowerShell launcher refuses a launcher of another repository, and a gate fails a stage whose results name another commit or a modified tree |
| A gate's own cost recompute left out the price of inference kept in the United States, so every stage of such a workspace would fail its gate after spending | Each turn records where it ran, and the gate prices it the same way |
| Deleting the last sealed stage, or rewriting its seal, went unnoticed | Every seal is also recorded outside the state folder (`<state>.chain`), and `Status` and every stage check them against each other |
| "Every investigation ran" was counted against a number that shrank when a case's migration failed to apply | A pilot and a held-out stage are held to the number they are specified with, and a case that did not apply fails the gate |
| The old arm had never been piloted on its own code, and its checkout was made after the stage was recorded | It has a one-case pilot that checks the plumbing (the old investigator is expected not to answer), and its checkout is made and checked first |
| Most stages had no stop rule | Every stage has one |
| A location was any of four stated ranges, with two lines of margin: "see line 10 and line 26" located eleven faults of eleven, and two lines reached a neighbouring statement | Every range stated must lie within the statement's own lines, and A4 is the owner's mark |
| Outcomes did not follow the table: a criterion not met could read "undecided", and a stage that ran out part-way read "disabled" | The table's order is code, and a stage that did not finish is undecided |
| A2 counted the specifics of answers that were rejected and never shown | Answers only |
| A held-out document of another model, effort or prompt was graded as the result | It is no result |
| A spreadsheet's byte-order mark or semicolons broke the marks; "yes" counted as no | Both are read; y, yes, true and 1 are yes; anything unknown is refused |
| The sheet's order came from a public seed, and the rules' fixed wording gave their items away | A random seed kept only in the key; the rules' items are shown apart and said to be unblinded |
| Two launchers could run at once on one state | One at a time (`<state>/.lock`) |

A third check, of those fixes, found and closed these:

- **A request that had failed was charged as one in flight,** up to the cap, on every resume and again on abandoning: two resumes and an abandon took 3.00 USD from the 2.50 development pool. Now only a request that may really have been in flight is charged, once.
- **From the second resume on, the harness did not count earlier attempts' spend,** so a 5.00 USD stage could reach 5.30. Every earlier attempt is counted each time, and by the gate.
- **The launcher's clean-tree check and the results' provenance covered different paths,** so a held-out stage could pass the first, spend, and fail its gate on the second. One list now, and provenance is taken when the stage starts.
- **Every open investigation was started again on resume,** whatever stopped it: a draw repeated after an investigation that was stopped for what it was doing. Only a provider error starts one again; any other is counted as it stands.
- **An error raised here, not in transit, counted as the provider's.** Only a transport failure with no status does.
- **A line range was attributed only to a file named before it,** so correct answers failed and another file's line counted. A file named after it counts too, and a range that holds the whole statement is context.
- **Deleting a stage that was still open went unnoticed.** A stage's start is recorded outside its folder too.
- **The old arm's one-case pilot could not meet the cache rule,** and the old adapter does not price the US-only surcharge: the pilot is not held to it, and the surcharge is allowed for in what the old arm records.

## What the development stage showed, and what changed before the held-out stage

The owner ran `DevPilot` and `Dev` on 2026-11-21 at commit `9c26c89`: 0.1379682 and 0.9004276 USD, every gate passed, both records sealed. The 18 investigations were then read against the development labels and the migrations they diagnose. The held-out set and its results were not opened.

What the investigations did:

- **All 18 answered,** in 3 to 5 of their 16 turns, with 2 to 6 tool calls (60 in all; none failed or was refused). Each read the briefing, then the whole changed migration on its second turn. An investigation cost 0.0546 USD at the median and 0.0834 at most, and took 14.1 s at the median and 19.8 s at most. The pinned model served every turn, and no answer had to be sent back or asked for again.
- **Read against the labels and the source, every first hypothesis names the cause and the statement, and every suggested action would repair the fault.** In four cases the action also restores a constraint the fault had removed. These are the author's readings of development cases, not marks.
- **One explanation was partly wrong** (`dev-109`): it said every observed value was the expected one cut to one decimal place. That holds for single events, not for the per-order totals of several events in the same briefing. Three later cases with the same kind of aggregation (`dev-123`, `dev-124`, `dev-128`) explained it correctly.
- **No answer cited the briefing,** though 11 stated something only the briefing showed, usually that the other checks had passed. The measure flagged 8 of 232 specifics (3.4%). It cannot see a check written as "c2", so it found only some of them.
- **Two calls were redundant:** a table's definition, asked for after reading the migration that defines it (`dev-109`, `dev-124`).
- **The rules,** by the same reading, named the cause in 7 of 18, stated no line and suggested no fix. In `dev-111` they gave no answer: the check had errored, not failed.

What the mechanical proxies did. They decide nothing, and these are their errors against that reading:

| Proxy | Agreed with the reading |
| --- | --- |
| Cause, keyword, the investigator | 13 of 18. Two correct answers were counted as hedges (below), and three said it in other words: "the kind", "two identical statements", "a second time" |
| Cause, keyword, the rules | Credited in 9 cases, right in 7. In both swaps the rules' "ties" matched the transposition rubric's "tie", but the generator only ever swaps two values with an UPDATE |
| Lines | 4 of 18. The other 14 also named their context: the INSERT the fault came after, the table's definition, a line next to the statement |

What changed, each change held by a test in [`test_evaluation_v2_development.py`](../tests/unit/test_evaluation_v2_development.py):

1. **The prompt is `failure-investigator/4`:** one sentence, that `get_briefing` is a call with an evidence ID, to be cited for what only the briefing shows. Nothing else in the investigator changed. The commit changes, so the development pilot runs again at it before the held-out stage.
2. **A statement proxy beside the line proxy:** the first range an answer states reaches a statement at fault and no other statement. It agreed with the reading in 17 of 18 (`dev-111` named the correct backfill first). It replaces nothing: the line proxy is as it was, still grades the Change Analyst's flags (C3) as specified, and the owner's marks decide A4.
3. **A term of the case's own rubric is no sign of a hedge.** A lost and a duplicated row are "missing" and "duplicated", which are also a partial migration's and a retry's terms, so every correct answer to those cases counted as hedged. Of the 18 answers, only those two cases' grades change.
4. **Rules that gave no answer did not name the cause** in the by-case keyword comparison, as A3 already counts it from the marks. Before, such a case was left out.
5. **Records keep each tool result whole,** up to 64 KiB, the investigator's own limit on a result. At 16,000 characters three briefings were cut by about 200 bytes each, which only their hashes recorded.
6. **The development labels record the seed they were built with, 20261119.** They said 20261102, the first corpus's. Their `holdout` field is the first corpus's split, and nothing here reads it.

What did not change:

- **The rubrics' terms.** Adding the words the model used would fit the proxy to the model; removing "tie" would fit it to the rules' wording. The first evaluation's frozen scoring and the Change Analyst's use the same rubrics, the held-out set has its own, and the owner's marks decide for both arms.
- **The unsupported-specifics measure.** It flags a computed number ("+0.02") and cannot see "c2". It stays as specified, and the share it reports is a floor on what was left uncited, not a measure of it.
- **No acceptance criterion, threshold, budget, cap, held-out label or held-out reference.**

## Arms

| Arm | What it is | Where it runs |
| --- | --- | --- |
| Rules | `diagnose`, the rule-based diagnosis every failing run gets; for the Change Analyst, the static risk rules. Free and deterministic | Every case |
| Improved | The Failure Investigator after [ADR-0009](adr/0009-the-investigator-finishes.md) and its review, and the development stage: `failure-investigator/4`, `tools/3` | Development (`/3`), then held-out (`/4`) |
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
| Cause, keyword proxy | The case's rubric on the first hypothesis, every group of terms matching the start of a word, and not hedged (the hypothesis does not also match two other cases' rubrics on terms its own rubric lacks). Reported beside the marks; it decides nothing. It misses a correct paraphrase and can pass a negation |
| Statement located | **The owner's mark** on whether the answer points at the faulty statement |
| Lines, mechanical proxy | The first hypothesis or the suggested action states at least one line range of the changed migration, at most four, and every one lies within the faulty statement's own lines. Reported beside the marks; it decides nothing. It fails an answer that also names its context |
| Statement, mechanical proxy | The first line range the first hypothesis states of the changed migration (else the suggested action's first) reaches a simple statement at fault and no other statement of the file. A statement is at fault if it holds a line the fault changed (development) or a fault marker (held-out). Reported beside the marks; it decides nothing |
| Citation validity | Of every evidence ID the answer cites, how many exist, counted from the raw answer |
| Unsupported specifics | Numbers of three or more digits and `snake_case` names in a hypothesis that appear in none of the evidence it cites. A proxy, and a floor: it cannot see a small invented number or a check written as "c2", and it counts a number computed from cited ones |
| By case | An investigation's case is answered, named or located if the majority of its repeats are |
| Cost, latency, turns, tool calls | Per investigation, from the API's token counts; a gate recomputes every turn's cost from its tokens and the price table |

For the Change Analyst, a risk counts when it is raised at MEDIUM or HIGH confidence. **Called:** it sent a request. **Answered.** **Flagged:** on a fault, a risk states lines within the faulty statement, the measure a rule can meet in its own words; by case, every repeat must flag it. **Anticipated:** the keyword proxy, reported beside it. **Alarmed:** on a clean control, any risk at all; by case, any repeat.

Rates are reported with 95% Wilson intervals, by investigation and by case. **The intervals over 33 investigations treat repeats as independent, which they are not;** the by-case counts over 11 are the honest denominator.

### The owner's reading

`eval2-sheet` writes every held-out answer of the improved and the old arm as numbered items, shuffled together in an order drawn from a random seed that only the key holds, each beside its case's reference (the fault, where its pattern comes from, and the faulty statement as the model's file has it). Which arm wrote an item is not shown; the key is written to another folder, to be opened only when the marks are in. The rules' diagnosis of each case follows, in a section of its own: its fixed wording would give it away, so it is not blinded, and the sheet says so. **The held-out results, transcripts and reports are not to be opened before every mark is in:** they say which arm wrote what. The owner marks each item `y` or `n` on four questions in `marks.csv`: does it name the cause; does it point at the faulty statement; would it have told me what to fix; does it state only what its evidence shows. `eval2-verdict` then reads the marks with the key.

## Acceptance criteria, fixed before the run

On the held-out set, for the improved Failure Investigator: 11 cases, 3 investigations each, 33 in all. A criterion that was not measured as specified (a dry run, another set, other repeats, a stage that did not finish, marks not given) is undecided, never met. The rules name the cause in 3 of the 11 by the keyword proxy and state no line.

| | Criterion | Why this number |
| --- | --- | --- |
| A1, finishes | At least 30 of 33 investigations answered | At 30 of 33 the interval's lower end is 76%. Below that it is not a tool |
| A2, grounded | Unsupported specifics at most 5% of those stated, over all answers; undecided if none was stated | A rejected citation already counts against A1 |
| A3, adds to the rules | By the owner's marks, by case: the cause named in at least 7 of 11, at least 4 of them cases the rules miss, and at most 1 case where only the rules name it | The rules name 3 by the keyword proxy. This is a practical bar, not a significance test: at the threshold an exact paired test of 4 against 0 gives p = 0.125 |
| A4, locates | By the owner's marks, by case: the faulty statement pointed at in at least 6 of 11 | The rules never state a line. This is what a reviewer cannot get from them |
| A5, affordable | Median at most 0.15 USD and 60 seconds an investigation | About what the first run cost; a reviewer waits for it |
| A6, as configured | No plumbing fault (no request sent, a failed request, a turn served by another model, a tool that could not reach its database) | The first run's void stage |
| A7, the owner finds it actionable | Of the answers the owner marks as naming the cause, at least 4 in 5 marked actionable | Naming a cause is not enough to fix it |

What each outcome means for the product (`verdicts.verdict`):

The order is fixed (`verdicts.outcome`): a run that is not the held-out stage as specified (another model, effort, prompt or set of labels, a modified checkout, a dry run) has no outcome; then A6 not met is void; A1 or A2 not met is disabled; A1 or A2 undecided (the stage ran out part-way, not by its stop rule) is undecided; any other criterion not met is experimental; any other undecided waits for the marks; all met is optional.

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
| `Dev` | The other 16 failing development cases, once, stop rule 4 | the rest of development | 1.60 |
| `Heldout` | The 11 held-out faults, three rounds, stop rule 4 | held-out 5.00 | 3.30 |
| `OldArmPilot` | The old investigator on `dev-101`, to check its plumbing; it is not expected to answer | old arm 2.00, at most 0.70 of it | 0.08 |
| `OldArm` | The old investigator on the 11, once (optional, above) | the rest of the old arm | 0.90 |
| `AnalystPilot` | The Change Analyst on `dev-101` (a fault) and `dev-102` (clean), stop rule 1 | analyst development 2.00, at most 1.00 of it | 0.08 |
| `AnalystDev` | The other 29 development changes, once, stop rule 6 | the rest of analyst development | 1.16 |
| `AnalystHeldout` | The 14 held-out changes, two rounds, stop rule 4 | analyst held-out 2.00 | 1.12 |
| **Total** | | **13.50** | **about 8.50** |

Expected costs assume about 0.10 USD an investigation (the first run's were 0.06 to 0.11) and 0.04 USD an analysis. They are estimates; the pools are hard limits.

- **Per investigation:** at most 1.00 USD, 12 tool calls and 16 turns (one kept in reserve for a correction); an analysis at most 0.40 USD, 8 tool calls and 10 turns. An investigation starts only while its stage has 0.60 USD left (an analysis 0.30), and is never given more than the stage has left: a stage cannot pass its pool. Below that amount the loop would ask for the answer after a turn or two, and the run would measure the budget.
- **What the loop does near its cost limit:** a turn is not sent if its worst case could pass the limit, and when the money left would not surely cover a turn after the next one, the next one is the answer. The worst case prices a turn's whole context as written to the cache and its output at the full limit: about 0.25 USD for an investigation's turn, whatever it really costs.
- **A pilot first, at the same commit.** No stage of a role starts until that role's pilot passed its gate at the commit being run: every investigation answered and ran, no request failed, the pinned model served every turn, the cache was read, and the spend recomputed from tokens equals the recorded spend.
- **A plumbing fault halts a stage** when the investigation it happened in ends (a turn another model served, a run that sent nothing, a tool that could not reach its database, a request the API refused). The rest of its budget would buy nothing that could be reported, and the stage is void.
- **A provider error is not the investigation's outcome.** The adapter sends again, up to four times, every request that cannot have been billed (a rate limit, an overload, a server error, a connection never made). One that still fails stops the stage at once, with no results written and that investigation's record open. Taking the stage up again (`Resume`, with its own typed approval) starts that investigation again from the beginning and charges what it had already spent; what stopped it says nothing about the investigation. A request the API refused as invalid, or an error raised before a request left, is the investigation's own, and is recorded as it is. An investigation stopped any other way (the process died, or was stopped) is counted as it stands, unanswered, and never started again.
- **A held-out stage runs once, at a commit on the remote, from an unmodified tree, and only if its pool covers it** at the third quartile of what the development stage measured an investigation costs. If it does not, the launcher refuses and says what it would need; raising a pool is the owner's decision.
- **Records:** every stage's folder holds its start record, its journal (each turn as it happened), its results and transcripts, its gate and its seal: every file's SHA-256, chained to the stage before, made read-only, and the chain recorded outside the state folder too. `Status` checks every seal and prints the spend by stage and pool. A stage whose process died, or that a provider error stopped, is taken up again (`Resume`) or closed as it stands (`Abandon`); either is charged the most the request in flight could have cost. Until then nothing else starts. A state folder is paid or dry for good, and a dry run's records never stand for a paid one.
- **The code:** a paid stage is refused unless every package of the project it would load is this checkout's, at the commit it records, unmodified; and a held-out stage, unless that commit is on the remote.
- **The read-only seal stops accidents, not intent.** Anyone who can write the folder can delete it; the chain outside it shows that something was removed, not what. The verified total is the sum the gates recomputed from token counts; the bill is the Console's, which only the owner can read.
- **Development may change the investigator,** on the development set's evidence alone. If it does, the investigator is committed again and the development pilot runs again at that commit before anything held-out, as does the old arm's pilot if the old arm is to run. It did change once ([above](#what-the-development-stage-showed-and-what-changed-before-the-held-out-stage)).
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
- **Keyword rubrics remain proxies,** and decide nothing here. On the development stage they missed 5 of 18 correct answers before the hedge was corrected and 3 after, and credited the rules with 2 wrong explanations.
- **The development cases are easy to read:** in each the fault is one or two statements of a 42- to 55-line migration. They showed that the investigator finishes and cites; they say little about subtler faults, which is the held-out set's question.
- **Blinding removes help a real pull request might carry.** A migration's comments can be useful or misleading. The evaluation asks what the investigator finds without them.
- **The old arm runs once** on each case, so its rates have wider intervals than the improved arm's.
- **Not measured:** whether a reviewer, given the explanation, finds and fixes the fault faster. That needs people who are not the author.

## Method sources

- **Repeats, and reading transcripts before trusting a score:** Anthropic, "Demystifying evals for AI agents" (2026), <https://www.anthropic.com/engineering/demystifying-evals-for-ai-agents>.
- **Held-out sets against overfitting to a development set:** Anthropic, "Writing effective tools for agents" (2025), <https://www.anthropic.com/engineering/writing-tools-for-agents>.
- **Comparing arms on the same cases, with intervals, and clustered questions:** Miller, "Adding Error Bars to Evals: A Statistical Approach to Language Model Evaluations" (2024), <https://arxiv.org/abs/2411.00640>.
- **Wilson intervals at small counts:** Brown, Cai and DasGupta, "Interval Estimation for a Binomial Proportion", Statistical Science 16(2), 2001.
- **Why the usual error bars mislead at small counts:** "Position: Don't Use the CLT in LLM Evals With Fewer Than a Few Hundred Datapoints" (2025), <https://arxiv.org/abs/2503.01747>.
