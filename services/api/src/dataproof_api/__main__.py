"""``python -m dataproof_api <process>``: run one process of a deployment (``deployment``)."""

import argparse
import logging
import os
import socket
import time
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path

import httpx2
import psycopg
import uvicorn

from dataproof_api.deployment import Stack
from dataproof_api.registration import Registration, app_manifest
from dataproof_control import migrate
from dataproof_runner.reaper import reap

log = logging.getLogger("dataproof")


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m dataproof_api",
        description="Run one DataProof process. Configuration: DATAPROOF_CONFIG and secrets "
        "in the environment (services/api/README.md).",
    )
    processes = parser.add_subparsers(dest="process", required=True)
    processes.add_parser("migrate", help="apply the control database's migrations")
    for name, port in (("api", 8000), ("webhook", 8001), ("gateway", 8080)):
        served = processes.add_parser(name, help=f"serve the {name}")
        served.add_argument("--host", default="127.0.0.1")
        served.add_argument("--port", type=int, default=port)
    register = processes.add_parser(
        "register-app", help="create the GitHub App from its manifest (run by the owner)"
    )
    register.add_argument("--name", required=True, help="a unique App name")
    register.add_argument("--webhook-url", required=True, help="https://<tunnel>/github/webhook")
    register.add_argument("--dashboard", default="http://127.0.0.1:3000")
    register.add_argument("--owner", help="an organization to own the App (default: you)")
    register.add_argument(
        "--secrets-dir", type=Path, default=Path.home() / ".dataproof" / "github-app"
    )
    register.add_argument("--port", type=int, default=8765)
    hook = processes.add_parser("webhook-url", help="point the App's webhook at a new address")
    hook.add_argument("url", help="the public tunnel address, e.g. https://x.trycloudflare.com")
    worker = processes.add_parser("worker", help="run queued runs and reproductions")
    worker.add_argument("--poll", type=float, default=2.0, help="seconds between empty polls")
    publisher = processes.add_parser("publisher", help="publish check runs to GitHub")
    publisher.add_argument("--interval", type=float, default=5.0)
    reaper = processes.add_parser(
        "reaper", help="remove expired environments; needs no database or secret"
    )
    reaper.add_argument("--interval", type=float, default=60.0)
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    if args.process == "register-app":
        return register_app(args)
    stack = Stack.from_environment()
    if args.process == "migrate":
        with psycopg.connect(stack.secrets.database_url) as connection:
            applied = migrate(connection)
        log.info("applied migrations: %s", applied or "none pending")
    elif args.process == "api":
        uvicorn.run(stack.api(), host=args.host, port=args.port, proxy_headers=False)
    elif args.process == "webhook":
        uvicorn.run(stack.webhook(), host=args.host, port=args.port, proxy_headers=False)
    elif args.process == "webhook-url":
        url = args.url.rstrip("/")
        url = url if url.endswith("/github/webhook") else f"{url}/github/webhook"
        stack.github_app().set_webhook_url(url)
        log.info("the App's webhook now points at %s", url)
    elif args.process == "gateway":
        uvicorn.run(stack.gateway(), host=args.host, port=args.port, proxy_headers=False)
    elif args.process == "worker":
        worker_id = f"{socket.gethostname()}-{os.getpid()}"
        backend = stack.backend()
        runner = stack.worker(backend, worker_id=worker_id)
        reproducer = stack.reproducer(backend, worker_id=worker_id)
        swept = 0.0
        while True:
            if time.monotonic() - swept > 30:
                runner.sweep()
                swept = time.monotonic()
            if not runner.run_once() and not reproducer.run_once():
                time.sleep(args.poll)
    elif args.process == "reaper":
        backend, journal = stack.backend(), stack.journal()
        while True:
            for report in reap(backend, journal, datetime.now(UTC)):
                log.info("reaped run %s: %s", report.run_id, report.outcome.value)
            time.sleep(args.interval)
    else:
        checks = stack.publisher()
        while True:
            checks.publish_pending()
            time.sleep(args.interval)
    return 0


def register_app(args: argparse.Namespace) -> int:
    callback = f"http://127.0.0.1:{args.port}/callback"
    manifest = app_manifest(
        name=args.name,
        webhook_url=args.webhook_url,
        dashboard_origin=args.dashboard,
        redirect_url=callback,
        homepage="https://github.com/s2002kumar/DataProof",
    )
    registration = Registration(
        manifest,
        args.secrets_dir,
        owner=args.owner,
        api=httpx2.Client(base_url="https://api.github.com", timeout=30),
    )
    log.info("open http://127.0.0.1:%s/ in a browser signed in to GitHub", args.port)
    app = registration.serve(args.port)
    log.info("registered %s (App ID %s, client ID %s)", app.slug, app.app_id, app.client_id)
    log.info(
        "credentials written to %s; install the App on the demo repository only", args.secrets_dir
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
