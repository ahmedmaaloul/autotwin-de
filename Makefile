# AutoTwin DE — the front door.
#
#   make            list every target
#   make setup      install dependencies, create .env
#   make demo       infrastructure, schema, seed data and a running simulator
#
# Design notes, because a Makefile is documentation that executes:
#
#  * macOS still ships GNU Make 3.81 (2006). This file therefore avoids .ONESHELL,
#    .RECIPEPREFIX and $(file ...), all of which need 3.82+. Every recipe is written so that
#    one shell per line is correct: multi-step logic is joined with `&&` or `\`.
#  * Targets that need containers depend on `guard-docker`, so a stopped Docker daemon
#    produces one sentence of advice instead of forty lines of connection errors.
#  * Python entry points follow one convention: `python -m <package>.cli <command>`.
SHELL := /bin/bash
# Honoured by GNU Make >= 3.82 and harmlessly ignored by 3.81; where it matters, recipes
# use explicit `&&` instead of relying on `-e`.
.SHELLFLAGS := -eu -o pipefail -c
.DEFAULT_GOAL := help

# ---------------------------------------------------------------------------
# Tooling (override on the command line: `make UV=/opt/uv/uv test`)
# ---------------------------------------------------------------------------
UV      ?= uv
PNPM    ?= pnpm
COMPOSE ?= docker compose
PY      := $(UV) run
WEB_DIR := apps/web

# Kept out of git (.gitignore covers .volumes/) — pid and log of the backgrounded simulator.
RUN_DIR := .volumes/run
SIM_PID := $(RUN_DIR)/simulator.pid
SIM_LOG := $(RUN_DIR)/simulator.log

# mypy is pointed at installed packages rather than at directories: the workspace uses a
# src/ layout, so a path-based invocation would derive module names like
# `packages.core.src.autotwin_core` and report every module twice.
MYPY_TARGETS := -p autotwin_contracts -p autotwin_core -p autotwin_api \
                -p autotwin_ingestion -p autotwin_simulator -p autotwin_streaming \
                -p autotwin_ml

# dbt is run through `uvx` so it gets its own resolved environment. It is a CLI the platform
# shells out to, not a library the platform imports, and pinning it into the workspace would
# drag dbt's dependency bounds across every other package.
DBT       ?= uvx --from "dbt-postgres~=1.9" dbt
DBT_FLAGS := --project-dir dbt --profiles-dir dbt

# OSRM: the Frankfurt–Stuttgart flagship corridor crosses Hessen and Baden-Württemberg, and
# Geofabrik publishes one extract per Bundesland, so the two are merged before building.
OSRM_DIR      := data/osrm
OSRM_NAME     := hessen-bw
OSRM_REGIONS  := hessen baden-wuerttemberg
OSRM_IMAGE    := ghcr.io/project-osrm/osrm-backend:v5.27.1
GEOFABRIK     := https://download.geofabrik.de/europe/germany
OSRM_PBF_HOST := $(addprefix $(OSRM_DIR)/,$(addsuffix .osm.pbf,$(OSRM_REGIONS)))
OSRM_PBF_CONT := $(addprefix /data/,$(addsuffix .osm.pbf,$(OSRM_REGIONS)))
OSRM_RUN      := docker run --rm -v "$(CURDIR)/$(OSRM_DIR):/data"

# ---------------------------------------------------------------------------
# Help
# ---------------------------------------------------------------------------
.PHONY: help
help: ## Show this help
	@printf '\n  \033[1mAutoTwin DE\033[0m — usage: make <target>\n'
	@awk 'BEGIN { FS = ":.*##" } \
		/^##@/ { printf "\n  \033[1m%s\033[0m\n", substr($$0, 5) } \
		/^[a-zA-Z0-9_.-]+:.*##/ { printf "    \033[36m%-16s\033[0m%s\n", $$1, $$2 }' \
		$(MAKEFILE_LIST)
	@printf '\n'

.PHONY: guard-docker
guard-docker:
	@docker info >/dev/null 2>&1 || { \
		printf '\n  \033[31mDocker is not reachable.\033[0m\n'; \
		printf '  Start Docker Desktop, or `sudo systemctl start docker` on Linux, then retry.\n\n'; \
		exit 1; \
	}

##@ Setup

