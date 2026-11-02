"""The reaper's journal: one JSON line per removed run, and nothing when nothing expired."""

import json
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from dataproof_contracts import CleanupOutcome
from dataproof_runner.backend import CleanupReport
from dataproof_runner.reaper import reap


class _Backend:
    def __init__(self, *batches: list[CleanupReport]) -> None:
        self._batches = list(batches)
        self.asked: list[datetime] = []

    def reap_expired(self, now: datetime) -> list[CleanupReport]:
        self.asked.append(now)
        return self._batches.pop(0) if self._batches else []


def test_each_removal_is_appended_as_one_json_line(tmp_path: Path) -> None:
    first, second = uuid4(), uuid4()
    backend = _Backend(
        [CleanupReport(first, CleanupOutcome.SUCCEEDED, ("db", "int", "ctl"))],
        [],
        [CleanupReport(second, CleanupOutcome.SUCCEEDED, ("db",))],
    )
    journal = tmp_path / "data" / "reaper.jsonl"
    now = datetime(2026, 11, 2, 12, tzinfo=UTC)

    for _ in range(3):
        reap(backend, journal, now)  # type: ignore[arg-type]

    entries = [json.loads(line) for line in journal.read_text("utf-8").splitlines()]
    assert entries == [
        {
            "at": now.isoformat(),
            "run_id": str(first),
            "outcome": "SUCCEEDED",
            "removed": ["db", "int", "ctl"],
        },
        {"at": now.isoformat(), "run_id": str(second), "outcome": "SUCCEEDED", "removed": ["db"]},
    ]
    assert backend.asked == [now, now, now]


def test_nothing_expired_writes_nothing(tmp_path: Path) -> None:
    journal = tmp_path / "reaper.jsonl"
    assert reap(_Backend(), journal, datetime.now(UTC)) == []  # type: ignore[arg-type]
    assert not journal.exists()
