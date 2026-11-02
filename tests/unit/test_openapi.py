"""The committed OpenAPI document matches the routes, and every write is a POST."""

from pathlib import Path

from dataproof_api.openapi import document, render

COMMITTED = Path(__file__).parents[2] / "services" / "api" / "openapi.json"


def test_the_committed_document_is_current() -> None:
    assert COMMITTED.read_text("utf-8") == render(), (
        "regenerate: uv run python -m dataproof_api.openapi services/api/openapi.json"
    )


def test_the_documented_routes() -> None:
    paths = document()["paths"]
    assert {path: sorted(paths[path]) for path in paths} == {
        "/github/webhook": ["post"],
        "/auth/login": ["get"],
        "/auth/callback": ["get"],
        "/auth/logout": ["post"],
        "/api/session": ["get"],
        "/api/runs": ["get"],
        "/api/runs/{run_id}": ["get"],
        "/api/runs/{run_id}/checks": ["get"],
        "/api/runs/{run_id}/evidence": ["get"],
        "/api/runs/{run_id}/binding-decisions": ["post"],
        "/api/runs/{run_id}/cancel": ["post"],
        "/api/runs/{run_id}/correction-replay": ["post"],
        "/api/runs/{run_id}/previews/{role}": ["post"],
        "/api/runs/{run_id}/reproductions": ["post"],
    }
