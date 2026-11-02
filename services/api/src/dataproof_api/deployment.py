"""Deployment configuration, and the processes composed from it.

A deployment is a trusted operator file (TOML; its path in ``DATAPROOF_CONFIG``) plus secrets from
the environment. The file names the GitHub App, the dashboard's origin, the preview domain, the
worker's data directory and every registered repository with its trusted project configuration
(the oracles, allowed change roots and runtime image that runs are held to). Unknown keys are
refused, so a secret cannot end up in the file by mistake: secrets are read only from the
environment, and each process asks only for the ones it needs.

Processes (``python -m dataproof_api <process>``): ``migrate``, ``api`` (webhook intake and the
reviewer API), ``gateway`` (signed previews), ``worker`` (runs) and ``publisher`` (check runs).
"""

import os
import subprocess
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import timedelta
from importlib.metadata import version
from pathlib import Path
from typing import Annotated, Any, Literal, Self

import docker
import httpx2
from fastapi import FastAPI
from pydantic import BaseModel, ConfigDict, Field, PositiveInt, field_validator, model_validator

from dataproof_api.app import create_app
from dataproof_api.checks import CheckPublisher
from dataproof_api.gateway import Gateway, Grants, backend_resolver
from dataproof_api.github_app import GitHubApp, load_private_key
from dataproof_api.intake import Intake, Registration
from dataproof_api.reviewer import PreviewLinks, Reviewer
from dataproof_api.reviewers import GitHubSignIn, Permissions
from dataproof_api.sessions import Sessions
from dataproof_api.sources import github_remotes
from dataproof_contracts._base import RepositoryName
from dataproof_control import (
    GitHubRecords,
    ReproductionQueue,
    RetryPolicy,
    RunResults,
    RunStore,
)
from dataproof_runner import Worker, WorkerSettings
from dataproof_runner.artifacts import LocalArtifactStore
from dataproof_runner.baselines import baseline_for
from dataproof_runner.broker import GitFetcher
from dataproof_runner.configured import ConfiguredPipelines
from dataproof_runner.local_docker import LocalDockerBackend
from dataproof_runner.pipeline import Pipeline, PipelineSettings
from dataproof_runner.project import ProjectConfig
from dataproof_runner.reproducer import Reproducer

_LOOPBACK = ("http://127.0.0.1", "http://localhost")


