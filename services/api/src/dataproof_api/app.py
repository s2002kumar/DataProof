"""The control plane's HTTP surface. So far: ``POST /github/webhook``."""

import json
from typing import Annotated, Any

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.concurrency import run_in_threadpool

from dataproof_api.intake import Intake
from dataproof_api.webhooks import verify_signature

#: GitHub caps webhook payloads at 25 MB; pull request events are far smaller.
MAX_WEBHOOK_BYTES = 5 * 1024 * 1024


def create_app(intake: Intake, *, webhook_secret: bytes) -> FastAPI:
    if not webhook_secret:
        raise ValueError("a webhook secret is required")
    app = FastAPI(title="DataProof control plane", docs_url=None, redoc_url=None)

    @app.post("/github/webhook", status_code=202)
    async def github_webhook(
        request: Request,
        x_github_event: Annotated[str, Header()],
        x_github_delivery: Annotated[str, Header(min_length=1, max_length=100)],
        x_hub_signature_256: Annotated[str | None, Header()] = None,
    ) -> dict[str, Any]:
        length = request.headers.get("content-length")
        if length is not None and (not length.isdigit() or int(length) > MAX_WEBHOOK_BYTES):
            raise HTTPException(413, "payload too large")
        body = await request.body()
        if len(body) > MAX_WEBHOOK_BYTES:
            raise HTTPException(413, "payload too large")
        if not verify_signature(webhook_secret, body, x_hub_signature_256):
            raise HTTPException(401, "invalid signature")
        try:
            payload = json.loads(body)
        except ValueError:
            raise HTTPException(400, "body is not JSON") from None
        if not isinstance(payload, dict):
            raise HTTPException(400, "body is not a JSON object")
        result = await run_in_threadpool(intake.receive, x_github_delivery, x_github_event, payload)
        return {
            "outcome": result.outcome.value,
            "run_id": str(result.run_id) if result.run_id else None,
            "duplicate": result.duplicate,
            "detail": dict(result.detail),
        }

    return app
