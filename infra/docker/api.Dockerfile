# syntax=docker/dockerfile:1.7
#
# AutoTwin DE — Python backend image.
#
# One image serves all three Python entry points (API, simulator, stream consumer); compose
# overrides the command. They share a dependency closure, so three images would mean three
# copies of the same wheels for no isolation benefit.
#
# Build from the repository root, because the uv workspace spans packages/ and services/:
#     docker build -f infra/docker/api.Dockerfile -t autotwin/backend:dev .
#
# Layer order is dependency-metadata → dependencies → source, so editing a Python file
# re-runs the last seconds of the build rather than the dependency resolution.

# uv is pinned to the version that wrote uv.lock. A newer uv reads an older lock, but the
# reverse is not true, so a floating tag here would turn a routine `uv lock` on a developer's
# machine into a broken image build.
ARG UV_VERSION=0.9.17
ARG PYTHON_VERSION=3.12

FROM ghcr.io/astral-sh/uv:${UV_VERSION} AS uv

# ---------------------------------------------------------------------------
# Stage 1 — build the virtual environment
# ---------------------------------------------------------------------------
FROM python:${PYTHON_VERSION}-slim-bookworm AS builder

COPY --from=uv /uv /uvx /usr/local/bin/

# Compilers and libpq headers live here so a dependency without a manylinux wheel
# (psycopg[c], an older LightGBM on a newer Python) still builds. None of it reaches runtime.
RUN --mount=type=cache,target=/var/cache/apt,sharing=locked \
    --mount=type=cache,target=/var/lib/apt/lists,sharing=locked \
    apt-get update \
    && apt-get install -y --no-install-recommends build-essential libpq-dev

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    UV_PROJECT_ENVIRONMENT=/app/.venv

WORKDIR /app

# Dependency metadata only: every member's pyproject.toml plus the lockfile. uv needs the
# member manifests to resolve the workspace even when it is not installing the members yet.
COPY pyproject.toml uv.lock ./
COPY packages/contracts/pyproject.toml packages/contracts/
COPY packages/core/pyproject.toml packages/core/
COPY services/api/pyproject.toml services/api/
COPY services/ingestion/pyproject.toml services/ingestion/
COPY services/simulator/pyproject.toml services/simulator/
COPY services/streaming/pyproject.toml services/streaming/
COPY services/ml/pyproject.toml services/ml/

# Third-party dependencies only. Invalidated by uv.lock, never by a source edit.
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-install-workspace

# First-party source. `alembic.in[i]` is an optional-source glob: it copies alembic.ini when
# it exists and contributes nothing when it does not, so the image can run migrations without
# the build breaking before the migration config lands. It needs a sibling that always
# matches, hence pyproject.toml on the same line.
COPY packages/ packages/
COPY services/ services/
COPY alembic/ alembic/
COPY pyproject.toml alembic.in[i] ./

# Installs the seven workspace members into the same environment. Editable on purpose, so
# `alembic` and the module CLIs resolve to /app instead of a second copy in site-packages.
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev

# ---------------------------------------------------------------------------
# Stage 2 — runtime
# ---------------------------------------------------------------------------
FROM python:${PYTHON_VERSION}-slim-bookworm AS runtime

# libpq5 is the only shared library the runtime actually needs: psycopg[c] links against it
# and psycopg[binary] ignores it. Shapely/pyproj carry their own GEOS/PROJ inside the wheel.
RUN --mount=type=cache,target=/var/cache/apt,sharing=locked \
    --mount=type=cache,target=/var/lib/apt/lists,sharing=locked \
    apt-get update \
    && apt-get install -y --no-install-recommends libpq5

# Non-root. A fixed uid/gid keeps bind-mounted ./data writable across rebuilds.
RUN groupadd --system --gid 10001 autotwin \
    && useradd --system --uid 10001 --gid autotwin --home-dir /app --no-create-home autotwin

ENV PATH=/app/.venv/bin:$PATH \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    AUTOTWIN_ENV=docker

WORKDIR /app
COPY --from=builder --chown=autotwin:autotwin /app /app

# Writable directories for the on-disk provider cache and model artefacts. Compose
# bind-mounts over both; this is for a plain `docker run` without mounts.
RUN mkdir -p /app/data /app/models && chown -R autotwin:autotwin /app/data /app/models

USER autotwin
EXPOSE 8000

# Python rather than curl, so the runtime image stays free of an HTTP client it does not need.
HEALTHCHECK --interval=15s --timeout=5s --start-period=30s --retries=5 \
    CMD ["python", "-c", "import urllib.request as u, sys; sys.exit(0 if u.urlopen('http://127.0.0.1:8000/health', timeout=3).status == 200 else 1)"]

CMD ["uvicorn", "autotwin_api.main:app", "--host", "0.0.0.0", "--port", "8000"]
