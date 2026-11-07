# Testing the Action on your repository

A short invitation, and the checklist for whoever accepts it. The owner sends the invitation; nobody is contacted by the project's tooling. Results go into [ACTION_FIELD_RESULTS.md](ACTION_FIELD_RESULTS.md), and nothing there or anywhere else says "external" until a person other than the author has run it.

## The invitation

> I'm looking for one or two developers with a PostgreSQL + Alembic codebase to try a GitHub Action I built, DataProof, on a pull request, and to tell me what broke.
>
> It runs your pull request's migration in two throwaway PostgreSQL containers, one at your main branch's schema and one after the migration, and compares the data against a few contracts you write on your main branch, such as "every invoice keeps its total to the cent". The job fails if the migration ran fine but changed data it shouldn't have. The migration runs with no credentials and no network, and nothing leaves your runner.
>
> Setup is a config file, a small SQL fixture of fake data, one or two contract files and a workflow, merged to your main branch. On the example repository, a run takes about 40 seconds. I'd ask for about an hour, and for your notes on anything that was confusing, slow or wrong, false alarms included.
>
> Quickstart: <https://github.com/s2002kumar/DataProof/blob/main/docs/ACTION.md>. Example: <https://github.com/s2002kumar/dataproof-example-subscriptions>.

## The checklist

### Before you start

- [ ] A repository with Alembic migrations against PostgreSQL. A fork or a scratch copy is fine.
- [ ] GitHub-hosted runners (`ubuntu-24.04`), or a self-hosted runner that is ephemeral. Do not use a persistent self-hosted runner for this.
- [ ] Someone who can merge to the base branch, because the configuration must be merged before the Action can use it.

### Set up (time this)

- [ ] `dataproof/config.toml`: the path of your `alembic.ini`, a fixture, and the directories for oracles and bindings ([configuration reference](ACTION.md#configuration-reference)).
- [ ] If your `env.py` reads its own settings instead of `DATABASE_URL`, list them under `[alembic.environment]`, using placeholders for the database's coordinates. If it imports your app's packages, list them in a pinned `alembic.requirements` file.
- [ ] `dataproof/fixture.sql`: a few hundred rows of fake data at your main branch's schema. Plain `INSERT` statements are enough.
- [ ] One to three oracles under `dataproof/oracles/`: a query over today's schema, its key columns, and how each value is compared.
- [ ] `.github/workflows/dataproof.yml` from the quickstart, pinned to the commit it names.
- [ ] Merge all of it. The pull request that adds it reports `Not verified (ERROR)`; that is expected.

### Run it

- [ ] A pull request with a migration you know is correct. Note the job's duration and the verdict. If it changes the schema your oracles read, approve the binding digest the summary names on your main branch, then re-run the job.
- [ ] If you can, a pull request with a migration you know is wrong, such as an old bug of yours reapplied. Does DataProof block it, and on which check?

### Send back

- [ ] Which repository it was, or a description if it's private, and your PostgreSQL and Alembic versions.
- [ ] Four timings: setup, first job, time to a verdict, and the approval round trip.
- [ ] The verdicts, and the job summaries if you can share them. They hold values from your fixture only.
- [ ] Everything that was wrong, confusing or slow. A crash, a false alarm and a clean run are equally useful.

**What is never needed:** access to your repository, your data or your secrets. The Action sends nothing anywhere. Its only network use is pulling the pinned container images and installing the pinned packages on your runner.
