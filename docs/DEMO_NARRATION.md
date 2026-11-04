# The demonstration, narrated

A script for the owner to narrate, about six minutes, in four acts: the original failure, the protected oracle, the approval mechanism, and the corrected result. Each act says what is on screen, what to say, and what not to claim. Two tracks carry the same story: the **GitHub App** (the recorded live run) and the **GitHub Action** (the public example repository, live pages). Use either, or both.

Everything shown is real: the pull requests, the check runs, the job summaries and the recording. Nothing is mocked, and the recording is not sped up. Where the recording predates a change, the script says so.

## Act 1: the failure (about 90 seconds)

**On screen, App track:** the demo pull request, [dataproof-demo-commerce#2](https://github.com/s2002kumar/dataproof-demo-commerce/pull/2), open on the migration file. **Action track:** [dataproof-example-subscriptions#2](https://github.com/s2002kumar/dataproof-example-subscriptions/pull/2), open on `migrations/versions/0002_money_columns.py`.

> This is a migration that moves legacy payment events into a per-order ledger. It joins the events to the order's items before summing, so an order with two items counts each event twice. *(Action track: this migration converts integer cents to money columns and divides integers, so 19.99 becomes 19.00.)*
>
> PostgreSQL runs it without an error. Every constraint holds, the row counts match, the application starts, and a conventional CI run is green. Refund 1002, a refund of 49.99 on a two-item order, is now recorded as 99.98. Nothing in the pipeline knew the business meaning of that number.
>
> That is the class of bug DataProof is for: migrations that succeed and corrupt data.

**Do not say:** that this is rare, or how often it happens. Say it is documented: the subscriptions example re-implements seven incidents from public bug reports ([examples/subscriptions](../examples/subscriptions/README.md)).

## Act 2: the protected oracle (about 90 seconds)

**On screen:** `scenarios/commerce/oracles/c1_event_preservation.toml` on the base branch (App track), or `dataproof/oracles/invoice_amounts.toml` on `main` of the example repository (Action track). Then the pull request's binding file.

> The expected values do not come from the pull request. They come from an *oracle* on the base branch: a read-only query over the schema before the migration, a key, and a comparator for each column. This one says: for every event ID, the order, the kind, the amount to the cent, and the exact instant must be preserved. *(Action track: for every invoice, subtotal, tax and total, to the cent.)*
>
> The pull request cannot edit that file; DataProof reads it from the base branch. What the pull request *can* do is propose a *binding*: a query that says how to read the same values from its new schema, with a mapping from the oracle's columns to its own. It cannot change the key universe, the comparator or the tolerance; those fields do not exist in a binding, and unknown fields are rejected.
>
> Before anything runs, DataProof shows a reviewer what the binding reads, resolved through views, and flags any relation the pull request did not create or alter, because a binding could read the surviving legacy table and pass every check while the new table is wrong.

**Do not say:** that bindings are verified automatically. The relation list informs a person; approval is the person's decision.

## Act 3: the approval (about 90 seconds)

**On screen, App track:** the recording at the first run ending `INCOMPLETE`, then the binding approval. **Action track:** the first run of [#1](https://github.com/s2002kumar/dataproof-example-subscriptions/pull/1) (`INCOMPLETE`, the annotation naming the digest), then [#3](https://github.com/s2002kumar/dataproof-example-subscriptions/pull/3), the one-line approval merged to `main`, then the re-run.

> The first run ends `INCOMPLETE`, and the check is not green: the contract that depends on the new binding did not run, and a run is never green by omission. The summary names what the reviewer does next.
>
> *(App track)* A maintainer approves the binding in the dashboard. The approval is recorded with the actor, the commit and the binding's digest, and it starts a new run; the original run and its evidence are kept. A pull request author may approve only when the repository has no other maintainer, and the record says that the separation was procedural.
>
> *(Action track)* A maintainer adds the binding's digest to `approved_bindings` in the configuration on `main`, through a reviewed pull request. The Action reads the base branch's tip at run time, so re-running the job sees the approval without a new commit on the pull request. A pull request cannot approve its own binding: an edit to that file in the pull request is ignored and listed.

**Do not say:** "approved automatically", "AI-approved", or that approval checks the migration. Approval says only that the binding is an acceptable way to read the new schema.

## Act 4: the verdicts (about 2 minutes)

**On screen, App track:** the recording's `BLOCKED` run (refund 1002, 49.99 against 99.98, both previews showing the order, the cited diagnosis), then the correction replay and the `CHECKED` run, then the two public check runs: [`BLOCKED`](https://github.com/s2002kumar/dataproof-demo-commerce/runs/126168145938) and [`CHECKED`](https://github.com/s2002kumar/dataproof-demo-commerce/runs/126170290869). **Action track:** the re-run of #2 (`BLOCKED`, 396 mismatches, invoice 5003: 19.99 → 19.00) and the re-run of #1 (`CHECKED`, eight required checks).

> With the binding approved, the run is `BLOCKED`. The summary leads with what it means and what to do, then the failing check with the exact keys and values: refund event 1002, expected 49.99, observed 99.98. The rule-based diagnosis says the changed values are exact multiples of the reference values, which is what a join that repeats rows before summing does. That diagnosis is advisory and cites its evidence; it did not decide anything.
>
> The corrected migration, a direct copy with one ledger row per event, is evaluated as a new run from the same fixture and the same contracts. It is `CHECKED`, and the summary lists exactly what was verified: every required check, with the key counts. That is what the check establishes, and no more; it is not a universal guarantee.
>
> Both verdicts are check runs on their commits, public, for anyone to open.

**Do not say:** that the AI investigator produced the diagnosis in the recording (it is the rule-based, model-off one), that a hosted service exists, or that anyone outside the project has run the Action.

## Closing (about 30 seconds)

> What DataProof claims is narrow and tested: specific checks, on an exact commit, fixture, contracts and engine version, with the expected values kept out of the pull request's hands. What it does not claim is written next to every number: the corpus is the author's, the AI roles are unmeasured until their budgeted run, the hosted deployment is not built, and nobody else has run it yet. The next step is someone else running it.

## If asked to go deeper

- **Isolation:** [ADR-0007](adr/0007-github-action-database-tier.md) and the hostile-migration test (`tests/integration/test_action.py`): environment, Docker socket, workspace, network, DNS, superuser, all denied; canary secrets reach nothing.
- **The finality rule:** [DEMO.md, check-run finality](DEMO.md#check-run-finality): a required check reads the check run created last, probed on real GitHub.
- **The benchmark:** [REPORT.md](../benchmarks/results/REPORT.md): 18 of 20 faults, 0 of 11 false positives, 13 of 20 for a fair counts-and-hashes baseline, two honest misses.
- **What is not built:** [STATUS.md](STATUS.md), "What is released, and what is not".
