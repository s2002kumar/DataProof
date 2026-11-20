"""The paid stages of the second AI evaluation, one at a time, each under a budget.

Started by ``benchmarks/launcher/Run-AiEval2.ps1``, which asks for the API key with its input
hidden and puts it in this process's environment alone. Nothing here prints, logs or stores
the key; whatever a stage prints is searched for it, and so is every file a stage wrote.

    python -m dataproof_benchmark.launcher status
    python -m dataproof_benchmark.launcher plan  <stage>
    python -m dataproof_benchmark.launcher run   <stage> --approve <usd>
    python -m dataproof_benchmark.launcher resume --approve <usd>
    python -m dataproof_benchmark.launcher abandon

What it holds to, and what the first evaluation's launcher did not:

* **Budgets are pools, and the launcher adds them up itself.** Each stage draws on a pool; a
  stage is given what its pool has left, never more than the total has left, and the harness
  starts an investigation only while its cap still fits in that.
* **A pilot first, at the same commit.** No stage of a role starts until a pilot of that role
  has passed its gate at the commit being run.
* **A held-out stage runs once.** A development stage may be attempted again after a fix,
  from its pool; nothing is ever started again by itself.
* **Every turn is on disk as it happens** (``evaluation.Journal``). A stage that died is
  taken up from its journal (``resume``: only what was never started is sent) or closed as
  it stands (``abandon``). Until then nothing else starts.
* **A stage's folder is sealed:** every file's SHA-256, chained to the stage before, and the
  files made read-only. ``status`` checks every seal.
* **The gate is code under test** (``gates``), and a stage that sent no request fails it.
* **The key and the model are checked before anything is billed:** the launcher asks the
  provider, at no cost, whether the key works and serves the pinned model.
* **A held-out stage is refused if its budget would not cover it** at what the development
  stage measured an investigation costs: running out part-way would spend the held-out set
  on a partial result.
"""

import argparse
import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from dataproof_benchmark import ablation, evaluation, gates

REPOSITORY = Path(__file__).resolve().parents[3]
BENCHMARKS = REPOSITORY / "benchmarks"
MODEL = f"anthropic:{ablation.MODEL}"
EFFORT = "medium"

POOLS: Mapping[str, Decimal] = {
    "development": Decimal("2.50"),
    "heldout": Decimal("5.00"),
    "old-arm": Decimal("2.00"),
    "analyst-development": Decimal("2.00"),
    "analyst-heldout": Decimal("2.00"),
}
"""The owner's limits (docs/AI_EVALUATION_V2.md): 13.50 USD in all."""
TOTAL = sum(POOLS.values(), Decimal(0))

PILOT_CASES = ("dev-101", "dev-109")
"""The development pilot: a case with one failed check and the one with the most."""
ANALYST_PILOT_CASES = ("dev-101", "dev-102")
"""The Change Analyst's pilot: a fault and a clean control of the development set."""
OLD_COMMIT = "fe75bce"
"""The commit whose investigator package is the old arm: the first evaluation's, published."""
FROZEN = (
    "benchmarks/src",
    "benchmarks/heldout",
    "benchmarks/launcher",
    "packages",
    "examples/subscriptions",
    "sample-app",
    "docs/AI_EVALUATION_V2.md",
)
"""What a stage's outcome depends on: none of it may differ from the commit being run."""
RESULTS = "stage-results.json"
TRANSCRIPTS = "stage-transcripts"
"""A stage's results and transcripts in its folder, as the harness names them."""
_KEY = re.compile(r"sk-ant-[A-Za-z0-9_\-]{8,}")
_KEY_VARIABLES = (
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_AUTH_TOKEN",
    "ANTHROPIC_LOG",
    "ANTHROPIC_BASE_URL",
    "ANTHROPIC_PROFILE",
)


@dataclass(frozen=True)
class Plan:
    name: str
    pool: str
    arguments: tuple[str, ...]
    """The harness's arguments, without what the launcher adds (budget, outputs, database)."""
    kind: str = "stage"
    role: str = "investigator"
    needs: tuple[str, ...] = ()
    """Stages whose latest attempt must have passed its gate."""
    pilot: str | None = None
    """The pilot that must have passed at the commit being run."""
    once: bool = False
    """A held-out stage: started once, ever."""
    most_usd: Decimal | None = None
    """The most this stage is given, if less than its pool."""
    old_arm: bool = False


