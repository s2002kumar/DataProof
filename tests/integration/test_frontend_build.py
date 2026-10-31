"""The sample frontend builds offline inside a step, as ADR-0004 requires of the candidate.

Steps run on the environment's internal network, which has no route to the internet, so these
builds are network-disabled. Each test also states what the offline constraint rules out.
"""

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from dataproof_contracts import Environment
from dataproof_runner.backend import FRONTEND_BUILD_LIMITS, EnvironmentHandle
from dataproof_runner.local_docker import LocalDockerBackend
from dataproof_runner.source import archive_commit
from docker_env import LEDGER_WEB_PAYMENTS, SAMPLE_APP, AppRepo

pytestmark = [pytest.mark.integration, pytest.mark.docker]

WEB = f"{SAMPLE_APP}/web"
BUILD = ["build-web", "--dir", WEB]
TIMEOUT = timedelta(minutes=8)


@pytest.fixture
def environment(backend: LocalDockerBackend) -> Iterator[EnvironmentHandle]:
    handle = backend.provision(
        uuid4(), Environment.CANDIDATE, expires_at=datetime.now(UTC) + timedelta(minutes=30)
    )
    yield handle
    backend.destroy(handle.run_id)


def _build(
    backend: LocalDockerBackend, environment: EnvironmentHandle, image: str, repo: AppRepo, sha: str
) -> tuple[int | None, str, float]:
    result = backend.run_step(
        environment,
        image=image,
        source=archive_commit(repo.path, sha),
        arguments=BUILD,
        timeout=TIMEOUT,
        limits=FRONTEND_BUILD_LIMITS,
    )
    assert not result.timed_out, result.output
    return result.exit_code, result.output, result.duration_seconds


def test_the_base_frontend_builds_offline(
    backend: LocalDockerBackend,
    environment: EnvironmentHandle,
    runtime_image: str,
    app_repo: AppRepo,
) -> None:
    exit_code, output, seconds = _build(
        backend, environment, runtime_image, app_repo, app_repo.base
    )
    assert exit_code == 0, output
    assert "/orders/[id]" in output
    assert "server-rendered on demand" in output, "pages render on request, not at build time"
    assert seconds < TIMEOUT.total_seconds()


def test_a_candidate_frontend_change_is_compiled_from_its_source(
    backend: LocalDockerBackend,
    environment: EnvironmentHandle,
    runtime_image: str,
    app_repo: AppRepo,
) -> None:
    head = app_repo.commit(
        app_repo.base,
        {f"{WEB}/lib/payments.ts": LEDGER_WEB_PAYMENTS.read_text("utf-8")},
        "Read refunds from the ledger API",
    )
    exit_code, output, _ = _build(backend, environment, runtime_image, app_repo, head)
    assert exit_code == 0, output


def test_a_build_that_needs_the_network_fails(
    backend: LocalDockerBackend,
    environment: EnvironmentHandle,
    runtime_image: str,
    app_repo: AppRepo,
) -> None:
    layout = app_repo.show(app_repo.base, f"{WEB}/app/layout.tsx")
    fetching = layout.replace(
        'import "./globals.css";',
        'import "./globals.css";\nimport { Inter } from "next/font/google";\n'
        'const inter = Inter({ subsets: ["latin"] });\nvoid inter;',
    )
    head = app_repo.commit(app_repo.base, {f"{WEB}/app/layout.tsx": fetching}, "Use a Google font")
    exit_code, output, _ = _build(backend, environment, runtime_image, app_repo, head)
    assert exit_code not in (None, 0)
    assert "fonts.googleapis.com" in output or "Failed to fetch" in output, output[-2000:]


def test_a_type_error_fails_the_build(
    backend: LocalDockerBackend,
    environment: EnvironmentHandle,
    runtime_image: str,
    app_repo: AppRepo,
) -> None:
    broken = 'export const total: number = "not a number";\n'
    head = app_repo.commit(app_repo.base, {f"{WEB}/lib/broken.ts": broken}, "A type error")
    exit_code, output, _ = _build(backend, environment, runtime_image, app_repo, head)
    assert exit_code not in (None, 0)
    assert "Type error" in output or "is not assignable" in output, output[-2000:]


def test_a_source_that_ships_node_modules_is_refused(
    backend: LocalDockerBackend,
    environment: EnvironmentHandle,
    runtime_image: str,
    app_repo: AppRepo,
) -> None:
    head = app_repo.commit(
        app_repo.base,
        {f"{WEB}/node_modules/next/package.json": '{"name": "next", "version": "0.0.0"}\n'},
        "Vendor a dependency",
    )
    exit_code, output, _ = _build(backend, environment, runtime_image, app_repo, head)
    assert exit_code == 66
    assert "ships its own node_modules" in output
