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
| `B3` | `B2` plus AI investigation on the same evidence. **Not run:** live model calls are a paid service that has not been approved (#7). |

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

## Findings worth knowing

- **Failures made of extra rows are not reproduced.** When a migration adds rows under new keys, as when refunds are inserted a second time, the failing keys exist only in the candidate. A subset seeded from the reference cannot contain them, so the replay passes and the result is honestly `NOT_REPRODUCED` (#8).
- **Duplicated keys are an error, not a failure.** When a migration duplicates a key, C1 reports `ERROR: DUPLICATE_KEY`. That still blocks the pull request, but a reproduction needs a failed check, so none is attempted.
- **Reconciliation misses aggregate-preserving faults.** `B1` misses faults that keep counts, keys and sums: transpositions, timestamp shifts, and times taken from the wrong column. This is the case for per-record contracts that BENCHMARK predicts.

## Not covered yet

- **The pipeline tier.** Full runs on local Docker with previews, API behaviour faults, stage timings and cleanup.
- **`B3`**, the AI ablation, and the Change Analyst evaluation (#7).

## Tests

- **`tests/unit/test_benchmark_corpus.py`:**
  - the committed corpus is what the frozen seed generates;
  - the corpus has at least 20 faults in at least 10 families, at least 10 clean controls, and holdout cases of both kinds;
  - no case name or corpus file carries a revealing word;
  - labels exist only in the evaluator manifest, outside every investigator source root;
  - the committed report is generated from the committed results.
- **`tests/integration/test_benchmark_run.py`** (PostgreSQL): a fresh run reproduces the committed results exactly.
