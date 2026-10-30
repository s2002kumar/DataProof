"""Deterministic data-contract verification against PostgreSQL."""

from dataproof_verifier.compare import compare_keyed, values_match
from dataproof_verifier.engine import QueryEvidence, Verification, verify_contract
from dataproof_verifier.files import (
    BindingFile,
    OracleFile,
    assemble_contract,
    load_binding,
    load_oracle,
)
from dataproof_verifier.queries import QueryError, QueryLimits, QueryResult, run_read_only
from dataproof_verifier.verdict import derive_verdict

__all__ = [
    "BindingFile",
    "OracleFile",
    "QueryError",
    "QueryEvidence",
    "QueryLimits",
    "QueryResult",
    "Verification",
    "assemble_contract",
    "compare_keyed",
    "derive_verdict",
    "load_binding",
    "load_oracle",
    "run_read_only",
    "values_match",
    "verify_contract",
]
