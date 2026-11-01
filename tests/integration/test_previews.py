"""Live previews on the local Docker backend: the app behind its trusted, token-checking edge."""

import json
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import psycopg
import pytest

from dataproof_contracts import Environment
from dataproof_runner.backend import FRONTEND_BUILD_LIMITS, EnvironmentHandle, PreviewAccess
from dataproof_runner.baselines import CommerceBaseline
from dataproof_runner.local_docker import LocalDockerBackend
from dataproof_runner.source import archive_commit
from docker_env import SAMPLE_APP, AppRepo, http_get

pytestmark = [pytest.mark.integration, pytest.mark.docker]

MIGRATE = ["migrate", "--config", f"{SAMPLE_APP}/alembic.ini", "--target", "head"]
SERVE = ["serve", "--app-dir", SAMPLE_APP, "--web-dir", f"{SAMPLE_APP}/web"]
TIMEOUT = timedelta(minutes=8)


@pytest.fixture
def environment(
    backend: LocalDockerBackend, runtime_image: str, app_repo: AppRepo
) -> Iterator[EnvironmentHandle]:
    """A candidate environment migrated to the base schema and loaded with the baseline."""
    handle = backend.provision(
        uuid4(), Environment.CANDIDATE, expires_at=datetime.now(UTC) + timedelta(minutes=30)
    )
    migrated = backend.run_step(
        handle,
        image=runtime_image,
        source=archive_commit(app_repo.path, app_repo.base),
        arguments=MIGRATE,
        timeout=timedelta(minutes=2),
    )
    assert migrated.succeeded, migrated.output
    with psycopg.connect(handle.database.conninfo(handle.database.owner)) as conn:
        CommerceBaseline().load(conn)
    yield handle
    backend.destroy(handle.run_id)


def test_a_preview_serves_the_api_and_frontend_only_behind_its_token(
    backend: LocalDockerBackend,
    environment: EnvironmentHandle,
    runtime_image: str,
    app_repo: AppRepo,
) -> None:
    result = backend.start_preview(
        environment,
        image=runtime_image,
        source=archive_commit(app_repo.path, app_repo.base),
        arguments=SERVE,
        timeout=TIMEOUT,
        limits=FRONTEND_BUILD_LIMITS,
    )
    assert result.ready, result.output[-3000:]
    assert result.access is not None
    access: PreviewAccess = result.access
    assert access.url.startswith("http://127.0.0.1:")
    assert result.build_seconds is not None
    assert result.peak_memory_bytes > 0
    assert result.cpu_seconds > 0
    assert environment.database.reader.password not in result.output

    status, headers, _ = http_get(f"{access.url}/__dataproof/health")
    assert status == 200
    assert headers["X-DataProof-Commit"] == app_repo.base
    assert headers["X-DataProof-Environment"] == "CANDIDATE"

    for path in ("/", "/orders/1", "/api/orders/1"):
        assert http_get(f"{access.url}{path}")[0] == 401, path
        assert http_get(f"{access.url}{path}", token="wrong")[0] == 401, path
    assert http_get(f"{access.url}/__dataproof/login?token=wrong")[0] == 403

    status, headers, _ = http_get(access.login_url)
    assert status == 303
    assert f"dataproof_preview={access.token}" in headers["Set-Cookie"]
    assert "HttpOnly" in headers["Set-Cookie"]

    status, _, body = http_get(f"{access.url}/api/orders/1", token=access.token)
    assert status == 200
    assert json.loads(body)["refunded_total"] == "49.99"
    status, headers, page = http_get(f"{access.url}/orders/1", token=access.token)
    assert status == 200
    assert b"49.99" in page
    assert headers["X-DataProof-Commit"] == app_repo.base

    assert backend.preview_access(environment) == access
    backend.destroy(environment.run_id)
    assert backend.resources(environment.run_id) == []


def test_a_frontend_that_does_not_build_is_a_build_failure(
    backend: LocalDockerBackend,
    environment: EnvironmentHandle,
    runtime_image: str,
    app_repo: AppRepo,
) -> None:
    head = app_repo.commit(
        app_repo.base,
        {f"{SAMPLE_APP}/web/lib/broken.ts": 'export const total: number = "x";\n'},
        "A type error",
    )
    result = backend.start_preview(
        environment,
        image=runtime_image,
        source=archive_commit(app_repo.path, head),
        arguments=SERVE,
        timeout=TIMEOUT,
        limits=FRONTEND_BUILD_LIMITS,
    )
    assert (result.ready, result.failure) == (False, "build")
    assert result.access is None
    assert backend.preview_access(environment) is None


def test_an_api_that_cannot_start_is_a_start_failure(
    backend: LocalDockerBackend,
    environment: EnvironmentHandle,
    runtime_image: str,
    app_repo: AppRepo,
) -> None:
    main = app_repo.show(app_repo.base, f"{SAMPLE_APP}/app/main.py")
    head = app_repo.commit(
        app_repo.base,
        {f"{SAMPLE_APP}/app/main.py": main + '\nraise RuntimeError("misconfigured")\n'},
        "An API that fails on import",
    )
    result = backend.start_preview(
        environment,
        image=runtime_image,
        source=archive_commit(app_repo.path, head),
        arguments=SERVE,
        timeout=TIMEOUT,
        limits=FRONTEND_BUILD_LIMITS,
    )
    assert (result.ready, result.failure) == (False, "start")
    assert "misconfigured" in result.output
