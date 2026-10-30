# dataproof-contracts

Typed result contracts shared by DataProof services. They are Pydantic v2 models, exported as JSON Schema 2020-12, and implement the data contracts in [ARCHITECTURE](../../docs/ARCHITECTURE.md). Every model is immutable and rejects unknown fields, so an untrusted document cannot smuggle extra keys past validation.

## Models and the invariants they enforce

| Model | Purpose | Enforced at validation time |
| --- | --- | --- |
| `ReviewRun` | One review run and its lifecycle state | `input_digest` is the canonical SHA-256 of `inputs` (the idempotency key); `binding_approval` and `correction_replay` runs carry `parent_run_id`, other runs do not; `NOT_EXECUTED` exactly when a policy reason is given. There is no binding-review run state. |
| `DataContract` | Protected `oracle` plus untrusted `candidate_binding` (ADR-0005) | The binding has no fields for comparators, tolerances, requiredness or key filters; `APPROVED` only with the digest of the current binding, so any binding edit needs new review; `binding_coverage_gaps()` reports unmapped oracle columns instead of dropping them |
| `CheckResult` | Outcome of one deterministic check | `NOT_RUN` exactly when a reason is given; `ERROR` exactly when an error code is given; `PASS` has no mismatches; `FAIL` cites evidence; a check that did not run reports nothing |
| `Finding` | A reviewer-facing finding | Every finding cites evidence; non-static findings reference their deterministic check; without a source location, localization is `UNKNOWN` |
| `EvidenceManifest` | Sealed evidence for one run | At most one source artifact and one snapshot per environment; restore verifications refer to snapshots in the manifest; evidence IDs are unique; retention ends after sealing |
| `HumanDecision` | A recorded human action | Binding decisions are bound to a binding digest and the PR author; an `ORGANIZATIONAL` approval by the PR author is rejected; a single-maintainer approval is recorded as `PROCEDURAL` |

`github_check_conclusion()` maps report verdicts to GitHub check conclusions. Only `CHECKED` and `CHECKED_WITH_WARNINGS` map to `success`; `NOT_EXECUTED` is `neutral` and `INCOMPLETE` is `action_required`.

## Value encoding

Evidence values keep their database type (`TypedValue`, tagged by `type`):

- **Decimals** are JSON strings in Python's `str(Decimal)` form, for example `"49.99"` or `"0E-8"`. Floats, booleans, NaN and infinities are rejected, and scale is preserved (`"49.990"` stays `"49.990"`).
- **Timestamps** are ISO 8601 strings with an offset. Values are pinned to a fixed UTC offset on validation. Python compares and subtracts datetimes that share a `tzinfo` object by wall clock and ignores `fold`, so two different instants on a DST fall-back night would otherwise compare equal.
- **SQL NULL** is `{"type": "null"}`, distinct from an empty string or zero. In a `Mismatch`, a JSON `null` for `expected` or `observed` means the key is absent on that side.
- **Keys** are arrays of typed values, never delimited strings.
- **Digests** are lowercase hex SHA-256, and Git SHAs are full 40-character hashes. Canonical JSON for digests uses sorted keys, no insignificant whitespace and UTF-8.

## Schemas and examples

[`schemas/`](schemas/) is generated from the models in serialization mode, so it describes the JSON that DataProof emits. Pydantic's OpenAPI-only `discriminator` keyword is removed, which makes the output plain JSON Schema that strict validators accept. Regenerate after changing a model:

```sh
uv run python -m dataproof_contracts.schema packages/contracts/schemas
```

Add `--check` to verify instead of write. A pre-commit hook and a unit test enforce this.

[`examples/`](examples/) holds fictional, valid documents named `<Model>.<case>.json`. They are test baselines: Python validates and round-trips each one and checks it against its schema, and the TypeScript suite in [`ts/`](ts/) does the same with Ajv in strict mode.
