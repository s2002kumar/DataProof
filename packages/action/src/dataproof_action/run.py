"""One run of the Action: trusted inputs, two disposable databases, the verifier, the report.

The order is fixed, and every exit is explicit:

1. The event must be ``pull_request`` (never ``pull_request_target``).
2. The base branch's tip is fetched; the configuration, fixture, oracles, requirements and
   binding approvals are read from it. The pull request's bindings are read from its head.
3. The migration image is built from those trusted inputs.
4. Reference and candidate environments are provisioned on the local Docker backend (the same
   backend and isolation the GitHub App uses): the base branch's migrations run from the base
   commit's archive in each, then the fixture is loaded, and both databases must digest alike.
5. The pull request's migrations run in the candidate from the head commit's archive, in a step
   container with no credentials, no network but its own database, and a timeout.
6. The verifier evaluates every oracle, with the pull request's bindings approved only if the
   base branch lists their digests. The verdict is derived exactly as the App derives it.
7. Both databases are dumped as evidence, the environments are destroyed, and the job gets a
   summary, annotations, a JSON report and an exit code that is zero only for a green verdict.

An infrastructure or configuration failure (no Docker, the base branch's own migrations fail,
the fixture does not load) is not a verdict: it exits with ``EXIT_CODES["ERROR"]`` and says why.
"""

import hashlib
import json
import logging
import shutil
import tempfile
import time
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path, PurePosixPath
from typing import Any
from uuid import UUID, uuid4

import psycopg

from dataproof_action.config import (
    ActionConfig,
    ConfigError,
    binding_approval_digest,
    parse_config,
)
from dataproof_action.event import EventError, PullRequestEvent, load_event
from dataproof_action.migrator import BuildError, build_image, remove_image
from dataproof_action.output import (
    annotations,
    job_summary,
    one_line,
    workflow_command,
    write_outputs,
    write_summary,
)
from dataproof_action.sources import Repository, SourceError
from dataproof_contracts import (
    CheckOutcome,
    CheckResult,
    DataContract,
    Environment,
    Investigation,
    Mismatch,
    NotRunReason,
    ReportVerdict,
    TextValue,
    github_check_conclusion,
)
from dataproof_investigator import diagnose
from dataproof_report import escape_cell
from dataproof_runner.backend import EnvironmentHandle, StagedSource, StepResult
from dataproof_runner.baselines import database_digest, user_tables
from dataproof_runner.local_docker import POSTGRES_IMAGE, BackendError, LocalDockerBackend
from dataproof_verifier import (
    OracleFile,
    QueryLimits,
    Verification,
    assemble_contract,
    derive_verdict,
    parse_binding,
    parse_oracle,
    type_check,
    verify_contract,
)

log = logging.getLogger(__name__)

VERSION = "0.1.0"
MIGRATION_CHECK = "migration.execute"
MIGRATION_CONTRACT = "dataproof.migration_executes"
RERUN_CHECK = "migration.rerun"
RERUN_CONTRACT = "dataproof.migration_rerun_is_noop"
TYPES_PREFIX = "types."
EXIT_SOURCE_DIGEST_MISMATCH = 65
ENVIRONMENT_TTL = timedelta(hours=2)
MAX_OUTPUT = 16 * 1024
MAX_ERROR_CHARS = 1500
EXIT_CODES: Mapping[str, int] = {
    "CHECKED": 0,
    "CHECKED_WITH_WARNINGS": 0,
    "BLOCKED": 1,
    "INCOMPLETE": 2,
    "NOT_EXECUTED": 3,
    "ERROR": 4,
}
"""The job fails for everything but a green verdict. ``ERROR`` is not a verdict: the run could
not be made, and nothing about the migration is established."""


