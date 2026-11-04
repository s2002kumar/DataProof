"""``python -m dataproof_action run``: the entry point the composite action calls."""

import argparse
import os
import sys
from pathlib import Path

from dataproof_action.config import DEFAULT_CONFIG_PATH
from dataproof_action.run import Inputs, run
from dataproof_runner.local_docker import POSTGRES_IMAGE


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="dataproof-action")
    commands = parser.add_subparsers(dest="command", required=True)
    runner = commands.add_parser("run", help="verify the pull request this job runs for")
    runner.add_argument(
        "--workspace", type=Path, default=Path(os.environ.get("GITHUB_WORKSPACE", "."))
    )
    runner.add_argument(
        "--config", default=DEFAULT_CONFIG_PATH, help="configuration path on the base branch"
    )
    runner.add_argument("--report-dir", type=Path, default=Path("dataproof-report"))
    runner.add_argument(
        "--postgres-image", default=POSTGRES_IMAGE, help="digest-pinned PostgreSQL image"
    )
    arguments = parser.parse_args(argv)
    inputs = Inputs(
        workspace=arguments.workspace.resolve(),
        config_path=arguments.config,
        report_dir=arguments.report_dir.resolve(),
        postgres_image=arguments.postgres_image,
        environ=dict(os.environ),
    )
    return run(inputs)


if __name__ == "__main__":
    sys.exit(main())
