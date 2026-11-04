"""Deterministic data-contract verification against PostgreSQL."""

from dataproof_verifier.api import ApiClient, ApiRequestError, verify_api_contract
from dataproof_verifier.binding_review import (
    ALLOWED_FUNCTIONS,
    BindingRejectedError,
    analyze_binding,
    check_read_query,
)
from dataproof_verifier.column_types import type_check, type_problems
from dataproof_verifier.compare import compare_keyed, values_match
from dataproof_verifier.engine import QueryEvidence, Verification, verify_contract
from dataproof_verifier.files import (
    BindingFile,
    OracleFile,
    assemble_contract,
    load_binding,
    load_oracle,
    parse_binding,
    parse_oracle,
)
from dataproof_verifier.queries import (
    ColumnType,
    QueryError,
    QueryLimits,
    QueryResult,
    run_read_only,
)
from dataproof_verifier.verdict import derive_verdict

__all__ = [
    "ALLOWED_FUNCTIONS",
    "ApiClient",
    "ApiRequestError",
    "BindingFile",
    "BindingRejectedError",
    "ColumnType",
    "OracleFile",
    "QueryError",
    "QueryEvidence",
    "QueryLimits",
    "QueryResult",
    "Verification",
    "analyze_binding",
    "assemble_contract",
    "check_read_query",
    "compare_keyed",
    "derive_verdict",
    "load_binding",
    "load_oracle",
    "parse_binding",
    "parse_oracle",
    "run_read_only",
    "type_check",
    "type_problems",
    "values_match",
    "verify_api_contract",
    "verify_contract",
]
