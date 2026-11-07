# The DataProof GitHub Action

The Action verifies a PostgreSQL/Alembic migration on a pull request, inside the repository's own GitHub Actions, with nothing to host. It runs the base branch's migrations and a fixture into two disposable PostgreSQL databases, runs the pull request's migration in one of them with the pull request's code isolated in a container, compares the data against contracts the pull request cannot edit, and reports the verdict as the job's result with a summary, annotations and a JSON report. The design and its limits are [ADR-0007](adr/0007-github-action-database-tier.md).

**Status:** built and tested in this repository ([`tests/integration/test_action.py`](../tests/integration/test_action.py)); it runs on this repository's own pull requests ([`.github/workflows/dataproof.yml`](../.github/workflows/dataproof.yml)) and on the public example repository, [dataproof-example-subscriptions](https://github.com/s2002kumar/dataproof-example-subscriptions), where its [pull request #1](https://github.com/s2002kumar/dataproof-example-subscriptions/pull/1) went `INCOMPLETE` then `CHECKED` once the binding was approved on `main`, and [#2](https://github.com/s2002kumar/dataproof-example-subscriptions/pull/2) went `BLOCKED` with the exact amounts, 40 s from opening to verdict ([field results](ACTION_FIELD_RESULTS.md)). **There is no version tag yet.** A tag needs every row of the [release security review](ACTION_SECURITY_REVIEW.md) to hold on the release commit, a clean installation from the pinned commit (the example repository is one), and the owner's approval. Nobody but the author has run it, so no adoption is claimed ([#80](https://github.com/s2002kumar/DataProof/issues/80)).

It is the database tier of DataProof. API behaviour checks (C6) against live previews, previews of both commits, the AI Change Analyst and Failure Investigator, reproduction on a subset and interactive binding approval are capabilities of the [GitHub App](../README.md#try-it), not of the Action.

## Quickstart

The repository needs an Alembic project whose `env.py` takes the database URL from `DATABASE_URL`, a SQL fixture, at least one oracle, and the configuration below, all on the base branch. [`examples/subscriptions`](../examples/subscriptions/README.md) is a complete, independently designed example.

1. **Configuration,** `dataproof/config.toml` on the base branch:

   ```toml
   version = 1

   [alembic]
   config = "alembic.ini"          # env.py reads DATABASE_URL
   timeout_seconds = 300

   [fixture]
   sql = "dataproof/fixture.sql"   # loaded after the base branch's migrations

   [contracts]
   oracle_dir = "dataproof/oracles/"
   binding_dir = "dataproof/bindings/"
   approved_bindings = []
   ```

2. **An oracle,** `dataproof/oracles/<name>.toml`, on the base branch: a read-only query over the base schema whose key columns fix the universe of rows and whose expected columns fix the values and how they are compared ([Verification](VERIFICATION.md), [ADR-0005](adr/0005-oracle-binding-governance.md)). For a migration that changes the schema, the pull request adds a *binding*, `dataproof/bindings/<name>.toml`, that says how to read the same values from the new schema; it cannot change what is expected.

3. **The workflow,** `.github/workflows/dataproof.yml`:

   ```yaml
   name: dataproof
   on:
     pull_request:
   permissions:
     contents: read
   jobs:
     verify:
       runs-on: ubuntu-24.04
       timeout-minutes: 20
       steps:
         - uses: actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1 # v7.0.1
           with:
             persist-credentials: false
         - uses: s2002kumar/DataProof@a0789aa9529737153f2171f0ba0acaa545660006 # pin a commit; no tag yet
           id: dataproof
         - uses: actions/upload-artifact@<sha>
           if: always()
           with:
             name: dataproof-report
             path: dataproof-report
   ```

   `pull_request`, not `pull_request_target`: the Action refuses the latter. `persist-credentials: false` is right for a public repository; a private repository needs the token to fetch, and the migration container never sees the checkout either way. The commit above is the one the public example repository, [dataproof-example-subscriptions](https://github.com/s2002kumar/dataproof-example-subscriptions), pins; its pull requests are real runs of exactly this workflow.

4. **Merge the configuration, fixture, oracles and workflow to the base branch first.** The Action reads all four from the base branch, so the pull request that introduces them cannot be verified by them: its job ends `Not verified (ERROR)` saying the configuration does not exist on the base branch (exit 4), which is the design working, not a fault. From the next pull request on, every run is real.

5. **Open a pull request** that adds a migration. The job's summary shows the verdict and what to do next.

### When the pull request proposes a binding

The run is `INCOMPLETE` and the job fails until a maintainer approves the binding. The summary shows what the binding reads, flags any relation the pull request did not create or alter, and names the digest to add:

```toml
[contracts]
approved_bindings = ["<digest from the summary>"]
```

That is a reviewed change to the base branch. Re-run the job after it merges: the Action reads the base branch's tip at run time, so the approval is seen without a new commit on the pull request. A pull request cannot approve its own binding: an edit to `dataproof/config.toml` in the pull request is ignored and listed in the summary.

The digest covers the binding's text (contract, query, route, mapping), not the commit, so an approval survives later commits on the pull request as long as the binding is unchanged, and applies to any pull request that proposes the identical binding.

## What the job shows

- **The checks:** `migration.execute` (the pull request's migrations ran without error, within the timeout); `migration.rerun` (a second `alembic upgrade head` succeeded and changed nothing, so no work happens outside the revision mechanism: C7); one data check per oracle (the keyed comparison through the approved binding: C1–C3 style contracts); and one type check per oracle, `types.<contract>` (the candidate's result columns can hold what the contract compares, for example `numeric` with scale 2 for money and `timestamp with time zone` for an instant: C8). Every one is required when its contract is. What C4 to C8 mean here, and what they do not, is [ADR-0008](adr/0008-invariants-c4-to-c8.md).
- **The job's result:** success only for `CHECKED` and `CHECKED_WITH_WARNINGS`. `BLOCKED` exits 1, `INCOMPLETE` 2, `NOT_EXECUTED` 3, and a run that could not be made (no Docker, the base branch's own migrations fail, the fixture does not load, a malformed configuration) exits 4 and says why. Nothing is ever green by omission.
- **The summary:** the verdict and what it means; the next step; each failing check with bounded expected-versus-observed examples by key and column; the coverage table with a tally, naming unsupported checks; the evidence digests; the rule-based diagnosis; bindings awaiting review with their relations; trusted files the pull request changed; what is not in the Action; and the digests the run was pinned to (configuration, fixture, oracles, migration image, PostgreSQL image).
- **Annotations:** each failing required check on each migration file the pull request changed; a binding awaiting review on its file; a warning on each trusted file the pull request changed; a notice for a passing run.
- **The report directory:** `report.json` (everything above as data, plus every query's SQL and result digests and each step's redacted output) and `reference.dump` and `candidate.dump`, `pg_dump` archives of both databases taken by trusted code. Upload it as an artifact.
- **Outputs:** `verdict` and `report`.

## Configuration reference

All paths are relative to the repository root, without `..`. Unknown keys are rejected, so a misspelt key cannot relax a setting.

| Key | Meaning |
| --- | --- |
| `version` | `1`. |
| `alembic.config` | `alembic.ini`. Every migration step receives the disposable database's URL as `DATABASE_URL`. An `env.py` that reads its own settings instead declares them in `alembic.environment`. |
| `alembic.requirements` | Optional: a pinned requirements file for what `env.py` imports beyond Alembic, SQLAlchemy and psycopg. Read from the base branch and installed into the migration image before any pull-request code runs; a pull request cannot add a dependency to its own run. A package the file names replaces DataProof's own pin for it, so an application pinned to an older SQLAlchemy keeps it. |
| `alembic.environment` | Optional: variables for an `env.py` that builds its URL from its own settings, or refuses to start without them (`environment = { POSTGRES_SERVER = "{host}", POSTGRES_PASSWORD = "{password}", SECRET_KEY = "test-only" }`). A value is literal text, or names the disposable database: `{host}`, `{port}`, `{user}`, `{password}`, `{dbname}`, `{database_url}`. Both migration steps, the base branch's and the pull request's, receive them. Names DataProof sets itself (`DATABASE_URL`, `PATH`, `HOME`, `LANG`, Python's two settings, anything starting `DATAPROOF_`) are refused. **This file is public: every literal value here is a test value, never a real secret.** |
| `alembic.timeout_seconds` | How long one migration step may run before it is killed (default 600). |
| `fixture.sql` | A SQL file (plain statements; no `COPY FROM STDIN`) loaded into both databases after the base branch's migrations. Its SHA-256 is the run's dataset digest. Both databases must digest alike after loading, so the fixture must be deterministic. |
| `contracts.oracle_dir` | Protected oracles, read from the base branch only. At least one is required. |
| `contracts.binding_dir` | Bindings proposed by the pull request, read from its head commit. |
| `contracts.approved_bindings` | Content digests of approved bindings. |
| `contracts.rejected_bindings` | Content digests of rejected bindings (`NOT_RUN: BINDING_REJECTED`). |
| `policy.forks` | `run` (default) or `refuse`. |

The reference and candidate are provisioned identically: the base commit's migrations from the base commit's archive, then the fixture. The candidate then runs `alembic upgrade head` from the head commit's archive, so the pull request's new revisions apply on top; a pull request that edits an already-applied revision file changes nothing in the database, as in production.

## The isolation guarantee, and its limits

**What the pull request's code gets.** An Alembic migration is arbitrary Python, so it runs only in a step container of DataProof's local Docker backend, the same one the GitHub App uses ([ADR-0006](adr/0006-pluggable-execution-backends.md), [runner](../services/runner/README.md#tested-isolation)):

- an image built in the job from the base branch's inputs, whose trusted entrypoint verifies the source archive's SHA-256 as an unprivileged user before `alembic upgrade` ([ADR-0004](adr/0004-candidate-source-execution.md));
- a `git archive` of the head commit, copied in: no checkout, no `.git`, no credential;
- an internal Docker network whose only other member is its own PostgreSQL container: no route to the internet, to the runner, to the Docker daemon or to the other database; DNS does not resolve;
- an environment of `DATABASE_URL` for a disposable database with a role that cannot create roles or databases, plus `PATH` and Python settings, plus the variables `alembic.environment` declares on the base branch. The entrypoint passes nothing else through, and checks those names again;
- every capability dropped, `no-new-privileges`, a non-root user, memory, CPU and process limits, a tmpfs workspace, and a timeout after which it is killed.

**What holds it.** `tests/integration/test_action.py` runs a hostile migration that enumerates its environment, looks for the Docker socket, the runner's workspace and a `.git` directory, tries HTTPS, a raw IP, the Docker host gateway and DNS, and tries `CREATE ROLE ... SUPERUSER`; every probe is denied, and the job's canary secrets appear in no migration, report or summary. Other tests show that a pull request cannot approve its own binding or weaken an oracle or the fixture (all read from the base branch and listed as ignored), that a dropped table, a no-op migration behind an approved binding and a hang are all `BLOCKED`, that `pull_request_target` is refused before Docker starts, and that nothing is left behind.

**What it does not cover.**

- **The workflow file.** A `pull_request` workflow runs the workflow from the pull request's merge, so a pull request can edit or remove the job that calls the Action. Make the job a required status check (a removed job then blocks the merge) and protect `.github/workflows/` and `dataproof/` with code-owner review (a rewritten job then needs an owner's approval), or require the workflow through a repository ruleset. The Action cannot enforce this.
- **The kernel.** Containers share the runner's kernel. On GitHub-hosted runners, which are single-use virtual machines, an escape yields that machine and a read-only token. On a self-hosted runner it yields the runner: do not run untrusted pull requests on a persistent one.
- **Resources.** A hostile migration can spend the step's CPU, memory and time until the timeout; it cannot exceed them.
- **The trusted supply chain.** The migration image installs the base branch's pinned requirements with network access at build time, before any pull-request code is in the job. What the base branch trusts, the Action trusts.
- **What a verdict means.** A run establishes that specific checks passed for an exact commit, fixture, contracts and PostgreSQL version. It proves nothing about production-scale locking, concurrency, performance or paths no oracle covers ([Verification](VERIFICATION.md#honest-limits)).

## Testing it on your repository

This is the guide for someone outside this project who agrees to try the Action on a repository of theirs. Field results, including failed installations and false positives, are recorded in [ACTION_FIELD_RESULTS.md](ACTION_FIELD_RESULTS.md); please add yours through a pull request or in [#80](https://github.com/s2002kumar/DataProof/issues/80).

**What you need:** a PostgreSQL/Alembic repository whose `env.py` can take the database URL from `DATABASE_URL`; a GitHub-hosted `ubuntu-24.04` runner (Docker is present) or an ephemeral self-hosted one; a maintainer who can merge to the base branch.

**Steps, with what to time:**

1. On a branch, add `dataproof/config.toml`, a `dataproof/fixture.sql` that loads a few hundred representative rows into the base schema (plain SQL statements), one to three oracles under `dataproof/oracles/`, and the workflow from the quickstart pinned to a commit. Note how long this took and what was unclear.
2. Merge that branch. The pull request that introduces the configuration cannot be verified by it (its job ends `Not verified (ERROR)`): expected.
3. Open a pull request with a migration you already know is correct. Record the job's duration from the Actions page and the verdict. If the migration changes the schema the oracles read, the run is `INCOMPLETE` until you add the binding digest from the summary to `approved_bindings` and re-run; record that too.
4. If you can, open a pull request with a migration you know is wrong (an old bug of yours, re-applied) and record whether DataProof blocks it, and on which check.
5. Send back, in the issue or a pull request to the field-results file: the repository (or a description if it is private), PostgreSQL and Alembic versions, the four timings (setup, first job, verdict, approval round trip), the verdicts, the job summary text if you can share it (it holds values from your fixture only), and anything that was wrong, confusing or slow. A false positive, a crash and a clean install are all equally useful.

**What we do not need:** access to your repository, your data, or your secrets. The Action never sends anything anywhere; its only network use is pulling the pinned images and installing the pinned packages on the runner.

**What to expect:** no version tag yet, so pin a commit; the Action may change between commits; breaking changes will be listed in the field-results file.

## Not in the Action

| Capability | Where it is |
| --- | --- |
| API behaviour contracts (C6) against live previews | GitHub App |
| Previews of both commits behind a signed gateway | GitHub App |
| AI Change Analyst and Failure Investigator (bounded, advisory) | GitHub App |
| Reproduction of a failure on a subset of the reference | GitHub App |
| Interactive binding approval bound to a commit, with the approver rule | GitHub App; the Action approves through the base branch |
| Sealed snapshots with a verified restore | The Action dumps both databases; it does not restore them to verify |