@dataclass(frozen=True)
class Inputs:
    workspace: Path
    config_path: str
    report_dir: Path
    postgres_image: str = POSTGRES_IMAGE
    environ: Mapping[str, str] = field(default_factory=dict)

    @property
    def summary_path(self) -> Path | None:
        value = self.environ.get("GITHUB_STEP_SUMMARY")
        return Path(value) if value else None

    @property
    def output_path(self) -> Path | None:
        value = self.environ.get("GITHUB_OUTPUT")
        return Path(value) if value else None


class RunError(RuntimeError):
    """The run could not be made; the message says why, for the job log and summary."""


@dataclass
class _Trusted:
    """Everything read from the base branch's tip, with digests."""

    base_sha: str
    config: ActionConfig
    config_text: str
    fixture_sql: str
    fixture_sha256: str
    oracles: list[tuple[str, OracleFile, Any]]
    oracle_digests: dict[str, str]
    requirements: str | None


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _read_trusted(repository: Repository, event: PullRequestEvent, config_path: str) -> _Trusted:
    base_sha = repository.resolve_base(event.base_ref)
    config_text = repository.read_text(base_sha, config_path)
    if config_text is None:
        raise ConfigError(
            f"{config_path} does not exist on {event.base_ref} ({base_sha[:12]}). The Action "
            "reads its configuration from the base branch, never from the pull request."
        )
    config = parse_config(config_text)
    fixture_sql = repository.read_text(base_sha, config.fixture.sql)
    if fixture_sql is None:
        raise ConfigError(f"the fixture {config.fixture.sql} does not exist on {event.base_ref}")
    requirements = None
    if config.alembic.requirements is not None:
        requirements = repository.read_text(base_sha, config.alembic.requirements)
        if requirements is None:
            raise ConfigError(
                f"the requirements file {config.alembic.requirements} does not exist on "
                f"{event.base_ref}"
            )
    oracles = []
    digests = {}
    for path in repository.list_files(base_sha, config.contracts.oracle_dir):
        if not path.endswith(".toml"):
            continue
        text = repository.read_text(base_sha, path)
        if text is None:
            continue
        try:
            spec, oracle = parse_oracle(text, source_path=path, base_sha=base_sha)
        except ValueError as error:
            raise ConfigError(f"the oracle {path} is invalid: {error}") from error
        oracles.append((path, spec, oracle))
        digests[path] = _sha256(text)
    if not oracles:
        raise ConfigError(
            f"no oracle (*.toml) exists under {config.contracts.oracle_dir} on {event.base_ref}; "
            "with nothing to verify, the run would prove nothing"
        )
    return _Trusted(
        base_sha=base_sha,
        config=config,
        config_text=config_text,
        fixture_sql=fixture_sql,
        fixture_sha256=_sha256(fixture_sql),
        oracles=oracles,
        oracle_digests=digests,
        requirements=requirements,
    )


def _read_bindings(
    repository: Repository, head_sha: str, binding_dir: str
) -> dict[str, tuple[str, Any]]:
    """The pull request's proposed bindings by contract ID: untrusted, validated on parse."""
    bindings: dict[str, tuple[str, Any]] = {}
    for path in repository.list_files(head_sha, binding_dir):
        if not path.endswith(".toml"):
            continue
        text = repository.read_text(head_sha, path)
        if text is None:
            continue
        try:
            contract_id, binding = parse_binding(text, source_path=path, head_sha=head_sha)
        except ValueError as error:
            raise RunError(f"the proposed binding {path} is invalid: {error}") from error
        if contract_id in bindings:
            raise RunError(f"two bindings are proposed for {contract_id}")
        bindings[contract_id] = (path, binding)
    return bindings


def _step_entry(
    step_id: str, role: Environment, source: StagedSource, result: StepResult
) -> dict[str, Any]:
    output = result.output
    if len(output) > MAX_OUTPUT:
        output = "[truncated]\n" + output[-MAX_OUTPUT:]
    return {
        "step_id": step_id,
        "environment": role.value,
        "source_sha256": source.sha256,
        "commit_sha": source.commit_sha,
        "exit_code": result.exit_code,
        "timed_out": result.timed_out,
        "duration_ms": int(result.duration_seconds * 1000),
        "output": output,
    }