.PHONY: setup
setup: ## Install Python + Node dependencies and create .env
	@command -v $(UV) >/dev/null 2>&1 || { \
		printf '  uv is missing — https://docs.astral.sh/uv/getting-started/installation/\n'; \
		exit 1; \
	}
	@command -v $(PNPM) >/dev/null 2>&1 || { \
		printf '  pnpm is missing — https://pnpm.io/installation\n'; exit 1; \
	}
	$(UV) sync --all-extras --dev
	$(PNPM) --dir $(WEB_DIR) install --frozen-lockfile
	@test -f .env || { cp .env.example .env; printf '  wrote .env from .env.example\n'; }
	@printf '\n  Ready. Next: \033[36mmake demo\033[0m\n\n'

.PHONY: lock
lock: ## Refresh uv.lock and the pnpm lockfile after a dependency change
	$(UV) lock
	$(PNPM) --dir $(WEB_DIR) install --lockfile-only

##@ Infrastructure

.PHONY: up
up: guard-docker ## Start local infrastructure (Postgres, Redpanda, Console)
	$(COMPOSE) up -d --remove-orphans
	@printf '\n  Postgres          localhost:5433  (autotwin/autotwin)\n'
	@printf '  Redpanda          localhost:19092\n'
	@printf '  Redpanda Console  http://localhost:8088\n\n'
	@printf '  The API and web app run on the host: \033[36mmake dev\033[0m\n'
	@printf '  Everything in containers instead:    docker compose --profile full up -d\n\n'

.PHONY: down
down: guard-docker ## Stop every container (volumes are kept)
	@$(MAKE) --no-print-directory sim-stop
	$(COMPOSE) --profile '*' down --remove-orphans

.PHONY: logs
logs: guard-docker ## Follow container logs — `make logs s=postgres` for one service
	$(COMPOSE) --profile '*' logs -f --tail=100 $(s)

.PHONY: ps
ps: guard-docker ## Show container status
	$(COMPOSE) --profile '*' ps

.PHONY: reset
reset: guard-docker ## DESTRUCTIVE — stop everything and delete the volumes, database included
	@printf '\n  \033[33mDeleting volumes:\033[0m autotwin_pgdata, autotwin_redpanda_data,\n'
	@printf '  autotwin_prometheus_data, autotwin_grafana_data.\n'
	@printf '  Every ingested and simulated row goes with them.\n\n'
	@$(MAKE) --no-print-directory sim-stop
	$(COMPOSE) --profile '*' down --volumes --remove-orphans
	@printf '\n  Gone. Rebuild with: \033[36mmake demo\033[0m\n\n'

##@ Database

.PHONY: db-upgrade
db-upgrade: ## Apply all Alembic migrations
	$(PY) alembic upgrade head

.PHONY: db-downgrade
db-downgrade: ## Roll back one Alembic revision
	$(PY) alembic downgrade -1

.PHONY: db-revision
db-revision: ## Autogenerate a migration — make db-revision m="add charging points"
	@test -n "$(m)" || { \
		printf '  usage: make db-revision m="short imperative description"\n'; exit 1; \
	}
	$(PY) alembic revision --autogenerate -m "$(m)"

##@ Data

.PHONY: seed
seed: ## Seed vehicle profiles and the demo corridors
	$(PY) python -m autotwin_ingestion.cli seed

.PHONY: ingest-charging
ingest-charging: ## Ingest the Bundesnetzagentur Ladesäulenregister
	$(PY) python -m autotwin_ingestion.cli ingest-charging

.PHONY: ingest-weather
ingest-weather: ## Ingest DWD station observations
	$(PY) python -m autotwin_ingestion.cli ingest-weather

.PHONY: ingest-traffic
ingest-traffic: ## Ingest Autobahn GmbH traffic events
	$(PY) python -m autotwin_ingestion.cli ingest-traffic

##@ Run

.PHONY: demo
demo: up ## Infrastructure, schema, demo data and a simulator — one command
	./infra/scripts/wait-for-postgres.sh
	@$(MAKE) --no-print-directory db-upgrade
	$(PY) python -m autotwin_ingestion.cli seed --demo
	@$(MAKE) --no-print-directory sim-stop
	@mkdir -p $(RUN_DIR)
	@nohup $(PY) python -m autotwin_simulator.cli run >$(SIM_LOG) 2>&1 & echo $$! >$(SIM_PID)
	@printf '\n  \033[1mAutoTwin DE is running.\033[0m\n\n'
	@printf '  Web (start with `make web`)  http://localhost:3000\n'
	@printf '  API docs                     http://localhost:8000/docs\n'
	@printf '  Redpanda Console             http://localhost:8088\n'
	@printf '  Simulator log                %s\n\n' '$(SIM_LOG)'
	@printf '  Stop the simulator with \033[36mmake down\033[0m.\n\n'

