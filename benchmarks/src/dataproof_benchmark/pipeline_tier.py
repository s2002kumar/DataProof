"""The pipeline tier: cases run end to end by the real worker on local Docker, with previews.

Each case becomes a pull request head in a throwaway repository (the sample app and every
protected oracle at the base), and the real ``Pipeline`` runs it under a worker: source staging,
policy, provisioning, migration, previews of both commits, verification (data contracts and the
API behaviour contracts through the previews), sealing, reporting and cleanup. The bindings the
case proposes are approved in its trusted configuration, as a reviewer's approval would.

For each run this records the verdict and every check's outcome, how long each stage took (from
the run's transition log), what each preview cost to build and serve, and whether cleanup left
anything behind. Timings vary between machines and runs; the outcomes must not.
"""

import itertools
import shutil
import subprocess
import uuid
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import docker
import psycopg

from commerce_scenario import PUBLISHED_DIGEST
from dataproof_benchmark.corpus import C1, C2, C3, C6_REFUNDS, C6_TOTALS, LEDGER_API, Case
from dataproof_benchmark.files import BINDINGS
from dataproof_benchmark.harness import REPOSITORY, databases
from dataproof_contracts import RunInputs, RunState, RunTrigger
from dataproof_control import RetryPolicy, RunResults, RunStore, migrate
from dataproof_runner import Worker, WorkerSettings
from dataproof_runner.artifacts import LocalArtifactStore
from dataproof_runner.baselines import CommerceBaseline
from dataproof_runner.local_docker import LocalDockerBackend
from dataproof_runner.pipeline import Pipeline, PipelineSettings
from dataproof_runner.project import ProjectConfig
from dataproof_verifier import parse_binding

SCENARIO = REPOSITORY / "scenarios" / "commerce"
LEDGER_WEB = SCENARIO / "ledger-app" / "web" / "lib" / "payments.ts"
PROJECT_REPOSITORY = "example-org/commerce-demo"
SETTINGS = WorkerSettings(
    lease=timedelta(minutes=10),
    heartbeat_interval=timedelta(seconds=15),
    review_ttl=timedelta(hours=1),
    retry=RetryPolicy(max_attempts=2, base_delay=timedelta(0), max_delay=timedelta(0)),
)


def sample(cases: Sequence[Case], *, clean: int = 2) -> list[Case]:
    """The first case of every fault family and the first clean controls, by case number."""
    chosen: dict[str, Case] = {}
    controls: list[Case] = []
    for case in sorted(cases, key=lambda c: c.case_id):
        if case.label.kind == "clean":
            if len(controls) < clean:
                controls.append(case)
        else:
            chosen.setdefault(case.label.family, case)
    return sorted([*chosen.values(), *controls], key=lambda c: c.case_id)


class Workspace:
    """A throwaway repository: the sample app and every oracle, then one head per case."""

    def __init__(self, path: Path) -> None:
        self.path = path
        path.mkdir(parents=True)
        self._git("init", "-q", "-b", "main")
        tracked = subprocess.run(  # noqa: S603 - fixed git binary
            ["git", "-C", str(REPOSITORY), "ls-files", "sample-app"],  # noqa: S607
            capture_output=True,
            check=True,
            text=True,
        ).stdout.split()
        files = {name: (REPOSITORY / name).read_text("utf-8") for name in tracked}
        for oracle in sorted((SCENARIO / "oracles").glob("*.toml")):
            files[f"dataproof/oracles/{oracle.name}"] = oracle.read_text("utf-8")
        self.base = self._commit(files, "Commerce sample app and its protected contracts")

    def _git(self, *arguments: str) -> str:
        return subprocess.run(  # noqa: S603 - fixed git binary
            [  # noqa: S607
                "git",
                "-c",
                "user.name=DataProof Benchmark",
                "-c",
                "user.email=benchmark@dataproof.invalid",
                "-c",
                "core.autocrlf=false",
                "-C",
                str(self.path),
                *arguments,
            ],
            capture_output=True,
            check=True,
            text=True,
        ).stdout.strip()

    def _commit(self, files: dict[str, str], message: str) -> str:
        for name, text in files.items():
            target = self.path / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(text, "utf-8", newline="\n")
        self._git("add", "-A")
        self._git("commit", "-q", "-m", message)
        return self._git("rev-parse", "HEAD")

    def head(self, case: Case) -> str:
        """The case's pull request head, on top of the base."""
        self._git("checkout", "-q", "--detach", self.base)
        self._git("clean", "-q", "-fdx")
        files = {
            f"sample-app/migrations/versions/{case.revision}_ledger_backfill.py": case.source(),
            "sample-app/app/payments.py": case.api_source or LEDGER_API.read_text("utf-8"),
            "sample-app/web/lib/payments.ts": LEDGER_WEB.read_text("utf-8"),
            **{
                f"dataproof/bindings/{b.name}": b.read_text("utf-8")
                for b in sorted(BINDINGS.glob("*.toml"))
            },
        }
        return self._commit(files, "Move legacy payment events into a per-order ledger")

    def binding_digests(self, head: str) -> frozenset[str]:
        digests = set()
        for binding in sorted(BINDINGS.glob("*.toml")):
            name = f"dataproof/bindings/{binding.name}"
            text = self._git("show", f"{head}:{name}")
            digests.add(parse_binding(text, source_path=name, head_sha=head)[1].digest())
        return frozenset(digests)


def _stage_seconds(store: RunStore, run_id: uuid.UUID) -> dict[str, float]:
    transitions = store.transitions(run_id)
    durations: dict[str, float] = {}
    for current, following in itertools.pairwise(transitions):
        state = current.to_state.value
        seconds = (following.at - current.at).total_seconds()
        durations[state] = round(durations.get(state, 0.0) + seconds, 1)
    return durations


