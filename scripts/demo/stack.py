"""Run DataProof's local stack for the live demo; Ctrl-C stops every process.

    python scripts/demo/stack.py --env <secrets.env> [--tunnel]

The deployment file is ``DATAPROOF_CONFIG`` and the secrets come from the environment, plus the
``--env`` file that ``register-app`` wrote. This script:

1. applies the control database's migrations;
2. builds the dashboard against the API's address and serves it on ``127.0.0.1:3000``;
3. starts the API (``:8000``), the webhook-only intake (``:8001``), the preview gateway
   (``:8080``), a worker and the check-run publisher, all on the loopback;
4. with ``--tunnel``, starts a Cloudflare quick tunnel to the webhook-only port alone, and points
   the GitHub App's webhook at it. Nothing else is reachable from the internet.

Logs are written to ``--logs`` (default ``.dataproof-demo/logs``), one file per process.
"""

import argparse
import os
import re
import shutil
import subprocess
import sys
import time
from collections.abc import Sequence
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
DASHBOARD = REPO / "services" / "dashboard"
PROCESSES = {
    "api": ["api", "--port", "8000"],
    "webhook": ["webhook", "--port", "8001"],
    "gateway": ["gateway", "--port", "8080"],
    "worker": ["worker"],
    "publisher": ["publisher"],
}
TUNNEL_URL = re.compile(r"https://[a-z0-9-]+\.trycloudflare\.com")


def environment(env_file: Path | None) -> dict[str, str]:
    values = dict(os.environ)
    if env_file is not None:
        for line in env_file.read_text("utf-8").splitlines():
            name, separator, value = line.partition("=")
            if separator and name.strip() and not name.startswith("#"):
                values[name.strip()] = value.strip()
    return values


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--env", type=Path, help="the secrets file register-app wrote")
    parser.add_argument(
        "--tunnel", action="store_true", help="expose the webhook through cloudflared"
    )
    parser.add_argument("--logs", type=Path, default=REPO / ".dataproof-demo" / "logs")
    args = parser.parse_args(argv)
    env = environment(args.env)
    for required in ("DATAPROOF_CONFIG", "DATAPROOF_DATABASE_URL"):
        if not env.get(required):
            raise SystemExit(f"set {required}")
    args.logs.mkdir(parents=True, exist_ok=True)
    python = [sys.executable, "-m", "dataproof_api"]

    subprocess.run([*python, "migrate"], env=env, check=True)  # noqa: S603
    pnpm = shutil.which("pnpm")
    node = shutil.which("node")
    if pnpm is None or node is None:
        raise SystemExit("pnpm and node are needed to serve the dashboard")
    dashboard_env = {**env, "DATAPROOF_API_ORIGIN": "http://127.0.0.1:8000"}
    subprocess.run(  # noqa: S603
        [pnpm, "--filter", "@dataproof/dashboard", "build"], cwd=REPO, env=dashboard_env, check=True
    )

    running: dict[str, subprocess.Popen[bytes]] = {}
    try:
        for name, arguments in PROCESSES.items():
            log = (args.logs / f"{name}.log").open("wb")
            running[name] = subprocess.Popen(  # noqa: S603
                [*python, *arguments], env=env, stdout=log, stderr=subprocess.STDOUT
            )
        log = (args.logs / "dashboard.log").open("wb")
        running["dashboard"] = subprocess.Popen(  # noqa: S603
            [node, "node_modules/next/dist/bin/next", "start", "-H", "127.0.0.1", "-p", "3000"],
            cwd=DASHBOARD,
            env=dashboard_env,
            stdout=log,
            stderr=subprocess.STDOUT,
        )
        print("dashboard: http://127.0.0.1:3000  (logs in", args.logs, ")")
        if args.tunnel:
            print("tunnel:", start_tunnel(running, args.logs, env, python))
        while all(process.poll() is None for process in running.values()):
            time.sleep(1)
        stopped = [name for name, process in running.items() if process.poll() is not None]
        print("stopped unexpectedly:", ", ".join(stopped), "- see the logs")
        return 1
    except KeyboardInterrupt:
        return 0
    finally:
        for process in running.values():
            process.terminate()
        for process in running.values():
            try:
                process.wait(timeout=20)
            except subprocess.TimeoutExpired:
                process.kill()


def start_tunnel(
    running: dict[str, subprocess.Popen[bytes]],
    logs: Path,
    env: dict[str, str],
    python: list[str],
) -> str:
    """A quick tunnel to the webhook-only port; the App's webhook follows it."""
    cloudflared = shutil.which("cloudflared")
    if cloudflared is None:
        raise SystemExit("install cloudflared for --tunnel (winget install Cloudflare.cloudflared)")
    path = logs / "tunnel.log"
    log = path.open("wb")
    running["tunnel"] = subprocess.Popen(  # noqa: S603
        [cloudflared, "tunnel", "--no-autoupdate", "--url", "http://127.0.0.1:8001"],
        stdout=log,
        stderr=subprocess.STDOUT,
    )
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        found = TUNNEL_URL.search(path.read_text("utf-8", errors="replace"))
        if found:
            subprocess.run([*python, "webhook-url", found.group(0)], env=env, check=True)  # noqa: S603
            return found.group(0)
        time.sleep(1)
    raise SystemExit("cloudflared did not report a tunnel address; see tunnel.log")


if __name__ == "__main__":
    sys.exit(main())
