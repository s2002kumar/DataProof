# dataproof-verifier

Deterministic data-contract verification against PostgreSQL. For each `DataContract`, it runs the protected oracle on the reference database and the reviewed candidate binding on the candidate database, compares keyed rows with typed comparators, and returns a `CheckResult` plus the query evidence it cites. Only this code sets check outcomes; no model is involved (ADR-0002).

## Evaluation order

Every exit is a typed outcome, never an exception or a silent pass:

| Step | Condition | Outcome |
| --- | --- | --- |
| 1 | The binding has no approval bound to its exact digest | `NOT_RUN: AWAITING_BINDING_REVIEW` (or `BINDING_REJECTED`); no query runs |
| 2 | An oracle key or value column is not mapped by the binding | `ERROR: BINDING_COVERAGE_GAP` |
| 3 | The oracle's reference-first precondition returns rows | `ERROR: ORACLE_VIOLATED_ON_REFERENCE`; the candidate is not queried |
| 4 | A query fails, times out, exceeds the row cap, repeats a key or returns a float | `ERROR` with `QUERY_FAILED`, `RESULT_LIMIT_EXCEEDED`, `DUPLICATE_KEY` or `UNSUPPORTED_VALUE_TYPE` |
| 5 | Keyed comparison finds differences, including keys missing from or added to the candidate | `FAIL` with bounded examples and the full count |
| 6 | No differences | `PASS` |

Comparators: `EXACT` is typed equality, and NULL equals only NULL. `DECIMAL` compares exact numbers within the declared tolerance; floats are rejected. `INSTANT` compares timestamps as UTC instants. A binding cannot narrow the oracle's key universe: missing keys are mismatches.

`derive_verdict()` turns check results into a report verdict. A required `FAIL` or `ERROR` gives `BLOCKED`. A required `NOT_RUN`, or no required checks at all, gives `INCOMPLETE`. Optional failures give `CHECKED_WITH_WARNINGS`. Otherwise the verdict is `CHECKED`.

## Query safety

Each query runs alone in a `READ ONLY` transaction with a statement timeout and a row cap, sent as a **prepared statement**. That detail matters. Under PostgreSQL's simple query protocol, which psycopg uses for queries without parameters, a string such as `SELECT …; COMMIT; DELETE …` ends the read-only transaction and the `DELETE` runs; the extended protocol rejects multiple statements. A statement that ends the transaction on its own (`COMMIT`) or returns no rows is refused. Tests cover each of these cases against PostgreSQL.

These are layers, not the whole defense. Binding statement validation and relation review aids (ADR-0005) and a least-privileged database role in real runs are still to come, so until then, run only trusted bindings.

## Contract files

Oracles and bindings are TOML; see [`scenarios/commerce`](../../scenarios/commerce/README.md). `load_oracle()` and `load_binding()` attach provenance from the caller, never from the file, and a binding file that carries oracle policy (for example a tolerance) fails validation. `assemble_contract()` marks a binding `APPROVED` only if its exact digest was approved.
