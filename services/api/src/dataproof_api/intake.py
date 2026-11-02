"""GitHub webhook intake: verified pull request events become runs (ARCHITECTURE, connector).

Intake trusts nothing in an event beyond what its signature proves, which is that GitHub sent
it. The repository must be one the operator configured, the event must come from the GitHub
App installation configured for it, and the pull request must target that repository. A run
is then pinned to the exact base and head SHAs in the event and to the operator's trusted
configuration, never to anything the pull request supplies. Runs are idempotent on their
inputs, so a redelivered event finds the run it already created. A fork's pull request
becomes a run that ``ANALYZING`` refuses (``NOT_EXECUTED: FORK_PULL_REQUEST``), so it is
reported, not silently dropped. Closing a pull request cancels its open runs.
"""

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any
from uuid import UUID

from pydantic import ValidationError

from dataproof_api.webhooks import PullRequestEvent
from dataproof_contracts import RunInputs, RunTrigger
from dataproof_control import (
    Delivery,
    DeliveryOutcome,
    GitHubRecords,
    PullRequestHead,
    RetryPolicy,
    RunStore,
)
from dataproof_runner.project import ProjectConfig

#: Pull request actions that put a head up for review.
RUN_ACTIONS = frozenset({"opened", "reopened", "synchronize", "ready_for_review"})


@dataclass(frozen=True)
class Registration:
    """A repository the operator configured: the trusted configuration its runs start under,
    and the only GitHub App installation whose events are accepted for it."""

    project: ProjectConfig
    installation_id: int
    dataset_digest: str
    engine_version: str


@dataclass(frozen=True)
class IntakeResult:
    outcome: DeliveryOutcome
    run_id: UUID | None = None
    detail: Mapping[str, Any] = field(default_factory=dict)
    duplicate: bool = False
    """The delivery had been received before; this is what intake did then."""


class Intake:
    def __init__(
        self,
        store: RunStore,
        github: GitHubRecords,
        registrations: Mapping[str, Registration],
        *,
        run_ttl: timedelta,
        retry: RetryPolicy,
    ) -> None:
        for repository, registration in registrations.items():
            if registration.project.repository != repository:
                raise ValueError(f"the registration for {repository} configures another one")
        self.store = store
        self.github = github
        self.registrations = dict(registrations)
        self.run_ttl = run_ttl
        self.retry = retry

    def receive(self, delivery_id: str, event: str, payload: Mapping[str, Any]) -> IntakeResult:
        """Handle one delivery whose signature has been verified."""
        earlier = self.github.delivery(delivery_id)
        if earlier is not None:
            return _result(earlier, duplicate=True)
        delivery = self._handle(delivery_id, event, payload)
        if self.github.record_delivery(delivery):
            return _result(delivery, duplicate=False)
        # The same delivery arrived twice at once; the outcome recorded first stands. Runs are
        # idempotent on their inputs, so both attempts found the same run.
        recorded = self.github.delivery(delivery_id)
        if recorded is None:
            raise RuntimeError(f"delivery {delivery_id} was neither recorded nor found")
        return _result(recorded, duplicate=True)

    def _handle(self, delivery_id: str, event: str, payload: Mapping[str, Any]) -> Delivery:
        def outcome(
            outcome: DeliveryOutcome,
            reason: str,
            *,
            action: str | None = None,
            repository: str | None = None,
        ) -> Delivery:
            return Delivery(
                delivery_id,
                event,
                outcome,
                action=action,
                repository=repository,
                detail={"reason": reason},
            )

        if event != "pull_request":
            return outcome(DeliveryOutcome.IGNORED, f"{event} events do not start runs")
        try:
            parsed = PullRequestEvent.model_validate(payload)
        except ValidationError:
            return outcome(DeliveryOutcome.REFUSED, "not a well-formed pull_request event")
        repository = parsed.repository.full_name
        registration = self.registrations.get(repository)
        if registration is None:
            return outcome(
                DeliveryOutcome.IGNORED,
                "repository is not configured",
                action=parsed.action,
                repository=repository,
            )
        if parsed.installation is None or parsed.installation.id != registration.installation_id:
            return outcome(
                DeliveryOutcome.REFUSED,
                "the event is not from the installation configured for this repository",
                action=parsed.action,
                repository=repository,
            )
        pull = parsed.pull_request
        if pull.base.repo is None or pull.base.repo.full_name != repository:
            return outcome(
                DeliveryOutcome.REFUSED,
                "the pull request targets another repository",
                action=parsed.action,
                repository=repository,
            )

        if parsed.action == "closed":
            cancelled = self.store.close_pull_request(
                repository, pull.number, actor=f"github:{parsed.sender.login}"
            )
            return Delivery(
                delivery_id,
                event,
                DeliveryOutcome.RUNS_CANCELLED if cancelled else DeliveryOutcome.IGNORED,
                detail={"reason": "pull request closed", "cancelled": [str(r) for r in cancelled]},
                action=parsed.action,
                repository=repository,
            )
        if parsed.action not in RUN_ACTIONS:
            return outcome(
                DeliveryOutcome.IGNORED,
                f"{parsed.action} does not start a run",
                action=parsed.action,
                repository=repository,
            )
        if pull.head.repo is None:
            return outcome(
                DeliveryOutcome.REFUSED,
                "the head repository no longer exists",
                action=parsed.action,
                repository=repository,
            )

        head_repository = pull.head.repo.full_name
        self.github.record_head(
            PullRequestHead(
                repository=repository,
                pull_request=pull.number,
                head_sha=pull.head.sha,
                head_repository=head_repository,
                author=pull.user.login,
                installation_id=registration.installation_id,
            )
        )
        config = registration.project
        self.store.save_config(config.stored())
        inputs = RunInputs(
            repository=repository,
            pull_request=pull.number,
            base_sha=pull.base.sha,
            head_sha=pull.head.sha,
            head_repository=None if head_repository == repository else head_repository,
            config_digest=config.digest(),
            dataset_digest=registration.dataset_digest,
            engine_version=registration.engine_version,
        )
        created = self.store.create_run(
            inputs,
            RunTrigger.WEBHOOK,
            actor=f"github:{parsed.sender.login}",
            run_ttl=self.run_ttl,
            retry=self.retry,
        )
        return Delivery(
            delivery_id,
            event,
            DeliveryOutcome.RUN_CREATED if created.created else DeliveryOutcome.RUN_EXISTS,
            run_id=created.run.run_id,
            detail={"superseded": [str(run) for run in created.superseded]},
            action=parsed.action,
            repository=repository,
        )


def _result(delivery: Delivery, *, duplicate: bool) -> IntakeResult:
    return IntakeResult(delivery.outcome, delivery.run_id, delivery.detail, duplicate)
