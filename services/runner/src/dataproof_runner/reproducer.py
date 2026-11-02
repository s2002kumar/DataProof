"""Runs queued reproduction requests, each under its run's stored configuration.

A request that cannot succeed (the check did not fail, the run has no sealed evidence, the check
is an API behaviour contract) ends at once with the reason. Anything else is retried until the
job's attempts are spent. The run itself is never changed (``Pipeline.reproduce``).
"""

import logging
from datetime import timedelta

from dataproof_control import ReproductionQueue, RunStore
from dataproof_runner.configured import ConfiguredPipelines

log = logging.getLogger(__name__)


class Reproducer:
    def __init__(
        self,
        queue: ReproductionQueue,
        store: RunStore,
        pipelines: ConfiguredPipelines,
        *,
        worker_id: str,
        lease: timedelta = timedelta(minutes=30),
    ) -> None:
        self.queue = queue
        self.store = store
        self.pipelines = pipelines
        self.worker_id = worker_id
        self.lease = lease

    def run_once(self) -> bool:
        """Run one queued request; False if none was waiting."""
        claim = self.queue.claim(self.worker_id, lease=self.lease)
        if claim is None:
            return False
        try:
            run = self.store.get_run(claim.run_id)
            reproduction = self.pipelines.pipeline(run).reproduce(run, claim.check_id)
        except ValueError as error:
            self.queue.fail(claim, str(error), final=True)
        except Exception as error:
            log.exception("reproduction %s failed", claim.request_id)
            self.queue.fail(claim, f"{type(error).__name__}: {error}")
        else:
            self.queue.finish(claim, reproduction.reproduction_id)
        return True
