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

FROM python:3.15-slim@sha256:94d3426f1def7db4a106a232ac121bfd975393e5dfd40a619b955fae45fe7913 AS dependencies
COPY --from=uv /uv /usr/local/bin/uv
COPY sample-app/pyproject.toml sample-app/uv.lock /build/
ENV UV_PROJECT_ENVIRONMENT=/opt/venv \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never
RUN uv sync --frozen --no-dev --no-install-project --project /build

# Frontend dependencies, only from the frontend's lockfile, as a flat node_modules that steps
# link into the source they build (ADR-0004: the task never installs anything).
FROM node:26-slim@sha256:e4db4c914bf8ad241bd24804fc25d272d89bad97e68cdc8a1414862790a02c39 AS web-dependencies
RUN npm install --global pnpm@11.11.0
WORKDIR /opt/web
COPY sample-app/web/package.json sample-app/web/pnpm-lock.yaml ./
RUN pnpm install --frozen-lockfile --ignore-workspace --config.node-linker=hoisted

FROM python:3.15-slim@sha256:94d3426f1def7db4a106a232ac121bfd975393e5dfd40a619b955fae45fe7913
COPY --from=dependencies /opt/venv /opt/venv
COPY --from=web-dependencies /usr/local/bin/node /usr/local/bin/node
COPY --from=web-dependencies /opt/web/node_modules /opt/web/node_modules
COPY infra/docker/runtime/entrypoint.py /opt/dataproof/entrypoint.py
# /opt/web is where a step builds the verified frontend source; only the step's own user may
# write there, and only inside its own disposable container. node_modules stays root-owned.
RUN useradd --uid 10001 --user-group --no-create-home --shell /usr/sbin/nologin runner \
    && install -d -m 0755 /run/dataproof /work \
    && chown 10001:10001 /opt/web
ENV PATH=/opt/venv/bin:$PATH \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    NEXT_TELEMETRY_DISABLED=1
USER 10001:10001
WORKDIR /work
ENTRYPOINT ["/opt/venv/bin/python", "-I", "/opt/dataproof/entrypoint.py"]