class _Settings(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class GitHubSettings(_Settings):
    app_id: Annotated[str, Field(pattern=r"^[0-9]+$")]
    private_key_file: Path
    client_id: Annotated[str, Field(min_length=1)]
    api_url: str = "https://api.github.com"
    web_url: str = "https://github.com"
    git_url: str = "https://github.com"
    """Where the worker fetches commits: ``<git_url>/<owner>/<name>.git`` with an installation
    token. A ``file://`` directory of bare repositories serves the local rehearsal."""

    @field_validator("git_url")
    @classmethod
    def _git_url(cls, value: str) -> str:
        if not value.startswith(("https://", "file://")):
            raise ValueError("git_url is https://, or file:// for a local rehearsal")
        return value.rstrip("/")


def _origin(value: str) -> str:
    value = value.rstrip("/")
    if not (value.startswith("https://") or value.startswith(_LOOPBACK)):
        raise ValueError("an origin is https://, or http:// on the loopback for local use")
    return value


class DashboardSettings(_Settings):
    origin: str
    """Where reviewers open the dashboard; the API refuses writes from any other origin."""

    _check = field_validator("origin")(_origin)


class PreviewSettings(_Settings):
    domain: Annotated[str, Field(pattern=r"^[a-z0-9.-]+$")]
    """Each preview is served at ``<role>-<run>.<domain>``: use a separate registrable domain.
    For local use, ``previews.localhost`` resolves to the loopback in browsers."""
    scheme: Literal["https", "http"] = "https"
    port: Annotated[int, Field(gt=0, lt=65536)] | None = None
    """The port in preview links, when the gateway is not on the scheme's default."""


class RunSettings(_Settings):
    ttl_minutes: PositiveInt = 240
    max_attempts: PositiveInt = 3


class WorkerSettingsFile(_Settings):
    data_dir: Path
    """The trusted source mirror and the artifact store live here."""
    scope: Annotated[str, Field(pattern=r"^[a-z0-9]{1,20}$")] = "dataproof"
    previews: bool = True


class RepositorySettings(_Settings):
    name: RepositoryName
    installation_id: PositiveInt
    project: dict[str, Any]
    """The trusted ``ProjectConfig`` for this repository, without its name."""

    @model_validator(mode="after")
    def _valid_project(self) -> Self:
        # Validate when the file loads, not when a run first needs it.
        self.config()
        return self

    def config(self) -> ProjectConfig:
        return ProjectConfig.model_validate({**self.project, "repository": self.name})


class Deployment(_Settings):
    github: GitHubSettings
    dashboard: DashboardSettings
    previews: PreviewSettings
    worker: WorkerSettingsFile
    runs: RunSettings = RunSettings()
    repositories: Annotated[list[RepositorySettings], Field(min_length=1)]

    @classmethod
    def load(cls, path: Path) -> "Deployment":
        with path.open("rb") as file:
            return cls.model_validate(tomllib.load(file))

    @property
    def installations(self) -> dict[str, int]:
        return {repository.name: repository.installation_id for repository in self.repositories}

    @property
    def configs(self) -> dict[str, ProjectConfig]:
        return {repository.name: repository.config() for repository in self.repositories}


class MissingSecretError(RuntimeError):
    pass


@dataclass(frozen=True)
class Secrets:
    """Secrets, from the environment only. Each process reads just the ones it needs."""

    environ: Mapping[str, str]

    def get(self, name: str, *, minimum: int = 1) -> str:
        value = self.environ.get(name, "")
        if len(value) < minimum:
            raise MissingSecretError(
                f"set {name}" + (f" (at least {minimum} characters)" if minimum > 1 else "")
            )
        return value

    @property
    def database_url(self) -> str:
        return self.get("DATAPROOF_DATABASE_URL")


@dataclass(frozen=True)
class Stack:
    """Every process of a deployment, composed from its configuration and secrets."""

    deployment: Deployment
    secrets: Secrets

    @classmethod
    def from_environment(cls, environ: Mapping[str, str] | None = None) -> "Stack":
        environ = os.environ if environ is None else environ
        path = environ.get("DATAPROOF_CONFIG")
        if not path:
            raise MissingSecretError("set DATAPROOF_CONFIG to the deployment file")
        return cls(Deployment.load(Path(path)), Secrets(environ))

    # Shared pieces ------------------------------------------------------------------------------

    def github_app(self) -> GitHubApp:
        settings = self.deployment.github
        return GitHubApp(
            settings.app_id,
            load_private_key(settings.private_key_file.read_bytes()),
            client=httpx2.Client(base_url=settings.api_url, timeout=30),
        )

    def store(self) -> RunStore:
        return RunStore(self.secrets.database_url)

    def retry(self) -> RetryPolicy:
        return RetryPolicy(max_attempts=self.deployment.runs.max_attempts)

    def run_ttl(self) -> timedelta:
        return timedelta(minutes=self.deployment.runs.ttl_minutes)

    def registrations(self) -> dict[str, Registration]:
        engine = version("dataproof-runner")
        return {
            name: Registration(
                project=config,
                installation_id=self.deployment.installations[name],
                dataset_digest=baseline_for(config.baseline).digest(),
                engine_version=engine,
            )
            for name, config in self.deployment.configs.items()
        }

    def grants(self) -> Grants:
        return Grants(self.secrets.get("DATAPROOF_PREVIEW_SECRET", minimum=32).encode())

    # Processes ----------------------------------------------------------------------------------

    def api(self) -> FastAPI:
        """Webhook intake and the reviewer API."""
        settings = self.deployment
        store = self.store()
        records = GitHubRecords(self.secrets.database_url)
        app = self.github_app()
        permissions = Permissions(app, settings.installations)
        intake = Intake(
            store, records, self.registrations(), run_ttl=self.run_ttl(), retry=self.retry()
        )
        reviewer = Reviewer(
            store,
            RunResults(self.secrets.database_url),
            records,
            Sessions(self.secrets.get("DATAPROOF_SESSION_SECRET", minimum=32).encode()),
            GitHubSignIn(
                settings.github.client_id,
                self.secrets.get("DATAPROOF_GITHUB_CLIENT_SECRET"),
                f"{settings.dashboard.origin}/auth/callback",
                web=httpx2.Client(base_url=settings.github.web_url, timeout=30),
                api=httpx2.Client(base_url=settings.github.api_url, timeout=30),
            ),
            permissions,
            configs=settings.configs,
            public_origin=settings.dashboard.origin,
            run_ttl=self.run_ttl(),
            retry=self.retry(),
            previews=PreviewLinks(
                self.grants(),
                settings.previews.domain,
                settings.previews.scheme,
                settings.previews.port,
            ),
            reproductions=ReproductionQueue(self.secrets.database_url),
        )
        return create_app(
            intake,
            webhook_secret=self.secrets.get("DATAPROOF_WEBHOOK_SECRET", minimum=16).encode(),
            reviewer=reviewer,
        )

    def webhook(self) -> FastAPI:
        """Webhook intake alone, with no other route: what a tunnel exposes to GitHub."""
        intake = Intake(
            self.store(),
            GitHubRecords(self.secrets.database_url),
            self.registrations(),
            run_ttl=self.run_ttl(),
            retry=self.retry(),
        )
        return create_app(
            intake,
            webhook_secret=self.secrets.get("DATAPROOF_WEBHOOK_SECRET", minimum=16).encode(),
            describe=False,
        )

    def journal(self) -> Path:
        """Where the independent reaper records what it removed."""
        return self.deployment.worker.data_dir / "reaper.jsonl"

    def backend(self) -> LocalDockerBackend:
        return LocalDockerBackend(scope=self.deployment.worker.scope, client=docker.from_env())

    def gateway(self, backend: LocalDockerBackend | None = None) -> FastAPI:
        settings = self.deployment
        return Gateway(
            self.store(),
            self.grants(),
            Permissions(self.github_app(), settings.installations),
            backend_resolver(backend or self.backend()),
            domain=settings.previews.domain,
            upstream=httpx2.Client(timeout=60),
        ).app()

    def publisher(self) -> CheckPublisher:
        return CheckPublisher(
            self.store(),
            RunResults(self.secrets.database_url),
            GitHubRecords(self.secrets.database_url),
            self.github_app(),
            self.deployment.installations,
            details_url=lambda run_id: f"{self.deployment.dashboard.origin}/runs/{run_id}",
        )

    def worker(self, backend: LocalDockerBackend | None = None, *, worker_id: str) -> Worker:
        """Claims and runs queued runs, each under its stored configuration."""
        pipelines = self.pipelines(backend or self.backend())
        return Worker(
            self.store(),
            pipelines.handlers(),
            pipelines.cleanup,
            worker_id=worker_id,
            settings=WorkerSettings(retry=self.retry()),
        )

    def reproducer(
        self, backend: LocalDockerBackend | None = None, *, worker_id: str
    ) -> Reproducer:
        """Runs queued reproduction requests, each under its run's stored configuration."""
        return Reproducer(
            ReproductionQueue(self.secrets.database_url),
            self.store(),
            self.pipelines(backend or self.backend()),
            worker_id=worker_id,
        )

    def pipelines(self, backend: LocalDockerBackend) -> ConfiguredPipelines:
        settings = self.deployment
        data = settings.worker.data_dir
        mirror = data / "mirror.git"
        if not (mirror / "HEAD").exists():
            mirror.parent.mkdir(parents=True, exist_ok=True)
            subprocess.run(  # noqa: S603 - fixed git binary and arguments
                ["git", "init", "--quiet", "--bare", str(mirror)],  # noqa: S607
                check=True,
            )
        remotes = github_remotes(
            self.github_app(), settings.installations, host=settings.github.git_url
        )
        fetcher = GitFetcher(mirror, remotes)
        results = RunResults(self.secrets.database_url)
        artifacts = LocalArtifactStore(data / "artifacts")
        store = self.store()

        def build(project: ProjectConfig) -> Pipeline:
            return Pipeline(
                project=project,
                clone=mirror,
                backend=backend,
                artifacts=artifacts,
                results=results,
                baseline=baseline_for(project.baseline),
                settings=PipelineSettings(previews=settings.worker.previews),
                fetcher=fetcher,
            )

        return ConfiguredPipelines(store, backend, build)