def _rerun_check(rerun: tuple[StepResult, str, str] | None, dataset_digest: str) -> CheckResult:
    """C7 as a check: the second ``upgrade head`` succeeded and changed nothing."""
    common: dict[str, Any] = {
        "check_id": RERUN_CHECK,
        "contract_id": RERUN_CONTRACT,
        "contract_version": 1,
        "required": True,
        "dataset_digest": dataset_digest,
    }
    if rerun is None:
        return CheckResult(
            **common,
            outcome=CheckOutcome.NOT_RUN,
            not_run_reason=NotRunReason.PREREQUISITE_FAILED,
        )
    result, before, after = rerun
    evidence = (f"{RERUN_CHECK}:CANDIDATE:step",)
    if result.succeeded and before == after:
        return CheckResult(
            **common,
            outcome=CheckOutcome.PASS,
            evidence_ids=evidence,
            elapsed_ms=int(result.duration_seconds * 1000),
        )
    changed = (
        []
        if before == after
        else [
            Mismatch(
                key=(TextValue(value="database_digest"),),
                column="after_rerun",
                expected=TextValue(value=before),
                observed=TextValue(value=after),
            )
        ]
    )
    return CheckResult(
        **common,
        outcome=CheckOutcome.FAIL,
        mismatch_count=len(changed),
        mismatch_examples=tuple(changed),
        evidence_ids=evidence,
        elapsed_ms=int(result.duration_seconds * 1000),
    )


def _coverage_row(check: CheckResult) -> dict[str, Any]:
    reason = check.not_run_reason or check.error_code
    return {
        "check_id": check.check_id,
        "required": check.required,
        "outcome": check.outcome.value,
        "reason": reason.value if reason else None,
        "mismatch_count": check.mismatch_count,
        "expected_key_count": check.expected_key_count,
        "observed_key_count": check.observed_key_count,
        "examples": [m.model_dump(mode="json") for m in check.mismatch_examples[:5]],
    }


def _investigation_entry(investigation: Investigation) -> dict[str, Any]:
    return {
        "advisory": True,
        "role": investigation.role.value,
        "model": investigation.model,
        "status": investigation.status.value,
        "hypotheses": [
            {
                "statement": h.statement,
                "cited_evidence_ids": list(h.cited_evidence_ids),
                "confidence": h.confidence.value,
            }
            for h in investigation.hypotheses
        ],
        "suggested_action": investigation.suggested_action,
        "caveat": investigation.caveat,
        "failure": investigation.failure,
    }


def _migration_files(changed: list[str]) -> list[str]:
    """Changed Python files that look like Alembic revisions (a ``versions`` directory)."""
    return [p for p in changed if p.endswith(".py") and "versions" in PurePosixPath(p).parts]


def _trusted_files_changed(changed: list[str], trusted: _Trusted, config_path: str) -> list[str]:
    files = set(trusted.config.trusted_files(config_path))
    oracle_dir = trusted.config.contracts.oracle_dir
    return sorted(p for p in changed if p in files or p.startswith(oracle_dir))


