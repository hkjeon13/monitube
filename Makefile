LOCAL_COMPOSE ?= docker compose -f docker-compose.yml -f infra/compose.dev.yaml
COMPOSE ?= $(LOCAL_COMPOSE)
SERVICE ?=

.DEFAULT_GOAL := help

.PHONY: help env build infra-up up down restart ps logs migrate api worker web shell-api db-shell redis-cli minio-console verify reset-local

help: ## Show available local-development commands.
	@awk 'BEGIN {FS = ":.*##"} /^[a-zA-Z0-9_-]+:.*##/ {printf "%-16s %s\n", $$1, $$2}' $(MAKEFILE_LIST)

env: ## Create .env from .env.example if it does not exist.
	@test -f .env || cp .env.example .env

build: env ## Build API, worker, and web images (installs their container dependencies).
	$(COMPOSE) build api worker web

infra-up: env ## Start only local infrastructure and apply committed migrations.
	$(COMPOSE) up --build --detach postgres redis minio minio-init
	$(COMPOSE) run --rm --no-deps migrate

up: env ## Start the complete local stack in the background.
	$(COMPOSE) up --build --detach

down: ## Stop local services while retaining PostgreSQL, Redis, and MinIO data.
	$(COMPOSE) down --remove-orphans

restart: ## Restart the complete local stack.
	$(COMPOSE) restart

ps: ## Show service status.
	$(COMPOSE) ps

logs: ## Follow logs; set SERVICE=api, worker, or web to narrow the output.
	$(COMPOSE) logs --follow $(SERVICE)

migrate: env ## Apply committed SQL migrations without deleting local data.
	$(COMPOSE) up --detach postgres
	$(COMPOSE) run --rm --no-deps migrate

api: env ## Run the API on the host after `cd apps/api && uv sync`.
	@set -a; . ./.env; set +a; cd apps/api && uv run --no-sync uvicorn monitube_api.main:create_app --factory --host 0.0.0.0 --port "$${API_PORT:-8000}" --reload

worker: env ## Run the worker on the host after the API dependencies are installed.
	@set -a; . ./.env; set +a; PYTHONPATH=apps/api:apps/worker uv run --project apps/api --no-sync python -m monitube_worker.worker

web: env ## Run the Next.js app on the host after `cd apps/web && npm install`.
	@set -a; . ./.env; set +a; cd apps/web && npm run dev -- --hostname 0.0.0.0 --port "$${WEB_PORT:-3000}"

shell-api: ## Open a shell in the API container.
	$(COMPOSE) exec api /bin/sh

db-shell: ## Open psql against the local PostgreSQL container.
	$(COMPOSE) exec postgres psql -U $${POSTGRES_USER:-monitube} -d $${POSTGRES_DB:-monitube}

redis-cli: ## Open redis-cli against the local Redis container.
	$(COMPOSE) exec redis redis-cli

minio-console: ## Print the local MinIO Console address.
	@echo "http://localhost:$${MINIO_CONSOLE_PORT:-9001}"

verify: ## Show service status and probe the API health endpoint.
	@set -a; . ./.env 2>/dev/null || true; set +a; $(COMPOSE) ps
	@set -a; . ./.env 2>/dev/null || true; set +a; curl --fail --silent --show-error http://localhost:$${API_PORT:-8000}/health

reset-local: ## DESTRUCTIVE: stop services and delete all local Docker volumes.
	$(COMPOSE) down --volumes --remove-orphans