.PHONY: dev
dev: ## Run the API and the web app together — `make up` first
	@printf '\n  API  http://localhost:8000/docs\n  Web  http://localhost:3000\n'
	@printf '  Ctrl-C stops both.\n\n'
	@trap 'kill 0' EXIT INT TERM; \
		$(PY) uvicorn autotwin_api.main:app --reload --host 0.0.0.0 --port 8000 & \
		$(PNPM) --dir $(WEB_DIR) dev & \
		wait

.PHONY: api
api: ## Run the FastAPI application with auto-reload
	$(PY) uvicorn autotwin_api.main:app --reload --host 0.0.0.0 --port 8000

.PHONY: web
web: ## Run the Next.js development server
	$(PNPM) --dir $(WEB_DIR) dev

.PHONY: simulator
simulator: ## Run the vehicle simulator in the foreground
	$(PY) python -m autotwin_simulator.cli run

.PHONY: consumer
consumer: ## Run the telemetry consumer (Kafka to PostGIS)
	$(PY) python -m autotwin_streaming.cli consume

.PHONY: sim-stop
sim-stop:
	@if [ -f $(SIM_PID) ]; then kill "$$(cat $(SIM_PID))" 2>/dev/null || true; rm -f $(SIM_PID); fi
	@pkill -f 'autotwin_simulator[.]cli' 2>/dev/null || true

##@ Quality

.PHONY: test
test: test-api test-web ## Run every unit test, both stacks

.PHONY: test-api
test-api: ## pytest, excluding the tests that need a live database or broker
	$(PY) pytest -m "not integration and not kafka"

.PHONY: test-web
test-web: ## vitest
	$(PNPM) --dir $(WEB_DIR) test --run

.PHONY: e2e
e2e: ## Playwright smoke tests — needs the app running (`make dev`)
	$(PNPM) --dir $(WEB_DIR) e2e

.PHONY: lint
lint: ## ruff + eslint
	$(PY) ruff check .
	$(PY) ruff format --check .
	$(PNPM) --dir $(WEB_DIR) lint

.PHONY: format
format: ## Apply every automatic fix
	$(PY) ruff format .
	$(PY) ruff check --fix .
	$(PNPM) --dir $(WEB_DIR) lint:fix

.PHONY: typecheck
typecheck: ## mypy --strict + tsc --noEmit
	$(PY) mypy $(MYPY_TARGETS)
	$(PNPM) --dir $(WEB_DIR) typecheck

.PHONY: ci
ci: ## Everything CI runs, locally, in the same order
	$(UV) sync --all-extras --dev
	$(PY) ruff check .
	$(PY) ruff format --check .
	$(PY) mypy $(MYPY_TARGETS)
	$(PY) pytest -m "not integration and not kafka"
	$(PNPM) --dir $(WEB_DIR) install --frozen-lockfile
	$(PNPM) --dir $(WEB_DIR) lint
	$(PNPM) --dir $(WEB_DIR) typecheck
	$(PNPM) --dir $(WEB_DIR) test --run
	$(PNPM) --dir $(WEB_DIR) build

##@ Frontend contract

.PHONY: gen-api
gen-api: ## Regenerate apps/web/types/api.generated.ts from the live OpenAPI schema
	@curl --silent --fail --max-time 3 http://localhost:8000/openapi.json >/dev/null || { \
		printf '  The API must be running first: \033[36mmake api\033[0m\n'; exit 1; \
	}
	$(PNPM) --dir $(WEB_DIR) gen:api

##@ Machine learning

.PHONY: ml-train
ml-train: ## Train the energy-consumption model into models/
	$(PY) python -m autotwin_ml.cli train

.PHONY: ml-evaluate
ml-evaluate: ## Score the active model against the physical baseline
	$(PY) python -m autotwin_ml.cli evaluate

##@ Analytics & orchestration

.PHONY: dbt-run
dbt-run: ## Build the dbt staging and marts models
	$(DBT) run $(DBT_FLAGS)

