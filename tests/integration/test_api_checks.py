"""API behaviour checks: the key universe from the reference database, the routes from contracts.

The reference database is real PostgreSQL; the two APIs are recording fakes, so each test can
say exactly which requests were made and what came back.
"""

from collections.abc import Iterator, Mapping
from decimal import Decimal
from typing import Any

import psycopg
import pytest
from pydantic import ValidationError

from commerce_env import DatabaseFactory
from dataproof_contracts import (
    BindingReviewStatus,
    CandidateBinding,
    CheckOutcome,
    Comparator,
    ComparatorKind,
    DataContract,
    DecimalValue,
    ErrorCode,
    NotRunReason,
    Oracle,
    Severity,
)
from dataproof_verifier import ApiRequestError, verify_api_contract

pytestmark = pytest.mark.integration

DATASET = "d" * 64
REFUNDS = "/orders/{order_id}/refunds"
TRANSACTIONS = "/orders/{order_id}/transactions?kind=REFUND"


class FakeApi:
    def __init__(self, responses: Mapping[str, Any]) -> None:
        self.responses = dict(responses)
        self.calls: list[str] = []

    def get(self, path: str) -> Any:
        self.calls.append(path)
        if path not in self.responses:
            raise ApiRequestError("HTTP 404")
        return self.responses[path]


@pytest.fixture
def reference(new_database: DatabaseFactory) -> Iterator[psycopg.Connection[Any]]:
    dsn = new_database()
    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute("CREATE TABLE orders (order_id integer PRIMARY KEY)")
        conn.execute("INSERT INTO orders VALUES (1), (2), (3)")
        yield conn


def contract(
    *,
    route: str | None = TRANSACTIONS,
    query_sql: str | None = None,
    approve: bool = True,
    mapping: Mapping[str, str] | None = None,
) -> DataContract:
    binding = CandidateBinding(
        source_path="dataproof/bindings/c6.toml",
        head_sha="c" * 40,
        api_route=route,
        query_sql=query_sql,
        column_mapping=dict(
            mapping or {"order_id": "order_id", "event_id": "source_event_id", "amount": "amount"}
        ),
    )
    return DataContract(
        contract_id="commerce.c6_refund_events_api",
        version=1,
        description="Refund events through the API",
        required=True,
        severity=Severity.HIGH,
        oracle=Oracle(
            source_path="dataproof/oracles/c6.toml",
            base_sha="b" * 40,
            query_sql="SELECT order_id FROM orders ORDER BY order_id",
            api_route=REFUNDS,
            key_columns=("order_id", "event_id"),
            expected_columns={"amount": Comparator(kind=ComparatorKind.DECIMAL, decimal_scale=2)},
        ),
        candidate_binding=binding,
        binding_review=BindingReviewStatus.APPROVED if approve else BindingReviewStatus.PENDING,
        approved_binding_digest=binding.digest() if approve else None,
    )


def reference_api() -> FakeApi:
    return FakeApi(
        {
            "/orders/1/refunds": [{"event_id": 1002, "amount": "49.99", "external_ref": "R1"}],
            "/orders/2/refunds": [
                {"event_id": 1004, "amount": "12.50"},
                {"event_id": 1005, "amount": "37.49"},
            ],
            "/orders/3/refunds": [],
        }
    )


def candidate_api(**overrides: Any) -> FakeApi:
    responses: dict[str, Any] = {
        "/orders/1/transactions?kind=REFUND": [
            {"source_event_id": 1002, "ledger_sequence": 2, "amount": "49.99"}
        ],
        "/orders/2/transactions?kind=REFUND": [
            {"source_event_id": 1005, "ledger_sequence": 1, "amount": "37.49"},
            {"source_event_id": 1004, "ledger_sequence": 2, "amount": "12.50"},
        ],
        "/orders/3/transactions?kind=REFUND": [],
    }
    responses.update(overrides)
    return FakeApi(responses)


def test_equivalent_apis_pass_whatever_their_shape_and_order(
    reference: psycopg.Connection[Any],
) -> None:
    ref, cand = reference_api(), candidate_api()
    result = verify_api_contract(contract(), reference, ref, cand, DATASET)
    assert result.check.outcome is CheckOutcome.PASS, result
    assert result.check.expected_key_count == result.check.observed_key_count == 3
    assert ref.calls == ["/orders/1/refunds", "/orders/2/refunds", "/orders/3/refunds"]
    assert cand.calls == [f"/orders/{n}/transactions?kind=REFUND" for n in (1, 2, 3)]
    kinds = {e.evidence_id.rsplit(":", 1)[-1] for e in result.evidence}
    assert kinds == {"universe", "api"}


def test_a_multiplied_refund_fails_with_the_exact_amounts(
    reference: psycopg.Connection[Any],
) -> None:
    cand = candidate_api(
        **{
            "/orders/1/transactions?kind=REFUND": [
                {"source_event_id": 1002, "ledger_sequence": 2, "amount": "99.98"}
            ]
        }
    )
    result = verify_api_contract(contract(), reference, reference_api(), cand, DATASET)
    assert result.check.outcome is CheckOutcome.FAIL
    (mismatch,) = result.check.mismatch_examples
    assert (mismatch.expected, mismatch.observed) == (
        DecimalValue(value=Decimal("49.99")),
        DecimalValue(value=Decimal("99.98")),
    )