class _Run:
    def __init__(
        self, inputs: Inputs, backend: LocalDockerBackend, image: str, config: ActionConfig
    ) -> None:
        self.inputs = inputs
        self.backend = backend
        self.image = image
        self.config = config
        self.run_id: UUID = uuid4()
        self.steps: list[dict[str, Any]] = []
        self.handles: dict[Environment, EnvironmentHandle] = {}

    def _migrate(
        self, handle: EnvironmentHandle, source: StagedSource, step_id: str, timeout: int
    ) -> StepResult:
        result = self.backend.run_step(
            handle,
            image=self.image,
            source=source,
            arguments=["migrate", "--config", self.config.alembic.config, "--target", "head"],
            timeout=timedelta(seconds=timeout),
            variables=self.config.alembic.environment,
        )
        self.steps.append(_step_entry(step_id, handle.role, source, result))
        if result.exit_code == EXIT_SOURCE_DIGEST_MISMATCH:
            raise RunError("the step refused its source: digest mismatch (no code ran)")
        return result

    def baseline(self, role: Environment, base: StagedSource, trusted: _Trusted) -> str:
        """Provision one role at the base schema with the fixture; its database digest."""
        handle = self.backend.provision(
            self.run_id, role, expires_at=datetime.now(UTC) + ENVIRONMENT_TTL
        )
        self.handles[role] = handle
        result = self._migrate(
            handle,
            base,
            f"{role.value.lower()}.base-migration",
            trusted.config.alembic.timeout_seconds,
        )
        if not result.succeeded:
            raise RunError(
                f"the base branch's own migrations failed in the {role.value.lower()} "
                f"environment (exit {result.exit_code}"
                f"{', timed out' if result.timed_out else ''}). Nothing about the pull request "
                f"is established. Output:\n{result.output[-1500:]}"
            )
        access = handle.database
        try:
            with psycopg.connect(access.conninfo(access.owner)) as connection:
                connection.execute(trusted.fixture_sql)
                connection.commit()
        except psycopg.Error as error:
            raise RunError(
                f"the fixture {trusted.config.fixture.sql} did not load into the "
                f"{role.value.lower()} database: {str(error).splitlines()[0]}"
            ) from error
        with psycopg.connect(access.conninfo(access.reader)) as connection:
            return database_digest(connection, user_tables(connection))

    def rerun(self, source: StagedSource, timeout: int) -> tuple[StepResult, str, str]:
        """C7: run ``alembic upgrade head`` once more on the migrated candidate.

        A completed revision is not applied again, so the step must succeed and leave every
        table byte for byte as it was. Work done outside the revision mechanism (in ``env.py``,
        or by a revision Alembic did not record) shows up as a changed digest.
        """
        handle = self.handles[Environment.CANDIDATE]
        access = handle.database
        with psycopg.connect(access.conninfo(access.reader)) as connection:
            before = database_digest(connection, user_tables(connection))
        result = self._migrate(handle, source, "candidate.migration-rerun", timeout)
        with psycopg.connect(access.conninfo(access.reader)) as connection:
            after = database_digest(connection, user_tables(connection))
        return result, before, after

    def verify(
        self,
        contracts: list[DataContract],
        migration: StepResult,
        rerun: tuple[StepResult, str, str] | None,
        dataset_digest: str,
    ) -> tuple[list[CheckResult], list[Verification]]:
        checks = [
            CheckResult(
                check_id=MIGRATION_CHECK,
                contract_id=MIGRATION_CONTRACT,
                contract_version=1,
                required=True,
                outcome=CheckOutcome.PASS if migration.succeeded else CheckOutcome.FAIL,
                evidence_ids=(f"{MIGRATION_CHECK}:CANDIDATE:step",),
                dataset_digest=dataset_digest,
                elapsed_ms=int(migration.duration_seconds * 1000),
            ),
            _rerun_check(rerun, dataset_digest),
        ]
        verifications: list[Verification] = []
        reference, candidate = (
            self.handles[Environment.REFERENCE],
            self.handles[Environment.CANDIDATE],
        )
        with (
            psycopg.connect(
                reference.database.conninfo(reference.database.reader), autocommit=True
            ) as ref,
            psycopg.connect(
                candidate.database.conninfo(candidate.database.reader), autocommit=True
            ) as cand,
        ):
            for contract in contracts:
                common: dict[str, Any] = {
                    "check_id": contract.contract_id,
                    "contract_id": contract.contract_id,
                    "contract_version": contract.version,
                    "required": contract.required,
                    "dataset_digest": dataset_digest,
                }
                types_common = {**common, "check_id": TYPES_PREFIX + contract.contract_id}
                if not migration.succeeded:
                    for fields in (common, types_common):
                        checks.append(
                            CheckResult(
                                **fields,
                                outcome=CheckOutcome.NOT_RUN,
                                not_run_reason=NotRunReason.PREREQUISITE_FAILED,
                            )
                        )
                    continue
                if contract.oracle.api_route is not None:
                    for fields in (common, types_common):
                        checks.append(
                            CheckResult(
                                **fields,
                                outcome=CheckOutcome.NOT_RUN,
                                not_run_reason=NotRunReason.UNSUPPORTED,
                            )
                        )
                    continue
                verification = verify_contract(contract, ref, cand, dataset_digest, QueryLimits())
                verifications.append(verification)
                checks.append(verification.check)
                checks.append(type_check(contract, verification))
        return checks, verifications

    def snapshots(self) -> dict[str, dict[str, Any]]:
        """``pg_dump`` of both databases into the report directory, by trusted code."""
        result = {}
        for role, handle in self.handles.items():
            try:
                dump = self.backend.dump_database(handle)
            except BackendError as error:
                log.warning("no %s snapshot: %s", role.value, error)
                continue
            path = self.inputs.report_dir / f"{role.value.lower()}.dump"
            path.write_bytes(dump)
            result[role.value] = {
                "sha256": hashlib.sha256(dump).hexdigest(),
                "bytes": len(dump),
                "path": path.name,
                "restore_verified": False,
            }
        return result


