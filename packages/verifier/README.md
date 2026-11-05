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

**The type check (C8).** A data comparison proves only what the fixture exercises: a money column declared `numeric(12, 0)` passes a fixture of whole-unit amounts and rounds the first real refund. `type_check(contract, verification)` therefore judges what PostgreSQL declared the candidate's result columns to be, from the same query the data check ran: a `DECIMAL` column needs `numeric` with a declared scale at least the contract's (or an integer type when the scale is zero), an `INSTANT` column needs `timestamp with time zone`, and floating-point types never qualify. It is a check of its own, `types.<contract>`, required when the contract is, `FAIL` with one keyed example per unfit column (what the contract needs, what the candidate declared), and `NOT_RUN: PREREQUISITE_FAILED` when the candidate query never ran. A column read through an expression carries no declared scale, so only its type family is judged; that limit is stated in the example text. The GitHub Action ([docs/ACTION.md](../../docs/ACTION.md)) and the App's pipeline ([runner](../../services/runner/README.md)) both run it on every data contract.

`derive_verdict()` turns check results into a report verdict. A required `FAIL` or `ERROR` gives `BLOCKED`. A required `NOT_RUN`, or no required checks at all, gives `INCOMPLETE`. Optional failures give `CHECKED_WITH_WARNINGS`. Otherwise the verdict is `CHECKED`.

## Query safety

Each query runs alone in a `READ ONLY` transaction with a statement timeout and a row cap, sent as a **prepared statement**. That detail matters. Under PostgreSQL's simple query protocol, which psycopg uses for queries without parameters, a string such as `SELECT …; COMMIT; DELETE …` ends the read-only transaction and the `DELETE` runs; the extended protocol rejects multiple statements. A statement that ends the transaction on its own (`COMMIT`) or returns no rows is refused. Tests cover each of these cases against PostgreSQL.

Each query's evidence records the SHA-256 of its SQL and of its result. The result digest treats the rows as a multiset, hashed in sorted order of their typed encoding, because oracle and binding queries have no `ORDER BY` and PostgreSQL may return the same rows in a different order from one run to the next. The same inputs therefore always give the same evidence.

Execution is the second layer. The first is binding review, below. A least-privileged database role in real runs (THREAT_MODEL, boundaries 3 and 4) arrives with the runner; until then, the verifier is not exposed to untrusted repositories.

## Binding review (ADR-0005)

Before any approval, `analyze_binding()` wraps the binding in `CREATE TEMP VIEW` inside a transaction that is always rolled back, so nothing in it executes. PostgreSQL itself then decides whether it is one plain query and records what it uses:

- **Relations** come from `pg_depend`, followed through every view reached, so a new view wrapping `legacy_payment_events` does not hide it. Each relation is labelled `CREATED`, `ALTERED` or `UNCHANGED` by comparing the candidate catalog with the reference catalog, and `UNCHANGED` relations are **flagged** for the reviewer. Flags do not reject: joining an unaltered `orders` table is legitimate.
- **Functions**, and every relation in a range table, are also read from the analyzed rewrite trees of the binding and every view it reaches. `pg_depend` omits built-in (pinned) objects, so a system catalog read, or `query_to_xml('select * from legacy_payment_events', …)`, would be invisible there.
- **Rejected** as `ERROR: BINDING_UNSUPPORTED_CONSTRUCT`: anything that is not a single query; user-defined functions and operators, whose bodies hide reads; built-in functions outside `ALLOWED_FUNCTIONS` (for example `query_to_xml`, `pg_sleep` or `now()`), even inside a view the binding reads; system catalogs; and foreign tables.

The engine analyzes a binding before checking its approval, so a run awaiting review already reports the analysis, or the problem, to the reviewer. Nothing from the binding executes until it is approved.

## API behaviour checks (C6)

`verify_api_contract()` compares the reference application's API with the candidate's, under an oracle that names a reference `api_route`:

- **Key universe:** the oracle's `query_sql` runs on the **reference database only**, and each row fills the route's `{column}` placeholders, URL-encoded. The binding's `api_route` is called with the same rows, so the candidate cannot narrow what is compared by answering for fewer orders.
- **Rows:** each response (an object, or a list of objects) becomes rows that also carry the row's columns. Both sides are keyed by the oracle's key columns and compared with its typed comparators, exactly like data contracts. Money must be a decimal string or integer, never a JSON float, and instants must carry an offset.
- **Routes:** a route is a local path (`ApiRoute`): no scheme, host, `//` or `..`. Requests go only through an `ApiClient` bound to one preview, so a pull request's binding cannot send a request anywhere else.
- **Gating:** binding approval works exactly as for data contracts. An unapproved binding makes no request.
- **Errors:** failed requests, non-object bodies, duplicate keys, unknown placeholders and the request budget are typed errors (`API_REQUEST_FAILED`, `DUPLICATE_KEY`, `BINDING_UNSUPPORTED_CONSTRUCT`, `RESULT_LIMIT_EXCEEDED`).
- **Evidence:** each side's evidence records the route and the SHA-256 of the sorted response rows.

## Contract files

Oracles and bindings are TOML; see [`scenarios/commerce`](../../scenarios/commerce/README.md). `parse_oracle()` and `parse_binding()` take the text, for files read from a source archive. `load_oracle()` and `load_binding()` attach provenance from the caller, never from the file, and a binding file that carries oracle policy (for example a tolerance) fails validation. `assemble_contract()` marks a binding `APPROVED` only if its exact digest was approved.
