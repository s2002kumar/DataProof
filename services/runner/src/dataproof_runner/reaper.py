"""The independent reaper: expired environments are removed even when the control plane is down.

Every environment carries its run, role and expiry as labels on the backend's own resources, so
the reaper needs nothing but the backend: not the control database, the API or a worker
(THREAT_MODEL, abandoned environments; #11). What it removes is appended to a local journal, one
JSON object per line. It never writes a run's results: a run's verdict and evidence are
write-once, and when its worker cleans up later the environments are simply already gone.
"""

import json
from datetime import datetime
from pathlib import Path

from dataproof_runner.backend import CleanupReport, ExecutionBackend


def reap(backend: ExecutionBackend, journal: Path, now: datetime) -> list[CleanupReport]:
    """Remove every environment whose expiry has passed; journal what was removed."""
    reports = backend.reap_expired(now)
    if reports:
        journal.parent.mkdir(parents=True, exist_ok=True)
        with journal.open("a", encoding="utf-8") as entries:
            for report in reports:
                entry = {
                    "at": now.isoformat(),
                    "run_id": str(report.run_id),
                    "outcome": report.outcome.value,
                    "removed": list(report.removed),
                }
                entries.write(json.dumps(entry) + "\n")
    return reports
