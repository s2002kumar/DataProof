# DataProof benchmark

The fault benchmark from [BENCHMARK](../docs/BENCHMARK.md) (work package 10, #58): a seeded corpus of faults and clean controls, fair baselines, and a report that any rerun reproduces exactly. The latest results are in [`results/REPORT.md`](results/REPORT.md), which is generated from [`results/benchmark-results.json`](results/benchmark-results.json).

## Corpus

`python -m dataproof_benchmark corpus` writes [`corpus/`](corpus/) from a seed frozen before any evaluation (`dataproof_benchmark.corpus.CORPUS_SEED`). It is never tuned after the fact: a change to the generator shows up as a stale corpus.

- **31 change sets** over the commerce scenario, each an Alembic revision with the ledger bindings, as a pull request would carry them:
  - **20 faults:** two seeded variants for each of ten families. The families are join multiplication, transposition, balanced loss and duplication, lossy numeric conversion, timestamp shift, NULL and empty confusion, orphaned references, wrong column mapping, partial migration, and retry effects.
  - **11 clean controls:** restructures and declared-compatible normalizations.
- **Labels** live only in [`corpus/labels.json`](corpus/labels.json), the evaluator manifest. Each records a case's family, whether it is a fault, the contracts it should fail, and the event keys and columns it affects. They are computed from the published fixture, independently of the SQL.
- **Neutral names:**
  - case IDs (`case-NNN`) and change sets (`ledger-backfill-NNN`) are shuffled;
  - every revision has the same docstring;
  - a test checks that no corpus file a reviewer or investigator can read names a fault (#12).
- **Holdout:** the seed freezes a holdout set, and the report shows it apart from the development cases.
- **Provenance, stated plainly:** the corpus is generated from templates by the author of the system under test, so it is **not independently authored**.
- **An honest gap:** the two NULL and empty confusion faults change a memo that no declared contract reads. They stay in the denominators and are reported as misses.

## Method

`python -m dataproof_benchmark run --database <admin DSN>` runs every case at the **database tier**. Each case is migrated into a fresh PostgreSQL database loaded with the published fixture, as a run's candidate is, and compared with one reference.

| Baseline | What it checks |
| --- | --- |
| `B0` | The migration ran. Its API smoke checks need live previews, which this tier does not run. |
| `B1` | Reconciliation over the reviewed binding view: row counts, a hash of the key set, and refund and capture sums per order and currency. It is configured fairly, credited wherever it catches a fault (F1 included), and never marked not run to widen the difference (#12). |
| `B2` | DataProof's deterministic contracts C1 to C3, with the case's bindings approved. |
| `B3` | `B2` plus AI investigation on the same evidence (see [AI ablation](#ai-ablation-b3)). **Not run live:** model calls are a paid service whose budget has not been approved (#7). |

When C1 fails, the failure is also **reproduced** on a foreign-key-closure subset of the reference (#8). It is loaded into fresh base-schema databases, the case's migration runs on the candidate, and C1 is verified again under the pipeline's own rules (`dataproof_runner.signature`): `REPRODUCED` only if every seed key fails again on the same columns.

Labels are read only after every case has run. The results hold nothing that varies between runs, such as timings, so a rerun must reproduce them exactly. `tests/integration/test_benchmark_run.py` checks this in CI.

`python -m dataproof_benchmark report` writes the Markdown report from the results. It covers:

- detection per baseline and per family, with denominators;
- false positives on clean controls;
- agreement of the failing contracts with the labels;
- C1 localization: reported columns, the exact mismatch count, and the precision of the example keys (the verifier reports at most 20 examples);
- reproduction per family, with subset sizes and every case not reproduced or not attempted;
- development and holdout splits;
- every miss.

## Pipeline tier

`python -m dataproof_benchmark pipeline --database <admin DSN> --image <runtime image ID>` runs cases **end to end by the real worker on local Docker**. The results are written to [`results/pipeline-results.json`](results/pipeline-results.json), and `pipeline-report` writes [`results/PIPELINE.md`](results/PIPELINE.md).

- **What runs:** each case becomes a pull-request head in a throwaway repository, with the sample app and every protected oracle at its base. The real `Pipeline` then does everything from source staging to cleanup:
  - policy checks;
  - reference and candidate environments, and the migration;
  - previews of both commits, with each frontend built offline;
  - the data contracts, and the API behaviour contracts (C6) through the previews;
  - sealed evidence, the report, and cleanup.

  The case's bindings are approved in its trusted configuration, as a reviewer's approval would.
- **Cases:**
  - the first case of every data fault family and two clean controls from the database corpus;
  - the **API behaviour cases** in [`corpus/api/`](corpus/api/). Their migration is the direct copy; the fault is in the payments API the pull request serves: a refunded total over every entry, or transaction amounts rounded to one decimal. A refactored but correct API is the clean control. Only the live previews can show these faults.
- **Recorded:** the verdict and every check's outcome, seconds per stage from the run's transition log, each preview's build time, readiness, peak memory and CPU, and how many containers, networks or volumes cleanup left behind.
- **Scoring:** each label is held to the contracts it was written for. Database-tier labels cover C1 to C3; API behaviour labels cover every contract. A data fault that also fails an API contract (C6), because the API serves the migrated data, is listed as *also failed* and is not scored.
- **Results (one local run):** 11 of 12 faults were detected; the miss is the one fault the declared contracts cannot observe. No clean control was flagged (0 of 3), every failing set matched its label, and 7 of the 10 data faults also failed through the API previews. Every run left nothing behind. See [`results/PIPELINE.md`](results/PIPELINE.md) for timings.
- **Variation:** timings vary between machines and runs, but outcomes must not. `tests/integration/test_benchmark_pipeline.py` reruns the first API case in CI and requires the committed outcomes, a complete cleanup, and C6 failing while the data contracts pass.

## AI ablation (B3)

`python -m dataproof_benchmark ablation --database <admin DSN>` diagnoses every database-tier case whose contracts fail or error, twice, on the same evidence:

- **Model-off:** the rule-based diagnosis every run gets.
- **Model:** the bounded Failure Investigator, exactly as a run's `INVESTIGATING` stage runs it. It has the five read-only tools over the case's live databases (through a role that cannot write) and the head commit's allowlisted source.

**Live runs and cost:**

- **Guard:** a live run needs `--live --budget-usd <approved>` (at most 10 USD) and `ANTHROPIC_API_KEY`. Without them, a scripted model runs every path at no cost. That dry run is written under `.work/` and is never reported as a result.
- **Pinned model:** `claude-opus-5-5` at effort high, at most 8,000 output tokens a turn, with no fallbacks, so every turn has a known cost ceiling.
- **Per-case cap (0.50 USD, hard):** the investigator never sends a turn that could take the case past 0.50 USD. It also stops at 12 tool calls or 16 turns.
- **Budget gate:** a case starts only while the cap still fits in what is left of the budget. With 18 failing or errored cases, the worst case is 9 USD.
- **Accounting:** every investigation records input and output tokens, cost, tool calls by outcome, latency and the model that served it.

**Scoring** is frozen in `ablation.py` before any live run:

- the status (completed with every citation valid, budget exhausted, invalid output or unavailable);
- how many labelled affected event IDs the hypotheses name;
- whether the hypotheses or suggested action use the family's root-cause rubric terms.

The rubric is a keyword proxy, so every hypothesis is kept verbatim in the report for reading. `ablation-report` writes `results/ABLATION.md`.

## Findings worth knowing

- **Failures made of extra rows are not reproduced.** When a migration adds rows under new keys, as when refunds are inserted a second time, the failing keys exist only in the candidate. A subset seeded from the reference cannot contain them, so the replay passes and the result is honestly `NOT_REPRODUCED` (#8).
- **Duplicated keys are an error, not a failure.** When a migration duplicates a key, C1 reports `ERROR: DUPLICATE_KEY`. That still blocks the pull request, but a reproduction needs a failed check, so none is attempted.
- **Reconciliation misses aggregate-preserving faults.** `B1` misses faults that keep counts, keys and sums: transpositions, timestamp shifts, and times taken from the wrong column. This is the case for per-record contracts that BENCHMARK predicts.

## Not covered yet

- **A live `B3` run** (#7). The harness is built and tested with scripted models; the run waits for an approved model budget.
- **The Change Analyst** has no implementation, so it has no evaluation.

## Tests

- **`tests/unit/test_benchmark_corpus.py`:**
  - the committed corpus is what the frozen seed generates;
  - the corpus has at least 20 faults in at least 10 families, at least 10 clean controls, and holdout cases of both kinds;
  - no case name or corpus file carries a revealing word;
  - labels exist only in the evaluator manifest, outside every investigator source root;
  - the committed report is generated from the committed results.
- **`tests/integration/test_benchmark_run.py`** (PostgreSQL): a fresh run reproduces the committed results exactly.
- **`tests/integration/test_benchmark_pipeline.py`** (Docker): the first API behaviour case, run end to end, gives the committed outcomes and leaves nothing behind.
- **`tests/integration/test_benchmark_ablation.py`** (PostgreSQL, scripted models): both arms diagnose a failing case; clean cases are skipped. The per-case cap stops before a turn that could pass it, and a case does not start once the budget left cannot cover the cap. The investigator's role cannot write.
- **`tests/unit/test_ablation_scoring.py`:** the frozen rubric and event matching, the live-run guard (an approved budget of at most 10 USD and an API key), and the worst case fits the largest budget.
