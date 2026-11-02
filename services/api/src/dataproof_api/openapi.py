"""The control plane API's OpenAPI document.

The committed ``services/api/openapi.json`` is what clients (the dashboard) are written against.
Regenerate it with::

    uv run python -m dataproof_api.openapi services/api/openapi.json

and verify it is current with ``--check``. The document is built from the routes alone: the
placeholder dependencies below are never called.
"""

import argparse
import json
import sys
from collections.abc import Sequence
from datetime import timedelta
from pathlib import Path
from typing import Any

import httpx2
from cryptography.hazmat.primitives.asymmetric import rsa

from dataproof_api.app import create_app
from dataproof_api.github_app import GitHubApp
from dataproof_api.intake import Intake
from dataproof_api.reviewer import Reviewer
from dataproof_api.reviewers import GitHubSignIn, Permissions
from dataproof_api.sessions import Sessions
from dataproof_control import GitHubRecords, RetryPolicy, RunResults, RunStore


def document() -> dict[str, Any]:
    nowhere = "postgresql:///unused"
    store = RunStore(nowhere)
    records = GitHubRecords(nowhere)
    client = httpx2.Client(base_url="https://github.invalid")
    app = GitHubApp(
        "0", rsa.generate_private_key(public_exponent=65537, key_size=2048), client=client
    )
    reviewer = Reviewer(
        store,
        RunResults(nowhere),
        records,
        Sessions(b"x" * 32),
        GitHubSignIn(
            "client", "secret", "https://dataproof.invalid/auth/callback", web=client, api=client
        ),
        Permissions(app, {}),
        configs={},
        public_origin="https://dataproof.invalid",
        run_ttl=timedelta(hours=1),
        retry=RetryPolicy(),
    )
    intake = Intake(store, records, {}, run_ttl=timedelta(hours=1), retry=RetryPolicy())
    return create_app(intake, webhook_secret=b"unused", reviewer=reviewer).openapi()


def render() -> str:
    return json.dumps(document(), indent=2, sort_keys=True) + "\n"


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("path", type=Path)
    parser.add_argument("--check", action="store_true", help="fail if the document is stale")
    args = parser.parse_args(argv)
    rendered = render()
    if args.check:
        if not args.path.exists() or args.path.read_text("utf-8") != rendered:
            print(f"stale OpenAPI document: {args.path}", file=sys.stderr)
            return 1
        return 0
    args.path.write_text(rendered, "utf-8", newline="\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