PLANS: Mapping[str, Plan] = {
    plan.name: plan
    for plan in (
        Plan(
            "dev-pilot",
            "development",
            (
                "eval2-investigate",
                "--set",
                "dev",
                "--only",
                *PILOT_CASES,
                "--stop-after-unanswered",
                "1",
            ),
            kind="pilot",
            most_usd=Decimal("1.00"),
        ),
        Plan(
            "dev",
            "development",
            ("eval2-investigate", "--set", "dev", "--skip", *PILOT_CASES),
            pilot="dev-pilot",
        ),
        Plan(
            "heldout",
            "heldout",
            (
                "eval2-investigate",
                "--set",
                "heldout",
                "--reps",
                "3",
                "--stop-after-unanswered",
                "4",
            ),
            needs=("dev",),
            pilot="dev-pilot",
            once=True,
        ),
        Plan(
            "old-arm",
            "old-arm",
            ("eval2-investigate", "--set", "heldout", "--reps", "1"),
            needs=("heldout",),
            once=True,
            old_arm=True,
        ),
        Plan(
            "analyst-pilot",
            "analyst-development",
            (
                "eval2-analyze",
                "--set",
                "dev",
                "--only",
                *ANALYST_PILOT_CASES,
                "--stop-after-unanswered",
                "1",
            ),
            kind="pilot",
            role="analyst",
            most_usd=Decimal("1.00"),
        ),
        Plan(
            "analyst-dev",
            "analyst-development",
            ("eval2-analyze", "--set", "dev", "--skip", *ANALYST_PILOT_CASES),
            role="analyst",
            pilot="analyst-pilot",
        ),
        Plan(
            "analyst-heldout",
            "analyst-heldout",
            ("eval2-analyze", "--set", "heldout", "--reps", "2"),
            role="analyst",
            needs=("analyst-dev",),
            pilot="analyst-pilot",
            once=True,
        ),
    )
}


class Refused(Exception):  # noqa: N818 - an outcome the owner reads, not a program error
    """A stage may not start, and why."""


# Records -----------------------------------------------------------------------------------------


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _read(path: Path) -> dict[str, Any]:
    loaded: dict[str, Any] = json.loads(path.read_text("utf-8"))
    return loaded


def _write_once(path: Path, document: Mapping[str, Any]) -> None:
    with path.open("x", encoding="utf-8", newline="\n") as file:
        file.write(json.dumps(document, indent=2, default=str) + "\n")


@dataclass(frozen=True)
class Attempt:
    """One started stage: its folder under ``stages/``, numbered in the order they started."""

    folder: Path

    @property
    def started(self) -> dict[str, Any]:
        return _read(self.folder / "started.json")

    @property
    def name(self) -> str:
        return str(self.started["stage"])

    @property
    def sealed(self) -> dict[str, Any] | None:
        path = self.folder / "sealed.json"
        return _read(path) if path.is_file() else None

    @property
    def journal(self) -> Path:
        return self.folder / "journal"

    def _turns(self, path: Path) -> list[dict[str, Any]]:
        turns = []
        for line in path.read_text("utf-8").splitlines()[1:]:
            try:
                turns.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        return turns

    def recorded_usd(self) -> Decimal:
        """What the stage's journal says it spent, turn by turn, with every request that got
        no answer counted at the most it could have cost."""
        total = Decimal(0)
        for path in sorted(self.journal.glob("*.turns.jsonl")):
            for turn in self._turns(path):
                total += Decimal(str(turn.get("cost_usd", "0")))
                total += Decimal(str(turn.get("unconfirmed_usd", "0")))
        return total

    def open_investigation_usd(self) -> Decimal | None:
        """What the investigation that was running when the process ended had spent, or None
        if none was running."""
        for path in sorted(self.journal.glob("*.turns.jsonl")):
            if not path.with_name(path.name.replace(".turns.jsonl", ".run.json")).is_file():
                return sum(
                    (Decimal(str(t.get("cost_usd", "0"))) for t in self._turns(path)), Decimal(0)
                )
        return None

    def charged_usd(self) -> Decimal:
        """What the stage takes from its pool: what its seal records, which for a stage closed
        without finishing includes an allowance for a request whose answer was never seen."""
        sealed = self.sealed
        if sealed is None:
            return Decimal(str(self.started["budget_usd"]))  # unknown yet: the worst case
        return Decimal(str(sealed["charged_usd"]))

    def problems(self) -> list[str]:
        """What no longer matches the stage's seal; empty when it holds."""
        sealed = self.sealed
        if sealed is None:
            return [f"{self.folder.name}: not sealed"]
        found = []
        for name, digest in sealed["files"].items():
            path = self.folder / name
            if not path.is_file():
                found.append(f"{self.folder.name}/{name}: missing")
            elif _sha256(path) != digest:
                found.append(f"{self.folder.name}/{name}: changed since it was sealed")
        listed = {
            path.relative_to(self.folder).as_posix()
            for path in self.folder.rglob("*")
            if path.is_file() and path.name != "sealed.json"
        }
        found += [
            f"{self.folder.name}/{name}: added after the seal"
            for name in sorted(listed - set(sealed["files"]))
        ]
        return found


