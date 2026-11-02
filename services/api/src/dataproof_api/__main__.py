"""``python -m dataproof_api <process>``: run one process of a deployment (``deployment``)."""

import argparse
import logging
import os
import socket
import time
from collections.abc import Sequence

import psycopg
import uvicorn

from dataproof_api.deployment import Stack
from dataproof_control import migrate

log = logging.getLogger("dataproof")


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m dataproof_api",
        description="Run one DataProof process. Configuration: DATAPROOF_CONFIG and secrets "
        "in the environment (services/api/README.md).",
    )
    processes = parser.add_subparsers(dest="process", required=True)
    processes.add_parser("migrate", help="apply the control database's migrations")
    for name, port in (("api", 8000), ("gateway", 8080)):
        served = processes.add_parser(name, help=f"serve the {name}")
        served.add_argument("--host", default="127.0.0.1")
        served.add_argument("--port", type=int, default=port)
    worker = processes.add_parser("worker", help="claim and run queued runs")
    worker.add_argument("--poll", type=float, default=2.0, help="seconds between empty polls")
    publisher = processes.add_parser("publisher", help="publish check runs to GitHub")
    publisher.add_argument("--interval", type=float, default=5.0)
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    stack = Stack.from_environment()
    if args.process == "migrate":
        with psycopg.connect(stack.secrets.database_url) as connection:
            applied = migrate(connection)
        log.info("applied migrations: %s", applied or "none pending")
    elif args.process == "api":
        uvicorn.run(stack.api(), host=args.host, port=args.port, proxy_headers=False)
    elif args.process == "gateway":
        uvicorn.run(stack.gateway(), host=args.host, port=args.port, proxy_headers=False)
    elif args.process == "worker":
        runner = stack.worker(worker_id=f"{socket.gethostname()}-{os.getpid()}")
        swept = 0.0
        while True:
            if time.monotonic() - swept > 30:
                runner.sweep()
                swept = time.monotonic()
            if not runner.run_once():
                time.sleep(args.poll)
    else:
        checks = stack.publisher()
        while True:
            checks.publish_pending()
            time.sleep(args.interval)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
