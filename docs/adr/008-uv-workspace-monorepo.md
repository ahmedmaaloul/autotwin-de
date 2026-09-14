# ADR 008 — uv workspace + pnpm, no Nx or Turborepo

**Status:** Accepted · **Date:** 2026-09-14

## Context

The repository holds seven Python packages and one Next.js application. They share types
(`autotwin_contracts`), a database layer (`autotwin_core`), and a release cadence. They must
install and run with one command on a clean machine.

## Decision

- **Python:** a single `uv` workspace. The root `pyproject.toml` lists the members and the dev
  tooling; each member declares only its own runtime dependencies. One lockfile, one virtualenv,
  one `uv sync`. Layer boundaries are enforced by declared dependencies, not convention —
  `autotwin_contracts` cannot import SQLAlchemy because it does not depend on it.
- **Frontend:** plain `pnpm` in `apps/web`.
- **Cross-stack orchestration:** a `Makefile`. It is the front door and the documentation.

## Alternatives rejected

- **Poetry / pip-tools.** Work, but multi-package workspaces are second-class and resolution is
  an order of magnitude slower, which is felt on every CI run.
- **Nx / Turborepo.** Real value once there are many JS packages with an expensive build graph.
  There is exactly one JS package. The task graph would be configuration for its own sake.
- **Separate repositories per service.** Cross-cutting changes to the event schema would need
  coordinated releases for a project developed by one person.

## Consequences

- A contributor needs both `uv` and `pnpm`. `make setup` installs and verifies both.
- CI runs two independent jobs (backend, frontend) in parallel, each with its own cache.
- Docker images install from the same lockfile, so what runs in the container is what ran
  locally.
