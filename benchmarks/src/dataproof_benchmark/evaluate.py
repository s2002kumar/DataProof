"""Run the corpus through the baselines and DataProof's contracts; score against the labels.

At the database tier each case is migrated into a fresh candidate (``harness``) and compared with
one reference:

* ``B0``, execution smoke: did the migration succeed? (Its API smoke checks need live previews,
  which this tier does not run; they are part of the pipeline tier.)
* ``B1``, reconciliation over the reviewed binding view: row counts, a hash of the key set, and
  refund and capture sums per order and currency. It is credited wherever these catch a fault,
  F1 included (#12).
* ``B2``, DataProof's deterministic contracts C1 to C3 with the case's bindings approved.

Labels are read only after every case has run. The results contain nothing that varies between
runs (no timings), so a rerun must reproduce them exactly.
"""

from collections.abc import Mapping
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import psycopg

import commerce_scenario
from commerce_scenario import PUBLISHED_DIGEST
from dataproof_benchmark.corpus import C1, C2, C3
from dataproof_benchmark.harness import DatabaseFactory, candidate, databases, reference
from dataproof_contracts import CheckOutcome, DataContract
from dataproof_verifier import (
    QueryLimits,
    Verification,
    assemble_contract,
    load_binding,
    load_oracle,
    verify_contract,
)

ORACLES = Path(commerce_scenario.__file__).resolve().parents[2] / "oracles"
BASE_SHA, HEAD_SHA = "b" * 40, "c" * 40
CONTRACTS = (C1, C2, C3)

_RECONCILIATION = {
    "reference": (
        "SELECT count(*), md5(string_agg(event_id::text, ',' ORDER BY event_id)) "
        "FROM legacy_payment_events",
        "SELECT e.order_id, o.currency::text, e.event_kind::text, SUM(e.amount) "
        "FROM legacy_payment_events AS e JOIN orders AS o USING (order_id) "
        "GROUP BY 1, 2, 3",
    ),
    "candidate": (
        "SELECT count(*), md5(string_agg(source_event_id::text, ',' ORDER BY source_event_id)) "
        "FROM ledger_entries",
        "SELECT l.order_id, o.currency::text, l.entry_kind::text, SUM(l.amount) "
        "FROM ledger_entries AS l JOIN orders AS o USING (order_id) "
        "GROUP BY 1, 2, 3",
    ),
}


def contracts(change_dir: Path) -> list[DataContract]:
    """The commerce data contracts with the case's own bindings, approved."""
    result = []
    for oracle_path in sorted(ORACLES.glob("*.toml")):
        spec, oracle = load_oracle(oracle_path, source_path=oracle_path.name, base_sha=BASE_SHA)
        if oracle.api_route is not None:
            continue
        binding_path = change_dir / "bindings" / oracle_path.name
        _, binding = load_binding(binding_path, source_path=binding_path.name, head_sha=HEAD_SHA)
        result.append(
            assemble_contract(
                spec, oracle, binding, approved_digests={binding.digest()}, rejected_digests=()
            )
        )
    return result


def reconcile(reference_dsn: str, candidate_dsn: str) -> list[str]:
    """B1's signals: what differs in counts, keys and per-order sums."""
    measured = {}
    for side, dsn in (("reference", reference_dsn), ("candidate", candidate_dsn)):
        counts, totals = _RECONCILIATION[side]
        with psycopg.connect(dsn) as conn:
            row = conn.execute(counts).fetchone()
            sums = {tuple(r[:3]): r[3] for r in conn.execute(totals).fetchall()}
        measured[side] = (row, sums)
    (ref_counts, ref_sums), (cand_counts, cand_sums) = measured["reference"], measured["candidate"]
    signals = []
    if ref_counts is None or cand_counts is None or ref_counts[0] != cand_counts[0]:
        signals.append("row count")
    if ref_counts is None or cand_counts is None or ref_counts[1] != cand_counts[1]:
        signals.append("key hash")
    for kind in ("REFUND", "CAPTURE"):
        ref = {k: v for k, v in ref_sums.items() if k[2] == kind}
        cand = {k: v for k, v in cand_sums.items() if k[2] == kind}
        if ref != cand:
            signals.append(f"{kind.lower()} sums")
    return signals


@dataclass(frozen=True)
class CaseResult:
    case_id: str
    migrated: bool
    migration_error: str | None
    b1_signals: tuple[str, ...]
    outcomes: Mapping[str, str]
    """Each contract's outcome (``PASS``, ``FAIL``, ``ERROR`` or ``NOT_RUN``)."""
    error_codes: Mapping[str, str]
    c1_mismatch_count: int
    c1_example_keys: tuple[int, ...]
    c1_example_columns: tuple[str, ...]

    @property
    def detected_by(self) -> dict[str, bool]:
        failed = {CheckOutcome.FAIL.value, CheckOutcome.ERROR.value}
        return {
            "B0": not self.migrated,
            "B1": not self.migrated or bool(self.b1_signals),
            "B2": not self.migrated or any(o in failed for o in self.outcomes.values()),
        }


def run_case(new_database: DatabaseFactory, reference_dsn: str, change_dir: Path) -> CaseResult:
    case_id = "case-" + change_dir.name.rsplit("-", 1)[1]
    try:
        candidate_dsn = candidate(new_database, change_dir)
    except Exception as error:  # a failing migration is a result, not a crash
        return CaseResult(case_id, False, type(error).__name__, (), {}, {}, 0, (), ())
    verifications: dict[str, Verification] = {}
    with (
        psycopg.connect(reference_dsn, autocommit=True) as ref,
        psycopg.connect(candidate_dsn, autocommit=True) as cand,
    ):
        for contract in contracts(change_dir):
            verifications[contract.contract_id] = verify_contract(
                contract, ref, cand, PUBLISHED_DIGEST, QueryLimits()
            )
    c1 = verifications[C1].check
    keys = sorted({int(str(getattr(m.key[0], "value", 0))) for m in c1.mismatch_examples})
    columns = sorted({m.column for m in c1.mismatch_examples if m.column is not None})
    return CaseResult(
        case_id=case_id,
        migrated=True,
        migration_error=None,
        b1_signals=tuple(reconcile(reference_dsn, candidate_dsn)),
        outcomes={c: verifications[c].check.outcome.value for c in CONTRACTS},
        error_codes={
            c: v.check.error_code.value for c, v in verifications.items() if v.check.error_code
        },
        c1_mismatch_count=c1.mismatch_count,
        c1_example_keys=tuple(keys),
        c1_example_columns=tuple(columns),
    )


def run(admin_dsn: str, corpus: Path) -> list[CaseResult]:
    """Run every case of the corpus; labels are not read here."""
    changes = sorted((corpus / "changes").iterdir())
    with databases(admin_dsn) as new_database:
        reference_dsn = reference(new_database)
        return [run_case(new_database, reference_dsn, change) for change in changes]


def as_json(result: CaseResult) -> dict[str, Any]:
    return {**asdict(result), "detected_by": result.detected_by}
