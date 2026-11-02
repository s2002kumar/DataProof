"""The independent reaper: expired environments go even when the control plane is down (#11).

The real ``python -m dataproof_api reaper`` process runs with the deployment file and nothing
else: no control database URL and no other secret, so any attempt to reach the control plane
would stop it. It must still remove an expired run's environments, journal them, leave live
runs alone, and leave the run's own cleanup to find them already gone.
"""

import json
import os
import subprocess
import sys
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest

from dataproof_contracts import CleanupOutcome, Environment
from dataproof_runner.local_docker import LocalDockerBackend
from dataproof_runner.pipeline import destroy_run

pytestmark = [pytest.mark.integration, pytest.mark.docker]

EXAMPLE = Path(__file__).parents[2] / "services" / "api" / "deployment.example.toml"


def _deployment(tmp_path: Path, scope: str) -> Path:
    text = EXAMPLE.read_text("utf-8")
    for old, new in (
        ('data_dir = "/var/lib/dataproof"', f'data_dir = "{(tmp_path / "data").as_posix()}"'),
        ('scope = "dataproof"', f'scope = "{scope}"'),
    ):
        assert old in text
        text = text.replace(old, new)
    path = tmp_path / "deployment.toml"
    path.write_text(text, "utf-8")
    return path


def test_the_reaper_removes_expired_environments_without_the_control_plane(
    backend: LocalDockerBackend, tmp_path: Path
) -> None:
    expired = backend.provision(
        uuid4(), Environment.CANDIDATE, expires_at=datetime.now(UTC) - timedelta(seconds=1)
    )
    live = backend.provision(
        uuid4(), Environment.CANDIDATE, expires_at=datetime.now(UTC) + timedelta(minutes=30)
    )
    environment = {
        name: value
        for name, value in os.environ.items()
        if not name.startswith(("DATAPROOF_", "GITHUB_", "ANTHROPIC_"))
    }
    environment["DATAPROOF_CONFIG"] = str(_deployment(tmp_path, backend.scope))
    journal = tmp_path / "data" / "reaper.jsonl"

    log = tmp_path / "reaper.log"
    with log.open("wb") as output:
        process = subprocess.Popen(
            [sys.executable, "-m", "dataproof_api", "reaper", "--interval", "0.5"],
            env=environment,
            stdout=output,
            stderr=subprocess.STDOUT,
        )
        try:
            deadline = time.monotonic() + 60
            while not journal.exists() and time.monotonic() < deadline:
                assert process.poll() is None, log.read_text("utf-8", errors="replace")
                time.sleep(0.5)
            time.sleep(1.5)  # a few more sweeps: nothing else may be removed
            assert process.poll() is None, "the reaper keeps running without a database"
        finally:
            process.terminate()
            process.wait(timeout=10)

    entries = [json.loads(line) for line in journal.read_text("utf-8").splitlines()]
    assert [entry["run_id"] for entry in entries] == [str(expired.run_id)], "reaped exactly once"
    assert entries[0]["outcome"] == CleanupOutcome.SUCCEEDED.value
    assert len(entries[0]["removed"]) == 3, "the database and both networks"
    assert backend.resources(expired.run_id) == []
    assert len(backend.resources(live.run_id)) == 3, "a live run is untouched"

    record = destroy_run(backend, expired.run_id)
    assert record["cleanup"] == CleanupOutcome.ALREADY_GONE.value, "the run's own cleanup"