def run_case(
    case: Case,
    *,
    workspace: Workspace,
    store: RunStore,
    results: RunResults,
    control_dsn: str,
    backend: LocalDockerBackend,
    artifacts: LocalArtifactStore,
    runtime_image: str,
) -> dict[str, Any]:
    head = workspace.head(case)
    project = ProjectConfig(
        repository=PROJECT_REPOSITORY,
        runtime_image=runtime_image,
        baseline="commerce-v1",
        approved_binding_digests=workspace.binding_digests(head),
        step_timeout_seconds=600,
    )
    pipeline = Pipeline(
        project=project,
        clone=workspace.path,
        backend=backend,
        artifacts=artifacts,
        results=results,
        baseline=CommerceBaseline(),
        settings=PipelineSettings(previews=True),
    )
    worker = Worker(
        store, pipeline.handlers(), pipeline.cleanup, worker_id="benchmark", settings=SETTINGS
    )
    inputs = RunInputs(
        repository=PROJECT_REPOSITORY,
        pull_request=int(case.case_id.split("-")[1]),
        base_sha=workspace.base,
        head_sha=head,
        config_digest=project.digest(),
        dataset_digest=PUBLISHED_DIGEST,
        engine_version="benchmark",
    )
    run_id = store.create_run(
        inputs,
        RunTrigger.MANUAL,
        actor="benchmark",
        run_ttl=timedelta(hours=1),
        retry=SETTINGS.retry,
    ).run.run_id
    started = datetime.now(UTC)
    worker.run_once()  # through review, where the run parks
    reviewed = store.get_run(run_id).state
    with psycopg.connect(control_dsn, autocommit=True) as conn:
        conn.execute(
            "UPDATE run_jobs SET available_at = statement_timestamp() WHERE run_id = %s",
            (run_id,),
        )
    worker.run_once()  # cleanup
    run = store.get_run(run_id)
    report = results.report(run_id)
    previews = results.previews(run_id)
    return {
        "case_id": case.case_id,
        "family": case.label.family,
        "kind": case.label.kind,
        "variant": case.label.variant,
        "expected_failing": sorted(case.label.failing),
        "state_at_review": reviewed.value,
        "final_state": run.state.value,
        "error_code": run.error_code.value if run.error_code else None,
        "verdict": report.verdict.value if report else None,
        "conclusion": report.conclusion.value if report else None,
        "outcomes": {c.result.check_id: c.result.outcome.value for c in results.checks(run_id)},
        "stage_seconds": _stage_seconds(store, run_id),
        "seconds_to_review": round((store.transitions(run_id)[-1].at - started).total_seconds(), 1)
        if reviewed is RunState.READY_FOR_REVIEW
        else None,
        "previews": {
            role.value: {
                "ready": record.ready,
                "build_seconds": record.build_seconds,
                "ready_seconds": record.ready_seconds,
                "peak_memory_mib": round(record.peak_memory_bytes / 2**20, 1),
                "cpu_seconds": record.cpu_seconds,
            }
            for role, record in previews.items()
        },
        "left_behind": len(backend.resources(run_id)),
    }


def run(
    admin_dsn: str, runtime_image: str, cases: Sequence[Case], workdir: Path
) -> list[dict[str, Any]]:
    """Run each case end to end; every container the tier created is removed afterwards."""
    if workdir.exists():
        shutil.rmtree(workdir)
    workdir.mkdir(parents=True)
    backend = LocalDockerBackend(scope=f"bench{uuid.uuid4().hex[:8]}", client=docker.from_env())
    try:
        with databases(admin_dsn) as new_database:
            control_dsn = new_database()
            with psycopg.connect(control_dsn) as conn:
                migrate(conn)
            store, results = RunStore(control_dsn), RunResults(control_dsn)
            workspace = Workspace(workdir / "repository")
            artifacts = LocalArtifactStore(workdir / "artifacts")
            return [
                run_case(
                    case,
                    workspace=workspace,
                    store=store,
                    results=results,
                    control_dsn=control_dsn,
                    backend=backend,
                    artifacts=artifacts,
                    runtime_image=runtime_image,
                )
                for case in cases
            ]
    finally:
        backend.reap_expired(datetime.max.replace(tzinfo=UTC))


DATA_CONTRACTS = (C1, C2, C3)
ALL_CONTRACTS = (C1, C2, C3, C6_TOTALS, C6_REFUNDS)


def labelled_contracts(case: Case) -> tuple[str, ...]:
    """The contracts a case's label speaks for.

    Database-tier cases were labelled for the data contracts (C1 to C3) alone. Their faults
    often show through the API contracts (C6) too, since the API serves the migrated data, but
    no label predicts that, so it is reported (``also_failed``) and not scored. API behaviour
    cases are labelled for every contract.
    """
    return ALL_CONTRACTS if case.api_source is not None else DATA_CONTRACTS


def _failing(result: dict[str, Any], contracts: Sequence[str]) -> list[str]:
    outcomes = result["outcomes"]
    return sorted(c for c in contracts if outcomes.get(c) in ("FAIL", "ERROR"))


def detected(result: dict[str, Any]) -> bool:
    return result["verdict"] == "BLOCKED" or any(
        o in ("FAIL", "ERROR") for o in result["outcomes"].values()
    )


def agrees(result: dict[str, Any]) -> bool:
    """Whether exactly the contracts the label expects to fail did, among those it speaks for."""
    return bool(_failing(result, result["labelled_contracts"]) == result["expected_failing"])


def also_failed(result: dict[str, Any]) -> list[str]:
    """Failing contracts outside the label's scope."""
    outside = [c for c in ALL_CONTRACTS if c not in result["labelled_contracts"]]
    return _failing(result, outside)