def _not_executed(
    inputs: Inputs, event: PullRequestEvent, trusted: _Trusted, reason: str
) -> dict[str, Any]:
    verdict = ReportVerdict.NOT_EXECUTED
    return {
        **_header(inputs, event, trusted, None),
        "verdict": verdict.value,
        "conclusion": github_check_conclusion(verdict).value,
        "exit_code": EXIT_CODES[verdict.value],
        "not_executed_reason": reason,
        "coverage": [],
        "steps": [],
        "investigations": [],
        "bindings_awaiting_review": [],
    }


def _header(
    inputs: Inputs, event: PullRequestEvent, trusted: _Trusted, image: str | None
) -> dict[str, Any]:
    return {
        "dataproof_action": VERSION,
        "pull_request": {
            "repository": event.repository,
            "number": event.number,
            "url": event.url,
            "head_sha": event.head_sha,
            "head_repository": event.head_repository,
            "from_fork": event.from_fork,
            "base_ref": event.base_ref,
            "base_sha_at_event": event.base_sha,
            "base_sha": trusted.base_sha,
        },
        "configuration": {
            "path": inputs.config_path,
            "digest": trusted.config.digest(),
            "fixture_sha256": trusted.fixture_sha256,
            "oracles": trusted.oracle_digests,
            "approved_bindings": list(trusted.config.contracts.approved_bindings),
            "migration_image": image or "",
            "postgres_image": inputs.postgres_image,
            "backend": LocalDockerBackend.name,
        },
    }


def _error_summary(inputs: Inputs, message: str) -> str:
    # The message can quote text the pull request controls (an invalid binding's keys, a
    # contract ID), so it is escaped like any untrusted value.
    return (
        "# DataProof\n\n## Not verified (`ERROR`)\n\n"
        "The run could not be made, so nothing about this pull request's migration is "
        "established, and this job is not green.\n\n"
        f"**Why:** {escape_cell(message, MAX_ERROR_CHARS)}\n\n"
        f"**Next:** fix the cause and re-run. Configuration: `{inputs.config_path}` on the base "
        "branch.\n"
    )


