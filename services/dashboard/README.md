# DataProof dashboard

The reviewer's view of DataProof (ARCHITECTURE, review UI; work package 9, #47). It is a Next.js app that talks only to the [control plane API](../api/README.md).

## Pages

- **Runs** (`/`): the runs of every registered repository the signed-in reviewer can read on GitHub, newest first. Each row shows the repository, pull request, head commit, trigger, state and verdict.
- **Run** (`/runs/{id}`):
  - **Facts:** repository, pull request and author, base and head commits (and the fork, if any), state, error or not-executed reason, supersession, trigger and parent run, and the GitHub check run.
  - **Coverage:** every check's requiredness, outcome, reason and mismatch count, as the report recorded them.
  - **Previews:** buttons that open the reference and candidate previews through the [signed gateway](../api/README.md#signed-preview-gateway), each on its own host, in a new tab with `noopener,noreferrer`.
  - **Checks:** outcomes, the first mismatches (key, column, expected and observed, with SQL `NULL` and absent values told apart), and the evidence each check cites.
  - **Binding inspector:** for a check awaiting binding review, the binding's SQL or API route, its column mapping, and the relations it reads with their provenance. Relations the pull request did not create or alter are flagged (ADR-0005). The inspector has **Approve** and **Reject**, with an optional comment, and states that approving does not approve the migration or change a result.
  - **Findings:** investigations labelled **Advisory**, with their hypotheses, confidence, cited evidence IDs, suggested action and caveat. Reproductions, with their outcome and subset.
  - **Decisions:** every recorded decision, a correction-replay form (a full commit SHA), and cancellation.
  - **Timeline:** every transition, with its actor.

## Security

- The dashboard and the API share one origin. In development Next.js forwards `/api` and `/auth` to `DATAPROOF_API_ORIGIN` (default `http://127.0.0.1:8000`); a production reverse proxy may route them directly. The session cookie never leaves that origin.
- Every write sends the session's CSRF token; the browser adds the `Origin` the API checks.
- Text from a pull request (binding SQL, routes, paths, check IDs) is rendered only as React text, never as HTML. A test renders SQL containing markup and checks that no element is created.
- Pages are served with a restrictive Content Security Policy (`default-src 'self'`, `object-src 'none'`, `frame-ancestors 'none'`), `X-Frame-Options: DENY`, `nosniff` and `Referrer-Policy: no-referrer`.
- Previews run pull-request code, so they never render inside the dashboard. They open on their own hosts, through the gateway.

## Commands

```bash
pnpm --filter @dataproof/dashboard dev        # needs the API at DATAPROOF_API_ORIGIN
pnpm --filter @dataproof/dashboard typecheck  # next typegen, then tsc
pnpm --filter @dataproof/dashboard test       # vitest (jsdom)
pnpm --filter @dataproof/dashboard build
```

The dashboard stays on TypeScript 5.9 because Next.js type-checks through the TypeScript compiler API, which the native TypeScript 7 does not provide.

## Tests

`test/` runs in CI's `typescript` job, which also builds the app. Fixtures come from the contract package's validated examples where they exist.

- **API client:** writes carry the CSRF token with same-origin credentials, refusals become errors that carry the server's reason, and run IDs are encoded so they cannot change the route.
- **Binding inspector:** hostile SQL is shown as text; relations the pull request did not create or alter are flagged; the reviewer's decision is sent with their comment; a refusal is shown.
- **Views:** a failed check's mismatch row reads `1002 amount 49.99 99.98`; findings are labelled advisory and show their citations; a reproduction shows its outcome; the runs list links to each run and shows its verdict.

## Not built yet

These are the remaining steps of work package 9 (#47):

- a browser end-to-end test of the whole review against a local stack;
