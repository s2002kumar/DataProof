"""Registering DataProof's GitHub App from a manifest, run by the owner on their own machine.

GitHub's manifest flow creates an App from a description: this module serves a local page that
posts the manifest to GitHub, receives the one-time code on its callback, and exchanges it for
the App's credentials. The App asks for the least DataProof needs (``PERMISSIONS``) and only
``pull_request`` events, and is private to its owner. Its private key, webhook secret and
OAuth client secret are written to a directory outside the repository with owner-only
permissions; nothing prints them. The App is then installed on the demo repository alone.
"""

import html
import json
import os
import secrets
import stat
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx2
import uvicorn
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import HTMLResponse

from dataproof_api.github_app import API_VERSION, PERMISSIONS

EVENTS = ("pull_request",)


def app_manifest(
    *, name: str, webhook_url: str, dashboard_origin: str, redirect_url: str, homepage: str
) -> dict[str, Any]:
    """The App DataProof needs: commit-bound checks and read access, nothing more."""
    return {
        "name": name,
        "url": homepage,
        "hook_attributes": {"url": webhook_url, "active": True},
        "redirect_url": redirect_url,
        "callback_urls": [f"{dashboard_origin.rstrip('/')}/auth/callback"],
        "public": False,
        "default_permissions": dict(PERMISSIONS),
        "default_events": list(EVENTS),
        "request_oauth_on_install": False,
    }


@dataclass(frozen=True)
class RegisteredApp:
    app_id: int
    slug: str
    client_id: str
    html_url: str


def save_credentials(conversion: dict[str, Any], directory: Path) -> RegisteredApp:
    """Write the App's secrets with owner-only permissions; return what is safe to show.

    The deployment's own session and preview-grant secrets are generated here too, so the one
    file holds every secret the stack needs besides the control database's address.
    """
    directory.mkdir(parents=True, exist_ok=True)
    key = directory / "github-app.pem"
    key.write_text(conversion["pem"], "utf-8")
    environment = directory / "secrets.env"
    environment.write_text(
        f"DATAPROOF_WEBHOOK_SECRET={conversion['webhook_secret']}\n"
        f"DATAPROOF_GITHUB_CLIENT_SECRET={conversion['client_secret']}\n"
        f"DATAPROOF_SESSION_SECRET={secrets.token_urlsafe(48)}\n"
        f"DATAPROOF_PREVIEW_SECRET={secrets.token_urlsafe(48)}\n",
        "utf-8",
    )
    for path in (key, environment):
        os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)
    app = RegisteredApp(
        app_id=int(conversion["id"]),
        slug=str(conversion["slug"]),
        client_id=str(conversion["client_id"]),
        html_url=str(conversion["html_url"]),
    )
    (directory / "github-app.json").write_text(json.dumps(app.__dict__, indent=2) + "\n", "utf-8")
    return app


class Registration:
    """The local page and callback of the manifest flow."""

    def __init__(
        self,
        manifest: dict[str, Any],
        directory: Path,
        *,
        owner: str | None,
        github: str = "https://github.com",
        api: httpx2.Client,
    ) -> None:
        self.manifest = manifest
        self.directory = directory
        self.state = secrets.token_urlsafe(32)
        prefix = f"/organizations/{owner}" if owner else ""
        self.target = f"{github.rstrip('/')}{prefix}/settings/apps/new?state={self.state}"
        self.api = api
        self.registered: RegisteredApp | None = None
        self.done = threading.Event()

    def app(self) -> FastAPI:
        app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)

        @app.get("/", response_class=HTMLResponse)
        def start() -> str:
            manifest = html.escape(json.dumps(self.manifest), quote=True)
            return (
                "<!doctype html><title>Register DataProof</title>"
                f'<form method="post" action="{html.escape(self.target, quote=True)}">'
                f'<input type="hidden" name="manifest" value="{manifest}">'
                "<p>This creates a private GitHub App with checks write and read-only access to "
                "contents, metadata and pull requests, receiving pull_request events.</p>"
                '<button type="submit">Create the App on GitHub</button></form>'
            )

        @app.get("/callback", response_class=HTMLResponse)
        def callback(
            code: str = Query(min_length=1, max_length=200),
            state: str = Query(min_length=1, max_length=200),
        ) -> str:
            if not secrets.compare_digest(state, self.state):
                raise HTTPException(400, "this registration did not start here")
            response = self.api.post(
                f"/app-manifests/{code}/conversions",
                headers={
                    "Accept": "application/vnd.github+json",
                    "X-GitHub-Api-Version": API_VERSION,
                },
            )
            if response.status_code != 201:
                raise HTTPException(502, f"GitHub answered {response.status_code}")
            self.registered = save_credentials(response.json(), self.directory)
            self.done.set()
            install = html.escape(f"{self.registered.html_url}/installations/new", quote=True)
            return (
                "<!doctype html><title>DataProof registered</title>"
                f"<p>Registered App {self.registered.slug} (ID {self.registered.app_id}). "
                f"Credentials are in {html.escape(str(self.directory))}.</p>"
                f'<p>Install it on the demo repository only: <a href="{install}">{install}</a></p>'
            )

        return app

    def serve(self, port: int) -> RegisteredApp:
        """Serve the page until GitHub calls back; the registered App."""
        server = uvicorn.Server(
            uvicorn.Config(self.app(), host="127.0.0.1", port=port, log_level="warning")
        )
        thread = threading.Thread(target=server.run, daemon=True)
        thread.start()
        self.done.wait()
        server.should_exit = True
        thread.join(timeout=10)
        if self.registered is None:
            raise RuntimeError("GitHub did not complete the registration")
        return self.registered