@dataclass(frozen=True)
class State:
    """Everything the launcher knows: the attempts on disk, in order. Nothing else is kept."""

    root: Path
    dry_run: bool = False

    @property
    def stages(self) -> Path:
        return self.root / "stages"

    def attempts(self) -> list[Attempt]:
        if not self.stages.is_dir():
            return []
        return [
            Attempt(folder)
            for folder in sorted(self.stages.iterdir())
            if (folder / "started.json").is_file()
        ]

    def of(self, name: str) -> list[Attempt]:
        return [attempt for attempt in self.attempts() if attempt.name == name]

    def open_attempt(self) -> Attempt | None:
        unsealed = [attempt for attempt in self.attempts() if attempt.sealed is None]
        return unsealed[0] if unsealed else None

    def charged(self, pool: str | None = None) -> Decimal:
        return sum(
            (
                attempt.charged_usd()
                for attempt in self.attempts()
                if pool is None or PLANS[attempt.name].pool == pool
            ),
            Decimal(0),
        )

    def budget(self, plan: Plan) -> Decimal:
        """What a stage may be given now: its pool's remainder, within the total's."""
        left = min(POOLS[plan.pool] - self.charged(plan.pool), TOTAL - self.charged())
        return min(left, plan.most_usd) if plan.most_usd is not None else left


# Preconditions -----------------------------------------------------------------------------------


def _git(*arguments: str) -> str:
    done = subprocess.run(  # noqa: S603 - fixed arguments, no shell
        ["git", "-C", str(REPOSITORY), *arguments],  # noqa: S607
        capture_output=True,
        text=True,
        check=False,
    )
    if done.returncode != 0:
        raise Refused(f"git {' '.join(arguments)} failed: {done.stderr.strip()}")
    return done.stdout.strip()


def commit(state: State, plan: Plan) -> str:
    """The commit a stage runs at, having checked what it depends on is exactly that commit.

    A held-out stage also needs the commit to be on the remote: its result is quoted, so the
    code that produced it must be public before the result exists.
    """
    head = _git("rev-parse", "HEAD")
    if state.dry_run:
        return head
    dirty = _git("status", "--porcelain", "--", *FROZEN)
    if dirty:
        raise Refused(f"what the stage depends on differs from the commit:\n{dirty}")
    if plan.once and not _git("branch", "--remotes", "--contains", head):
        raise Refused(f"{head[:12]} is on no remote branch: push it before a held-out stage")
    return head


def _passed(attempt: Attempt) -> bool:
    sealed = attempt.sealed
    return sealed is not None and bool(sealed["gate_pass"]) and not sealed.get("abandoned")