def run(inputs: Inputs) -> int:
    """The Action's whole run; returns the job's exit code and never raises for a verdict."""
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    started = time.monotonic()
    inputs.report_dir.mkdir(parents=True, exist_ok=True)
    try:
        return _run(inputs, started)
    except (EventError, ConfigError, SourceError, BuildError, RunError, BackendError) as error:
        message = str(error)
        line = one_line(message)
        log.error("DataProof: not verified: %s", line)
        write_summary(inputs.summary_path, _error_summary(inputs, message))
        # The report is data, not a page or a log: it keeps the message as it was.
        (inputs.report_dir / "report.json").write_text(
            json.dumps(
                {"dataproof_action": VERSION, "verdict": None, "error": message, "exit_code": 4},
                indent=2,
            ),
            "utf-8",
        )
        write_outputs(inputs.output_path, {"verdict": "ERROR", "exit-code": "4"})
        print(workflow_command("error", line, title="DataProof not verified"))
        return EXIT_CODES["ERROR"]


def _run(inputs: Inputs, started: float) -> int:
    event = load_event(inputs.environ)
    repository = Repository(inputs.workspace)
    trusted = _read_trusted(repository, event, inputs.config_path)
    repository.ensure_commit(event.head_sha)
    config = trusted.config
    log.info(
        "DataProof: pull request #%s, head %s against %s at %s",
        event.number,
        event.head_sha[:12],
        event.base_ref,
        trusted.base_sha[:12],
    )
    if event.from_fork and config.policy.forks == "refuse":
        return _finish(inputs, _not_executed(inputs, event, trusted, "FORK_PULL_REQUEST"))

    changed = repository.changed_paths(trusted.base_sha, event.head_sha)
    bindings = _read_bindings(repository, event.head_sha, config.contracts.binding_dir)
    contracts = []
    approval_digests: dict[str, str] = {}
    for _, spec, oracle in trusted.oracles:
        binding = bindings[spec.contract_id][1] if spec.contract_id in bindings else None
        approved: set[str] = set()
        rejected: set[str] = set()
        if binding is not None:
            # The base branch approves a binding by its content digest; the contract model
            # binds the approval to the binding's own digest, which includes the commit.
            content = binding_approval_digest(spec.contract_id, binding)
            approval_digests[spec.contract_id] = content
            if content in config.contracts.approved_bindings:
                approved.add(binding.digest())
            elif content in config.contracts.rejected_bindings:
                rejected.add(binding.digest())
        contracts.append(
            assemble_contract(
                spec, oracle, binding, approved_digests=approved, rejected_digests=rejected
            )
        )
    unmatched = sorted(set(bindings) - {spec.contract_id for _, spec, _ in trusted.oracles})
    if unmatched:
        log.warning(
            "bindings proposed for unknown contracts are ignored: %s", one_line(repr(unmatched))
        )

    context = Path(tempfile.mkdtemp(prefix="dataproof-migrator-"))
    image: str | None = None
    backend = LocalDockerBackend(
        scope=f"gha{uuid4().hex[:10]}", postgres_image=inputs.postgres_image
    )
    this_run: _Run | None = None
    try:
        log.info("DataProof: building the migration image from the base branch's inputs")
        image = build_image(context, requirements=trusted.requirements)
        this_run = _Run(inputs, backend, image, config)
        base = repository.archive(trusted.base_sha)
        head = repository.archive(event.head_sha)
        digests = {}
        for role in (Environment.REFERENCE, Environment.CANDIDATE):
            log.info("DataProof: provisioning the %s at the base schema", role.value.lower())
            digests[role] = this_run.baseline(role, base, trusted)
        if len(set(digests.values())) != 1:
            raise RunError(
                "the reference and candidate databases differ after loading the same fixture, "
                "so the fixture is not deterministic (BASELINE_DIVERGED)"
            )
        log.info("DataProof: running the pull request's migrations in the candidate")
        migration = this_run._migrate(
            this_run.handles[Environment.CANDIDATE],
            head,
            "candidate.migration",
            config.alembic.timeout_seconds,
        )
        rerun = None
        if migration.succeeded:
            log.info("DataProof: running the migrations once more; nothing may change")
            rerun = this_run.rerun(head, config.alembic.timeout_seconds)
        log.info("DataProof: verifying %d contracts", len(contracts))
        checks, verifications = this_run.verify(contracts, migration, rerun, trusted.fixture_sha256)
        verdict = derive_verdict(checks)
        investigation = diagnose(this_run.run_id, {c.check_id: c for c in checks})
        snapshots = this_run.snapshots()
    finally:
        try:
            removed = backend.destroy(this_run.run_id if this_run else uuid4())
            log.info(
                "DataProof: cleanup %s (%d resources)", removed.outcome.value, len(removed.removed)
            )
        finally:
            if image:
                remove_image(image)
            shutil.rmtree(context, ignore_errors=True)

    assert this_run is not None  # noqa: S101 - set before anything above could succeed
    pending = []
    by_id = {c.contract_id: c for c in contracts}
    for verification in verifications:
        contract = by_id[verification.check.contract_id]
        binding = contract.candidate_binding
        if binding is None or verification.check.not_run_reason not in (
            NotRunReason.AWAITING_BINDING_REVIEW,
            NotRunReason.BINDING_REJECTED,
        ):
            continue
        analysis = verification.binding_analysis
        pending.append(
            {
                "contract_id": contract.contract_id,
                "source_path": binding.source_path,
                "digest": approval_digests[contract.contract_id],
                "status": contract.binding_review.value,
                "problem": verification.binding_problem,
                "relations": [
                    {
                        "name": r.name,
                        "provenance": r.provenance.value,
                        "through_views": list(r.through_views),
                        "flagged": r.flagged,
                    }
                    for r in (analysis.relations if analysis else ())
                ],
                "functions": list(analysis.functions) if analysis else [],
            }
        )
    evidence_queries = [
        {
            "evidence_id": e.evidence_id,
            "environment": e.environment.value,
            "sql_sha256": e.sql_sha256,
            "result_sha256": e.result_sha256,
            "row_count": e.row_count,
        }
        for v in verifications
        for e in v.evidence
    ]
    manifest = hashlib.sha256(
        json.dumps(
            {"queries": evidence_queries, "snapshots": snapshots, "steps": this_run.steps},
            sort_keys=True,
            default=str,
        ).encode()
    ).hexdigest()
    report: dict[str, Any] = {
        **_header(inputs, event, trusted, image),
        "verdict": verdict.value,
        "conclusion": github_check_conclusion(verdict).value,
        "exit_code": EXIT_CODES[verdict.value],
        "database_digest": digests[Environment.REFERENCE],
        "changed_paths": changed,
        "migration_files_changed": _migration_files(changed),
        "trusted_files_changed": _trusted_files_changed(changed, trusted, inputs.config_path),
        "bindings": {
            contract_id: {
                "source_path": path,
                "approval_digest": approval_digests.get(contract_id),
                "digest": binding.digest(),
            }
            for contract_id, (path, binding) in bindings.items()
        },
        "steps": this_run.steps,
        "coverage": [_coverage_row(c) for c in checks],
        "checks": [c.model_dump(mode="json") for c in checks],
        "bindings_awaiting_review": pending,
        "investigations": [_investigation_entry(investigation)],
        "evidence": {
            "manifest_sha256": manifest,
            "queries": evidence_queries,
            "snapshots": snapshots,
        },
        "elapsed_seconds": round(time.monotonic() - started, 1),
    }
    return _finish(inputs, report)


def _finish(inputs: Inputs, report: dict[str, Any]) -> int:
    (inputs.report_dir / "report.json").write_text(
        json.dumps(report, indent=2, sort_keys=True, default=str), "utf-8"
    )
    summary = job_summary(report)
    write_summary(inputs.summary_path, summary)
    for line in annotations(report):
        print(line)
    exit_code = int(report["exit_code"])
    write_outputs(
        inputs.output_path,
        {
            "verdict": str(report["verdict"]),
            "exit-code": str(exit_code),
            "report": str(inputs.report_dir / "report.json"),
        },
    )
    log.info("DataProof: %s (exit %d)", report["verdict"], exit_code)
    return exit_code
