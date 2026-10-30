# Contributing

DataProof is built in small, reviewed pull requests that follow the [implementation plan](docs/IMPLEMENTATION.md). The rules in [AGENTS.md](AGENTS.md) apply to every change, whoever or whatever writes it.

## Setup

Requirements: [uv](https://docs.astral.sh/uv/) 0.12 or later (it installs Python 3.12), Node.js 24 with pnpm 11, and Docker for local PostgreSQL integration tests.

```sh
uv sync
pnpm install
uv run pre-commit install
```

## Checks

Run these before pushing. CI runs the same commands, and every check must pass before a pull request merges.

| Check | Command |
| --- | --- |
| Formatting, lint, `mypy --strict`, schema drift, file hygiene, secret scan | `uv run pre-commit run --all-files` |
| Python unit and PostgreSQL integration tests | `uv run pytest` |
| TypeScript type check and JSON Schema conformance | `pnpm run typecheck` and `pnpm run test` |

Integration tests need a real PostgreSQL. Set `DATAPROOF_TEST_DATABASE_URL`, or keep Docker running so that testcontainers can start `postgres:17-alpine`. The tests never skip: without a database they error.

The sample app has its own lockfile: after changing `sample-app/pyproject.toml`, run `uv lock --project sample-app`.

After changing a contract model, regenerate its JSON Schema as described in the [contracts README](packages/contracts/README.md#schemas-and-examples).

## Pull requests

- Branch from `main` and work on one issue per branch.
- Use conventional commit messages (`feat(contracts):`, `test(verifier):`, `docs:`, `ci:`), and keep each commit focused and truthful.
- Fill in the pull request template: motivation, design changes, the tests that actually ran and their results, and risks with a rollback path.
- `main` is protected. Changes land only through pull requests with every required check green.