def test_a_candidate_cannot_narrow_the_universe_by_omitting_an_item(
    reference: psycopg.Connection[Any],
) -> None:
    cand = candidate_api(
        **{"/orders/2/transactions?kind=REFUND": [{"source_event_id": 1004, "amount": "12.50"}]}
    )
    result = verify_api_contract(contract(), reference, reference_api(), cand, DATASET)
    assert result.check.outcome is CheckOutcome.FAIL
    assert result.check.mismatch_count == 1


def test_an_unapproved_binding_calls_nothing(reference: psycopg.Connection[Any]) -> None:
    ref, cand = reference_api(), candidate_api()
    result = verify_api_contract(contract(approve=False), reference, ref, cand, DATASET)
    assert result.check.not_run_reason is NotRunReason.AWAITING_BINDING_REVIEW
    assert ref.calls == cand.calls == []


@pytest.mark.parametrize(
    ("binding", "code"),
    [
        ({"route": None, "query_sql": "SELECT 1"}, ErrorCode.BINDING_UNSUPPORTED_CONSTRUCT),
        ({"route": "/orders/{order_ref}/transactions"}, ErrorCode.BINDING_UNSUPPORTED_CONSTRUCT),
        (
            {"mapping": {"order_id": "order_id", "event_id": "source_event_id"}},
            ErrorCode.BINDING_COVERAGE_GAP,
        ),
    ],
)
def test_unusable_bindings_are_errors(
    reference: psycopg.Connection[Any], binding: dict[str, Any], code: ErrorCode
) -> None:
    result = verify_api_contract(
        contract(**binding), reference, reference_api(), candidate_api(), DATASET
    )
    assert (result.check.outcome, result.check.error_code) == (CheckOutcome.ERROR, code)


@pytest.mark.parametrize(
    ("override", "code"),
    [
        (
            {"/orders/1/transactions?kind=REFUND": [{"source_event_id": 1002, "amount": 49.99}]},
            ErrorCode.UNSUPPORTED_VALUE_TYPE,
        ),
        ({"/orders/1/transactions?kind=REFUND": ["not an object"]}, ErrorCode.API_REQUEST_FAILED),
        (
            {"/orders/2/transactions?kind=REFUND": [{"source_event_id": 1005, "amount": "1"}] * 2},
            ErrorCode.DUPLICATE_KEY,
        ),
    ],
)
def test_untrustworthy_responses_are_errors(
    reference: psycopg.Connection[Any], override: dict[str, Any], code: ErrorCode
) -> None:
    cand = candidate_api(**override)
    result = verify_api_contract(contract(), reference, reference_api(), cand, DATASET)
    assert (result.check.outcome, result.check.error_code) == (CheckOutcome.ERROR, code)


def test_a_failed_request_is_an_error(reference: psycopg.Connection[Any]) -> None:
    cand = candidate_api()
    del cand.responses["/orders/3/transactions?kind=REFUND"]
    result = verify_api_contract(contract(), reference, reference_api(), cand, DATASET)
    assert result.check.error_code is ErrorCode.API_REQUEST_FAILED


def test_the_request_budget_is_enforced(reference: psycopg.Connection[Any]) -> None:
    ref = reference_api()
    result = verify_api_contract(
        contract(), reference, ref, candidate_api(), DATASET, max_requests=2
    )
    assert result.check.error_code is ErrorCode.RESULT_LIMIT_EXCEEDED
    assert ref.calls == []


def test_route_values_are_url_encoded(reference: psycopg.Connection[Any]) -> None:
    reference.execute("CREATE TABLE skus (sku text PRIMARY KEY)")
    reference.execute("INSERT INTO skus VALUES ('a/b?c')")
    base = contract()
    oracle = base.oracle.model_copy(
        update={
            "query_sql": "SELECT sku FROM skus",
            "api_route": "/skus/{sku}",
            "key_columns": ("sku",),
            "expected_columns": {"price": Comparator(kind=ComparatorKind.DECIMAL, decimal_scale=2)},
        }
    )
    plain = DataContract.model_validate(
        {
            **base.model_dump(),
            "oracle": oracle.model_dump(),
            "candidate_binding": None,
            "binding_review": "NOT_REQUIRED",
            "approved_binding_digest": None,
        }
    )
    api = FakeApi({"/skus/a%2Fb%3Fc": {"price": "1.00"}})
    result = verify_api_contract(plain, reference, api, api, DATASET)
    assert result.check.outcome is CheckOutcome.PASS
    assert api.calls == ["/skus/a%2Fb%3Fc", "/skus/a%2Fb%3Fc"]


@pytest.mark.parametrize(
    "route", ["http://attacker.example/x", "//attacker.example/x", "/orders/../admin", "orders/1"]
)
def test_routes_can_only_name_local_paths(route: str) -> None:
    with pytest.raises(ValidationError):
        CandidateBinding(
            source_path="b.toml", head_sha="c" * 40, api_route=route, column_mapping={"a": "b"}
        )
