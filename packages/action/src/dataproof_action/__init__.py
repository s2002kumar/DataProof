"""DataProof GitHub Action: database-tier verification of a PostgreSQL/Alembic pull request.

The Action reuses the GitHub App's verifier, contracts, local Docker backend and trusted
entrypoint. It reads what it trusts (configuration, fixture, oracles, requirements, binding
approvals) from the base branch and what the pull request proposes (migrations, bindings) from
the head commit, runs both migrations in disposable PostgreSQL databases with the pull request's
code isolated in a container, and reports a job summary, annotations and a JSON report. The job
fails for every verdict but ``CHECKED`` and ``CHECKED_WITH_WARNINGS``.
"""

from dataproof_action.config import DEFAULT_CONFIG_PATH, ActionConfig, ConfigError, parse_config
from dataproof_action.event import EventError, PullRequestEvent, load_event
from dataproof_action.run import EXIT_CODES, Inputs, run

__all__ = [
    "DEFAULT_CONFIG_PATH",
    "EXIT_CODES",
    "ActionConfig",
    "ConfigError",
    "EventError",
    "Inputs",
    "PullRequestEvent",
    "load_event",
    "parse_config",
    "run",
]
