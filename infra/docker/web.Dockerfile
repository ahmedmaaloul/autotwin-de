# syntax=docker/dockerfile:1.7
#
# AutoTwin DE — Next.js web image.
#
# Built from the repository root so the build context matches infra/docker/api.Dockerfile
# and a single `docker compose --profile full build` works without per-service contexts:
#     docker build -f infra/docker/web.Dockerfile -t autotwin/web:dev .
#
# The runtime stage ships Next's `standalone` output (see apps/web/next.config.ts): the
# server plus only the traced node_modules, which is ~90 % smaller than copying the whole
# dependency tree and means no package manager is present in the running container.

ARG NODE_VERSION=22

# ---------------------------------------------------------------------------
# Base — pnpm via corepack, pinned by the `packageManager` field in apps/web/package.json
# ---------------------------------------------------------------------------
FROM node:${NODE_VERSION}-alpine AS base

# Next's SWC binaries are glibc-linked and need the musl shim on Alpine.
RUN apk add --no-cache libc6-compat

ENV PNPM_HOME=/pnpm \
    PATH=/pnpm:$PATH \
    COREPACK_ENABLE_DOWNLOAD_PROMPT=0 \
    NEXT_TELEMETRY_DISABLED=1
RUN corepack enable

WORKDIR /app

# ---------------------------------------------------------------------------
# Stage 1 — dependencies (their own layer, keyed on the lockfile alone)
# ---------------------------------------------------------------------------
FROM base AS deps

COPY apps/web/package.json apps/web/pnpm-lock.yaml apps/web/
WORKDIR /app/apps/web
RUN --mount=type=cache,id=pnpm,target=/pnpm/store \
    pnpm install --frozen-lockfile

# ---------------------------------------------------------------------------
# Stage 2 — build
# ---------------------------------------------------------------------------
FROM base AS builder

COPY --from=deps /app/apps/web/node_modules apps/web/node_modules
# node_modules and .next are excluded by .dockerignore, so this cannot clobber the layer above.
COPY apps/web apps/web

WORKDIR /app/apps/web
RUN pnpm build

# ---------------------------------------------------------------------------
# Stage 3 — runtime
# ---------------------------------------------------------------------------
FROM node:${NODE_VERSION}-alpine AS runtime

RUN apk add --no-cache libc6-compat

ENV NODE_ENV=production \
    NEXT_TELEMETRY_DISABLED=1 \
    PORT=3000 \
    HOSTNAME=0.0.0.0

WORKDIR /app

# `outputFileTracingRoot` is the repository root, so the standalone bundle keeps the
# apps/web/ prefix and hoists the traced node_modules to its own root.
COPY --from=builder --chown=node:node /app/apps/web/.next/standalone ./
COPY --from=builder --chown=node:node /app/apps/web/.next/static ./apps/web/.next/static
COPY --from=builder --chown=node:node /app/apps/web/public ./apps/web/public

# The node image already ships an unprivileged `node` user (uid 1000); no need to mint one.
USER node
EXPOSE 3000

HEALTHCHECK --interval=15s --timeout=5s --start-period=20s --retries=5 \
    CMD ["wget", "--quiet", "--spider", "http://127.0.0.1:3000/"]

CMD ["node", "apps/web/server.js"]