def check(state: State, plan: Plan) -> tuple[str, Decimal]:
    """Whether the stage may start: the commit it would run at and the budget it would get.
    Raises ``Refused`` with the reason otherwise. Sends nothing and writes nothing."""
    unsealed = state.open_attempt()
    if unsealed is not None:
        raise Refused(
            f"{unsealed.folder.name} was started and never closed. Take it up again "
            "(resume: only what was never started is sent) or close it as it stands (abandon)."
        )
    for attempt in state.attempts():
        broken = attempt.problems()
        if broken:
            raise Refused("a sealed stage's records have changed:\n  " + "\n  ".join(broken))
    head = commit(state, plan)
    if plan.once and state.of(plan.name):
        raise Refused(f"{plan.name} is a held-out stage and was started before: it runs once")
    for needed in plan.needs:
        previous = state.of(needed)
        if not previous or not _passed(previous[-1]):
            raise Refused(f"{plan.name} needs {needed} to have passed its gate first")
    if plan.pilot is not None:
        pilots = [a for a in state.of(plan.pilot) if a.started["commit"] == head]
        if not pilots or not _passed(pilots[-1]):
            raise Refused(
                f"{plan.name} needs {plan.pilot} to have passed at this commit ({head[:12]}): "
                "a pilot first, on the code being run"
            )
    budget = state.budget(plan)
    least = start_usd(plan)
    if budget < least:
        raise Refused(
            f"{plan.pool} has {budget} USD left, less than the {least} USD an investigation "
            "needs to start"
        )
    if plan.once:
        projected = projection(state, plan)
        if projected is not None and projected["needs_usd"] > budget:
            raise Refused(
                f"at what the development stage measured (an investigation costing "
                f"{projected['per_run_usd']} USD or less three times in four), {plan.name}'s "
                f"{projected['runs']} investigations need about {projected['needs_usd']} USD, "
                f"and it would be given {budget}. A held-out stage that runs out part-way is a "
                "partial result that cannot be repeated. Raising its budget is the owner's "
                "decision."
            )
    if plan.old_arm and not state.dry_run:
        old_checkout(state, make=False)
    return head, budget


def start_usd(plan: Plan) -> Decimal:
    """What a stage must have left to start one more investigation."""
    if plan.role == "investigator":
        return evaluation.INVESTIGATION_START_USD
    return evaluation.ANALYSIS_START_USD


def cap_usd(plan: Plan) -> Decimal:
    """The most one investigation of the stage may cost."""
    if plan.role == "investigator":
        return evaluation.INVESTIGATION_CAP_USD
    return evaluation.ANALYST_CASE_CAP_USD


EXPECTED_RUNS: Mapping[str, int] = {"heldout": 33, "old-arm": 11, "analyst-heldout": 28}
"""How many investigations a held-out stage runs: its cases times its repeats."""


def projection(state: State, plan: Plan) -> dict[str, Any] | None:
    """What a held-out stage would need, from the costs its role's development stages
    measured: the third quartile of an investigation's cost for every run but the last, which
    must still find the stage's start amount left. None before any development stage ran."""
    import statistics

    role = [p.name for p in PLANS.values() if p.role == plan.role and not p.once]
    costs: list[Decimal] = []
    for attempt in state.attempts():
        if attempt.name not in role or not _passed(attempt):
            continue
        for path in sorted(attempt.journal.glob("*.run.json")):
            run = _read(path)
            record = run.get("investigation") or run.get("analysis") or {}
            if "usage" in record:
                costs.append(Decimal(str(record["usage"]["cost_usd"])))
    if not costs or state.dry_run:
        return None
    quartiles = statistics.quantiles(costs, n=4) if len(costs) > 1 else [costs[0]] * 3
    per_run = Decimal(str(quartiles[2])).quantize(Decimal("0.0001"))
    runs = EXPECTED_RUNS[plan.name]
    return {
        "runs": runs,
        "per_run_usd": per_run,
        "needs_usd": (per_run * (runs - 1) + start_usd(plan)).quantize(Decimal("0.01")),
    }