.PHONY: dbt-test
dbt-test: ## Run the dbt data tests
	$(DBT) test $(DBT_FLAGS)

.PHONY: airflow-up
airflow-up: guard-docker ## Start Airflow (webserver on :8082, LocalExecutor)
	$(COMPOSE) --profile airflow up -d
	@printf '\n  Airflow  http://localhost:8082  (autotwin/autotwin)\n\n'

.PHONY: airflow-down
airflow-down: guard-docker ## Stop Airflow, leaving the rest of the stack running
	$(COMPOSE) --profile airflow rm --stop --force airflow-webserver airflow-scheduler airflow-init

##@ Routing (optional local OSRM)

.PHONY: osrm-prepare
osrm-prepare: guard-docker ## Download Hessen + Baden-Württemberg and build the OSRM graph
	@printf '\n  Building the routing graph in %s.\n' '$(OSRM_DIR)'
	@printf '  About 1.5 GB of downloads and 5-15 minutes of CPU. Re-runs are incremental.\n\n'
	@mkdir -p $(OSRM_DIR)
	@for region in $(OSRM_REGIONS); do \
		if [ -f "$(OSRM_DIR)/$$region.osm.pbf" ]; then \
			printf '  have   %s.osm.pbf\n' "$$region"; \
		else \
			printf '  fetch  %s.osm.pbf\n' "$$region"; \
			curl --fail --location --progress-bar \
				-o "$(OSRM_DIR)/$$region.osm.pbf" \
				"$(GEOFABRIK)/$$region-latest.osm.pbf"; \
		fi; \
	done
# osrm-extract takes a single file, and Geofabrik publishes no combined south-west extract,
# so the two Bundesländer are merged first. osmium-tool comes from the host when it is
# installed and from an official Debian image otherwise — no third-party image involved.
	@if [ -f "$(OSRM_DIR)/$(OSRM_NAME).osm.pbf" ]; then \
		printf '  have   %s.osm.pbf (merged)\n' '$(OSRM_NAME)'; \
	elif command -v osmium >/dev/null 2>&1; then \
		printf '  merge  %s.osm.pbf (host osmium)\n' '$(OSRM_NAME)'; \
		osmium merge $(OSRM_PBF_HOST) -o "$(OSRM_DIR)/$(OSRM_NAME).osm.pbf"; \
	else \
		printf '  merge  %s.osm.pbf (osmium-tool in debian:bookworm-slim)\n' '$(OSRM_NAME)'; \
		$(OSRM_RUN) debian:bookworm-slim sh -c \
			'apt-get update -qq && apt-get install -y -qq --no-install-recommends osmium-tool \
			 && osmium merge $(OSRM_PBF_CONT) -o /data/$(OSRM_NAME).osm.pbf'; \
	fi
# extract -> partition -> customize is the MLD pipeline and the expensive part (minutes of
# CPU and several GB of RAM), so it is skipped once the customised graph exists.
	@if [ -f "$(OSRM_DIR)/$(OSRM_NAME).osrm.mldgr" ]; then \
		printf '  have   routing graph — delete %s/*.osrm* to force a rebuild\n' '$(OSRM_DIR)'; \
	else \
		$(OSRM_RUN) $(OSRM_IMAGE) osrm-extract -p /opt/car.lua /data/$(OSRM_NAME).osm.pbf && \
		$(OSRM_RUN) $(OSRM_IMAGE) osrm-partition /data/$(OSRM_NAME).osrm && \
		$(OSRM_RUN) $(OSRM_IMAGE) osrm-customize /data/$(OSRM_NAME).osrm; \
	fi
	@printf '\n  Graph ready. Start it with \033[36mmake osrm-up\033[0m and set\n'
	@printf '  AUTOTWIN_OSRM_BASE_URL=http://localhost:5001 in .env.\n\n'

.PHONY: osrm-up
osrm-up: guard-docker ## Start the local OSRM server on :5001
	@test -f "$(OSRM_DIR)/$(OSRM_NAME).osrm.mldgr" || { \
		printf '  No routing graph yet — run \033[36mmake osrm-prepare\033[0m first.\n'; exit 1; \
	}
	$(COMPOSE) --profile routing up -d osrm
	@printf '\n  OSRM  http://localhost:5001/route/v1/driving/8.68,50.11;9.18,48.78?overview=false\n\n'
