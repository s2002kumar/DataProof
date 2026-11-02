"""Stage handlers that run every run under exactly the configuration it pins.

A run's inputs pin its project configuration by digest, and a binding approval starts a run
under a new configuration, so one worker serves runs under many. ``ConfiguredPipelines`` loads
each run's configuration from the control database by that digest, checks that it hashes to it,
and hands the stage to a :class:`Pipeline` built for that configuration alone. A run whose
configuration is missing or altered is an error, not a run under some other configuration.
"""

from collections.abc import Callable, Mapping
from typing import Any

from dataproof_contracts import ErrorCode, ReviewRun, RunState
from dataproof_control import RunStore
from dataproof_runner.backend import ExecutionBackend
from dataproof_runner.pipeline import STAGES, Pipeline, destroy_run
from dataproof_runner.project import ConfigIntegrityError, ProjectConfig
from dataproof_runner.worker import StageContext, StageHandler, StageOutcome


class UnknownConfigError(LookupError):
    """No configuration is stored under the digest a run pins."""


class ConfiguredPipelines:
    def __init__(
        self,
        store: RunStore,
        backend: ExecutionBackend,
        build: Callable[[ProjectConfig], Pipeline],
    ) -> None:
        self.store = store
        self.backend = backend
        self.build = build
        """Builds the pipeline for one configuration; everything else it needs is fixed."""

    def pipeline(self, run: ReviewRun) -> Pipeline:
        stored = self.store.config(run.inputs.config_digest)
        if stored is None:
            raise UnknownConfigError(f"no configuration is stored as {run.inputs.config_digest}")
        return self.build(ProjectConfig.from_stored(stored))

    def handlers(self) -> dict[RunState, StageHandler]:
        return {state: self._handler(state) for state in STAGES}

    def _handler(self, state: RunState) -> StageHandler:
        def handle(context: StageContext) -> StageOutcome:
            try:
                pipeline = self.pipeline(context.run)
            except (UnknownConfigError, ConfigIntegrityError) as error:
                return StageOutcome(
                    RunState.ERROR,
                    error_code=ErrorCode.CONFIG_DIGEST_MISMATCH,
                    detail={"pinned": context.run.inputs.config_digest, "problem": str(error)},
                )
            return pipeline.handlers()[state](context)

        return handle

    def cleanup(self, context: StageContext) -> Mapping[str, Any]:
        """Cleanup needs no configuration, so a run whose configuration is lost still ends."""
        return destroy_run(self.backend, context.run.run_id)