def preflight(key: str, model: str, client: Any = None) -> None:
    """Whether the key works and the pinned model is served to it, asked at no cost (the
    models endpoint is not billed). A paid request is never the first thing that fails."""
    import anthropic

    client = client or anthropic.Anthropic(api_key=key, max_retries=0, timeout=30)
    try:
        found = client.models.retrieve(model)
    except anthropic.AuthenticationError as exc:
        raise Refused("the key was not accepted: nothing was sent and nothing spent") from exc
    except anthropic.NotFoundError as exc:
        raise Refused(f"{model} is not served to this key: nothing was spent") from exc
    except anthropic.APIError as exc:
        raise Refused(f"the provider could not be asked ({type(exc).__name__})") from exc
    # The endpoint may name a dated snapshot of the model; a turn served by anything but the
    # pinned name still fails the stage's gate.
    if found.id != model and not found.id.startswith(f"{model}-"):
        raise Refused(f"asked for {model}, the provider named {found.id}")


def old_checkout(state: State, *, make: bool) -> Path:
    """The old investigator's package, from a detached checkout of the first evaluation's
    commit, which must be unmodified."""
    folder = state.root / "old-arm-checkout"
    if not folder.is_dir():
        if not make:
            return folder
        _git("worktree", "add", "--detach", str(folder), OLD_COMMIT)
    done = subprocess.run(  # noqa: S603
        ["git", "-C", str(folder), "status", "--porcelain"],  # noqa: S607
        capture_output=True,
        text=True,
        check=True,
    )
    head = subprocess.run(  # noqa: S603
        ["git", "-C", str(folder), "rev-parse", "HEAD"],  # noqa: S607
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    if done.stdout.strip() or not head.startswith(OLD_COMMIT):
        raise Refused(f"{folder} is not an unmodified checkout of {OLD_COMMIT}")
    return folder / "packages" / "investigator" / "src"


# Running a stage ---------------------------------------------------------------------------------


def _command(plan: Plan, state: State, folder: Path, budget: Decimal, dsn: str) -> list[str]:
    arguments = [sys.executable, "-m", "dataproof_benchmark", *plan.arguments]
    arguments += ["--effort", EFFORT, "--workdir", str(state.root / "work")]
    arguments += ["--out", str(folder / RESULTS), "--journal", str(folder / "journal")]
    if plan.role == "investigator":
        arguments += ["--database", dsn]
    if not state.dry_run:
        arguments += ["--live", "--budget-usd", str(budget)]
    return arguments


def _environment(plan: Plan, state: State, key: str | None) -> dict[str, str]:
    environment = {k: v for k, v in os.environ.items() if k not in _KEY_VARIABLES}
    environment |= {"PYTHONUNBUFFERED": "1", "PYTHONIOENCODING": "utf-8"}
    if key is not None:
        environment["ANTHROPIC_API_KEY"] = key
    if plan.old_arm and not state.dry_run:
        # A dry run sends nothing, so it has no old investigator to compare: it runs this one.
        environment["PYTHONPATH"] = str(old_checkout(state, make=True))
    return environment


def _stream(
    command: Sequence[str], environment: Mapping[str, str], log: Path, key: str | None
) -> int:
    """Run a stage, showing what it prints and keeping it, with the key never in either."""
    with log.open("a", encoding="utf-8", newline="\n") as kept:
        kept.write(f"[launcher] {datetime.now(UTC).isoformat()} started\n")
        process = subprocess.Popen(  # noqa: S603 - our own interpreter and module
            list(command),
            cwd=BENCHMARKS,
            env=dict(environment),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        if process.stdout is None:  # pragma: no cover - PIPE was asked for
            raise RuntimeError("the stage's output could not be read")
        for line in process.stdout:
            shown = line.replace(key, "[REDACTED]") if key else line
            shown = _KEY.sub("[REDACTED]", shown)
            sys.stdout.write(shown)
            sys.stdout.flush()
            kept.write(shown)
            kept.flush()
        code = process.wait()
        kept.write(f"[launcher] {datetime.now(UTC).isoformat()} exit {code}\n")
    return code


def _leaks(folder: Path, key: str | None) -> list[str]:
    """Files of the stage that hold the key, or anything shaped like one."""
    found = []
    for path in sorted(folder.rglob("*")):
        if not path.is_file():
            continue
        text = path.read_text("utf-8", errors="replace")
        if (key and key in text) or _KEY.search(text):
            found.append(path.relative_to(folder).as_posix())
    return found


def _transcripts(folder: Path) -> dict[str, list[dict[str, Any]]]:
    kept = {}
    for path in sorted((folder / TRANSCRIPTS).glob("*.json")):
        kept[path.stem] = json.loads(path.read_text("utf-8"))
    return kept


def _gate(plan: Plan, state: State, folder: Path, budget: Decimal) -> dict[str, Any]:
    results = folder / RESULTS
    if not results.is_file():
        return {"pass": False, "kind": plan.kind, "reasons": ["the stage wrote no results"]}
    document = _read(results)
    live = not state.dry_run
    versions = {}
    if plan.role == "investigator" and not plan.old_arm:
        from dataproof_investigator import PROMPT_VERSION, TOOL_VERSION

        versions = {"prompt_version": PROMPT_VERSION, "tool_version": TOOL_VERSION}
    if plan.old_arm and live:
        versions = {"prompt_version": "failure-investigator/1", "tool_version": "tools/1"}
    return gates.gate(
        document,
        _transcripts(folder),
        kind=plan.kind,
        cap_usd=budget,
        live=live,
        model=MODEL,
        versions=versions,
        need_cache_read=True,
        complete=plan.kind == "pilot" or plan.once,
    )


def seal(
    state: State, attempt: Attempt, verdict: Mapping[str, Any], *, abandoned: bool
) -> dict[str, Any]:
    """Close a stage: its files' hashes, chained to the stage before it, then read-only."""
    before = [a for a in state.attempts() if a.folder.name < attempt.folder.name and a.sealed]
    previous = _sha256(before[-1].folder / "sealed.json") if before else None
    recorded = attempt.recorded_usd()
    plan = PLANS[attempt.name]
    allowance = Decimal(0)
    spent_by_open = attempt.open_investigation_usd() if abandoned else None
    if spent_by_open is not None:
        # A request may have been answered, and billed, after the last turn on disk. The loop
        # sends a turn only if its worst case fits in what the investigation has left, so
        # what that request could have cost is at most the cap less what was spent.
        allowance = max(Decimal(0), cap_usd(plan) - spent_by_open)
    files = {
        path.relative_to(attempt.folder).as_posix(): _sha256(path)
        for path in sorted(attempt.folder.rglob("*"))
        if path.is_file()
    }
    sealed = {
        "stage": attempt.name,
        "folder": attempt.folder.name,
        "sealed_at": datetime.now(UTC).isoformat(),
        "gate_pass": bool(verdict.get("pass")) and not abandoned,
        "abandoned": abandoned,
        "recorded_usd": str(recorded),
        "in_flight_allowance_usd": str(allowance),
        "charged_usd": str(recorded + allowance),
        "previous_seal_sha256": previous,
        "files": files,
    }
    _write_once(attempt.folder / "sealed.json", sealed)
    for path in attempt.folder.rglob("*"):
        if path.is_file():
            path.chmod(stat.S_IREAD)
    return sealed


def _close(state: State, attempt: Attempt, plan: Plan, code: int, key: str | None) -> int:
    """After a stage's process has ended: look for the key, gate, seal, and say what happened."""
    leaks = _leaks(attempt.folder, key)
    if leaks:
        print(f"THE KEY APPEARS IN {attempt.folder}: {', '.join(leaks)}", file=sys.stderr)
        print(
            "Publish nothing from this folder, and revoke the key in the Console.", file=sys.stderr
        )
        return 9
    if not (attempt.folder / RESULTS).is_file():
        print(
            f"\n{plan.name} ended with exit code {code} and wrote no results. Its journal holds "
            f"what it spent so far: {attempt.recorded_usd()} USD. Nothing else can start until "
            "it is taken up again (resume) or closed as it stands (abandon).",
            file=sys.stderr,
        )
        return 2
    budget = Decimal(str(attempt.started["budget_usd"]))
    verdict = _gate(plan, state, attempt.folder, budget)
    _write_once(attempt.folder / "gate.json", verdict | {"exit_code": code})
    sealed = seal(state, attempt, verdict, abandoned=False)
    print(json.dumps(verdict, indent=2, default=str))
    print(f"\n{plan.name}: spent {sealed['recorded_usd']} USD of the {budget} USD it was given.")
    if not verdict["pass"]:
        print(
            f"{plan.name} DID NOT PASS its gate. Nothing that needs it will start.", file=sys.stderr
        )
        return 3
    print(f"{plan.name} passed its gate.")
    return 0


def run(state: State, name: str, approve: Decimal | None, dsn: str, key: str | None) -> int:
    plan = PLANS[name]
    head, budget = check(state, plan)
    if not state.dry_run:
        if key is None:
            raise Refused("a paid stage needs the API key, from the launcher's hidden prompt")
        if approve != budget:
            raise Refused(f"this stage would be given {budget} USD; --approve said {approve}")
        preflight(key, ablation.MODEL)
    number = len(state.attempts()) + 1
    folder = state.stages / f"{number:02d}-{name}"
    folder.mkdir(parents=True)
    command = _command(plan, state, folder, budget, dsn)
    frozen = {
        path: _sha256(REPOSITORY / path)
        for path in ("docs/AI_EVALUATION_V2.md", "benchmarks/heldout/labels.json")
    }
    _write_once(
        folder / "started.json",
        {
            "stage": name,
            "attempt": len(state.of(name)) + 1,
            "started_at": datetime.now(UTC).isoformat(),
            "commit": head,
            "dry_run": state.dry_run,
            "pool": plan.pool,
            "budget_usd": str(budget),
            "arguments": [a for a in command[1:] if a != dsn],
            "protocol_sha256": frozen,
        },
    )
    print(f"\n=== {name}: started, with {budget} USD of the {plan.pool} pool ===", flush=True)
    code = _stream(command, _environment(plan, state, key), folder / "stage.log", key)
    return _close(state, Attempt(folder), plan, code, key)


def resume(state: State, approve: Decimal | None, dsn: str, key: str | None) -> int:
    attempt = state.open_attempt()
    if attempt is None:
        raise Refused("no stage is open: there is nothing to take up again")
    plan = PLANS[attempt.name]
    started = attempt.started
    budget = Decimal(str(started["budget_usd"]))
    if (attempt.folder / RESULTS).is_file():
        return _close(state, attempt, plan, 0, key)  # it ended; only its gate and seal are owed
    if not state.dry_run:
        if key is None:
            raise Refused("a paid stage needs the API key, from the launcher's hidden prompt")
        if _git("rev-parse", "HEAD") != started["commit"] or _git(
            "status", "--porcelain", "--", *FROZEN
        ):
            raise Refused(
                f"the code is no longer the commit {started['commit'][:12]} it started at"
            )
        left = budget - attempt.recorded_usd()
        if approve != left:
            raise Refused(f"{left} USD of this stage's budget is unspent; --approve said {approve}")
        preflight(key, ablation.MODEL)
    print(
        f"\n=== {attempt.name}: taken up again; what its journal holds is not sent ===", flush=True
    )
    command = _command(plan, state, attempt.folder, budget, dsn)
    code = _stream(command, _environment(plan, state, key), attempt.folder / "stage.log", key)
    return _close(state, attempt, plan, code, key)


def abandon(state: State) -> int:
    attempt = state.open_attempt()
    if attempt is None:
        raise Refused("no stage is open: there is nothing to close")
    reason = {"pass": False, "reasons": ["closed as it stood, without finishing"]}
    sealed = seal(state, attempt, reason, abandoned=True)
    print(
        f"{attempt.folder.name} is closed as it stood. Charged to its pool: "
        f"{sealed['charged_usd']} USD ({sealed['recorded_usd']} recorded, and "
        f"{sealed['in_flight_allowance_usd']} for a request whose answer was never seen)."
    )
    return 0


# Reporting ---------------------------------------------------------------------------------------


def status(state: State) -> tuple[str, list[str]]:
    """The spend so far, stage by stage and pool by pool, and anything wrong with a seal."""
    lines = ["Stage                      Gate     Given   Recorded  Charged  Commit"]
    problems: list[str] = []
    previous: str | None = None
    for attempt in state.attempts():
        started, sealed = attempt.started, attempt.sealed
        if sealed is None:
            gate, charged = "OPEN", attempt.charged_usd()
        else:
            gate = (
                "closed" if sealed["abandoned"] else ("passed" if sealed["gate_pass"] else "FAILED")
            )
            charged = Decimal(str(sealed["charged_usd"]))
            problems += attempt.problems()
            if sealed["previous_seal_sha256"] != previous:
                problems.append(f"{attempt.folder.name}: its seal does not follow the one before")
            previous = _sha256(attempt.folder / "sealed.json")
        lines.append(
            f"{attempt.folder.name:<26} {gate:<8} {started['budget_usd']:>6}  "
            f"{attempt.recorded_usd():>9.4f}  {charged:>7.4f}  {started['commit'][:10]}"
        )
    lines.append("")
    lines.append("Pool                   Limit  Charged     Left")
    for pool, limit in POOLS.items():
        used = state.charged(pool)
        lines.append(f"{pool:<21} {limit:>6}  {used:>7.4f}  {limit - used:>7.4f}")
    used = state.charged()
    lines.append(f"{'total':<21} {TOTAL:>6}  {used:>7.4f}  {TOTAL - used:>7.4f}")
    lines.append("")
    lines.append(
        "Recorded is the sum of every turn's cost in the stage's journal; a gate recomputes it "
        "from token counts. Compare the total with the Console's cost page for the same day."
    )
    return "\n".join(lines), problems


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--state", type=Path, required=True, help="where stage records are kept")
    parser.add_argument("--dry-run", action="store_true", help="scripted models, no key, no spend")
    parser.add_argument("--database", default="postgresql://postgres@127.0.0.1:5436/postgres")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("status")
    planned = commands.add_parser("plan")
    planned.add_argument("stage", choices=[*sorted(PLANS), "resume"])
    started = commands.add_parser("run")
    started.add_argument("stage", choices=sorted(PLANS))
    started.add_argument("--approve", type=Decimal)
    again = commands.add_parser("resume")
    again.add_argument("--approve", type=Decimal)
    commands.add_parser("abandon")
    commands.add_parser("reset", help="dry run only: remove the dry run's records")
    args = parser.parse_args(argv)
    state = State(args.state, dry_run=args.dry_run)
    # The key is taken out of this process's environment at once; only a stage's own process
    # is given it.
    key = os.environ.pop("ANTHROPIC_API_KEY", None) or None
    try:
        if args.command == "status":
            text, problems = status(state)
            print(text)
            if problems:
                print("\n".join(["", "RECORDS HAVE CHANGED:", *problems]), file=sys.stderr)
                return 3
            return 0
        if args.command == "plan" and args.stage == "resume":
            attempt = state.open_attempt()
            if attempt is None:
                raise Refused("no stage is open: there is nothing to take up again")
            left = Decimal(str(attempt.started["budget_usd"])) - attempt.recorded_usd()
            print(f"stage:   {attempt.name}, taken up again; what its journal holds is not sent")
            print(f"commit:  {attempt.started['commit']}")
            print(f"budget:  {left} USD at most, what is left of its budget")
            print(f"approve: {left}")
            return 0
        if args.command == "plan":
            plan = PLANS[args.stage]
            head, budget = check(state, plan)
            print(f"stage:   {plan.name} ({plan.kind}, {plan.role})")
            print(f"commit:  {head}")
            print(f"runs:    dataproof_benchmark {' '.join(plan.arguments)}")
            print(f"budget:  {budget} USD at most, from the {plan.pool} pool")
            projected = projection(state, plan) if plan.once else None
            if projected is not None:
                print(
                    f"expects: about {projected['needs_usd']} USD for {projected['runs']} "
                    f"investigations, at {projected['per_run_usd']} USD each (development)"
                )
            print(f"approve: {budget}")
            return 0
        if args.command == "run":
            return run(state, args.stage, args.approve, args.database, key)
        if args.command == "resume":
            return resume(state, args.approve, args.database, key)
        if args.command == "abandon":
            return abandon(state)
        if not args.dry_run:
            raise Refused("only a dry run's records can be removed")
        if state.root.is_dir():
            for path in state.root.rglob("*"):
                if path.is_file():
                    path.chmod(stat.S_IREAD | stat.S_IWRITE)
            shutil.rmtree(state.root)
        return 0
    except Refused as refused:
        print(f"REFUSED: {refused}", file=sys.stderr)
        return 3


if __name__ == "__main__":
    raise SystemExit(main())
