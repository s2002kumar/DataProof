# Trusted runtime image for reference and candidate steps (ADR-0004, ADR-0006).
#
# Built by a trusted maintainer or CI from the protected branch, never from pull-request input.
# Dependencies come only from the sample app's lockfile; a step never installs anything. The
# entrypoint verifies the staged source archive before any pull-request code runs, as an
# unprivileged user.
#
# Build from the repository root (the root .dockerignore limits the context):
#   docker build -f infra/docker/runtime/Dockerfile -t dataproof-runtime:dev .

FROM ghcr.io/astral-sh/uv:0.13.6@sha256:af22a52f5eafef3d78e551599d405c02df61919d18028f0cea8eda93bc9986d0 AS uv

FROM python:3.12-slim@sha256:aa943ce411423c7582288f18707ff66eddcbaeb510502d747a72aeb34f30d159 AS dependencies
COPY --from=uv /uv /usr/local/bin/uv
COPY sample-app/pyproject.toml sample-app/uv.lock /build/
ENV UV_PROJECT_ENVIRONMENT=/opt/venv \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never
RUN uv sync --frozen --no-dev --no-install-project --project /build

FROM python:3.12-slim@sha256:aa943ce411423c7582288f18707ff66eddcbaeb510502d747a72aeb34f30d159
COPY --from=dependencies /opt/venv /opt/venv
COPY infra/docker/runtime/entrypoint.py /opt/dataproof/entrypoint.py
RUN useradd --uid 10001 --user-group --no-create-home --shell /usr/sbin/nologin runner \
    && install -d -m 0755 /run/dataproof /work
ENV PATH=/opt/venv/bin:$PATH \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1
USER 10001:10001
WORKDIR /work
ENTRYPOINT ["/opt/venv/bin/python", "-I", "/opt/dataproof/entrypoint.py"]
