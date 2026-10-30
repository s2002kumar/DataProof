"""Review run identity, inputs and lifecycle record (docs/ARCHITECTURE.md, ``ReviewRun``)."""

from typing import Self
from uuid import UUID

from pydantic import AwareDatetime, PositiveInt, model_validator

from dataproof_contracts._base import (
    Contract,
    GitSha,
    NonEmptyText,
    RepositoryName,
    Sha256Hex,
    sha256_digest,
)
from dataproof_contracts.enums import (
    DERIVED_RUN_TRIGGERS,
    ErrorCode,
    NotExecutedReason,
    RunState,
    RunTrigger,
)


class RunInputs(Contract):
    """Everything a run is bound to. Its digest is the run's idempotency key.

    Old results never authorize new inputs: any change here (a new head SHA, an approved
    binding that changes the config digest, a new fixture) produces a different digest.
    """

    repository: RepositoryName
    pull_request: PositiveInt | None
    base_sha: GitSha
    head_sha: GitSha
    config_digest: Sha256Hex
    dataset_digest: Sha256Hex
    engine_version: NonEmptyText

    def digest(self) -> str:
        return sha256_digest(self)


class WorkerLease(Contract):
    """Persisted supervisor lease; the fencing token rejects writes from a stale worker."""

    worker_id: NonEmptyText
    fencing_token: PositiveInt
    acquired_at: AwareDatetime
    heartbeat_at: AwareDatetime
    expires_at: AwareDatetime

    @model_validator(mode="after")
    def _ordered_times(self) -> Self:
        if not self.acquired_at <= self.heartbeat_at < self.expires_at:
            raise ValueError("lease times must satisfy acquired_at <= heartbeat_at < expires_at")
        return self


class ReviewRun(Contract):
    run_id: UUID
    inputs: RunInputs
    input_digest: Sha256Hex
    trigger: RunTrigger
    parent_run_id: UUID | None = None
    state: RunState
    error_code: ErrorCode | None = None
    not_executed_reason: NotExecutedReason | None = None
    lease: WorkerLease | None = None
    isolation_id: NonEmptyText | None = None
    created_at: AwareDatetime
    updated_at: AwareDatetime

    @model_validator(mode="after")
    def _consistent(self) -> Self:
        if self.input_digest != self.inputs.digest():
            raise ValueError("input_digest does not match the canonical digest of inputs")
        derived = self.trigger in DERIVED_RUN_TRIGGERS
        if derived != (self.parent_run_id is not None):
            raise ValueError(
                "parent_run_id is required for binding_approval and correction_replay runs "
                "and forbidden otherwise"
            )
        if self.parent_run_id == self.run_id:
            raise ValueError("a run cannot be its own parent")
        if (self.state is RunState.NOT_EXECUTED) != (self.not_executed_reason is not None):
            raise ValueError("not_executed_reason is required exactly when state is NOT_EXECUTED")
        if self.updated_at < self.created_at:
            raise ValueError("updated_at precedes created_at")
        return self
