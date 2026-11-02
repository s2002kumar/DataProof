"""Binding decisions, checked against what the run showed the reviewer (ADR-0005).

A reviewer approves or rejects a binding as one of the run's checks recorded it: the decision
must name a binding that a check of that run is waiting on. An approval starts a new run under
the run's own configuration plus every binding decision made at its head; the control store
records the decision and starts that run in one transaction. Who may decide, and the approver
rule, are for the caller (the reviewer API) to enforce before calling this.
"""

from datetime import timedelta
from uuid import UUID

from dataproof_contracts import CandidateBinding, HumanDecision, NotRunReason
from dataproof_control import RecordedDecision, RetryPolicy, RunResults, RunStore, StoredConfig
from dataproof_runner.project import ProjectConfig


class NotAwaitingReviewError(ValueError):
    """The run has no check waiting on this binding."""


def awaiting_review(results: RunResults, run_id: UUID) -> dict[str, CandidateBinding]:
    """The bindings that the run's checks are waiting on, by digest."""
    return {
        record.binding.digest(): record.binding
        for record in results.checks(run_id)
        if record.binding is not None
        and record.result.not_run_reason is NotRunReason.AWAITING_BINDING_REVIEW
    }


def decide_binding(
    store: RunStore,
    results: RunResults,
    decision: HumanDecision,
    *,
    run_ttl: timedelta,
    retry: RetryPolicy,
) -> RecordedDecision:
    run = store.get_run(decision.run_id)
    if decision.binding_digest not in awaiting_review(results, run.run_id):
        raise NotAwaitingReviewError(
            f"no check of run {run.run_id} is waiting on binding {decision.binding_digest}"
        )
    stored = store.config(run.inputs.config_digest)
    if stored is None:
        raise LookupError(f"run {run.run_id} pins a configuration that is not stored")
    base = ProjectConfig.from_stored(stored)

    def configure(approved: frozenset[str], rejected: frozenset[str]) -> StoredConfig:
        return base.model_copy(
            update={
                "approved_binding_digests": base.approved_binding_digests | approved,
                "rejected_binding_digests": base.rejected_binding_digests | rejected,
            }
        ).stored()

    return store.decide_binding(decision, configure=configure, run_ttl=run_ttl, retry=retry)
