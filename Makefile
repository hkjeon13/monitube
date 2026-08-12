LOCAL_COMPOSE ?= docker compose -f docker-compose.yml -f infra/compose.dev.yaml
COMPOSE ?= $(LOCAL_COMPOSE)
SERVICE ?=

.DEFAULT_GOAL := help

.PHONY: help env build build-rust infra-up up down restart ps logs migrate api api-rust-shadow tokenizer worker rollup-backfill-rust web shell-api db-shell redis-cli minio-console check check-rust check-tokenizer verify reset-local

help: ## Show available local-development commands.
	@awk 'BEGIN {FS = ":.*##"} /^[a-zA-Z0-9_-]+:.*##/ {printf "%-16s %s\n", $$1, $$2}' $(MAKEFILE_LIST)

env: ## Create .env from .env.example if it does not exist.
	@test -f .env || cp .env.example .env

build: env ## Build API, tokenizer, worker, and web images.
	$(COMPOSE) build api tokenizer worker web

build-rust: env ## Build shadow API, production Rust services, and maintenance image.
	$(COMPOSE) --profile rust-migration --profile rust-production --profile rust-maintenance build api-rust-shadow api-rust nlp-worker-rust collection-worker-rust analysis-worker-rust maintenance-rust

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

api-rust-shadow: env ## Run the Rust API locally on the shadow port.
	@set -a; . ./.env; set +a; RUST_API_HOST=127.0.0.1 RUST_API_PORT="$${RUST_API_SHADOW_PORT:-18001}" cargo run --package monitube-api-rust

tokenizer: ## Run the internal tokenizer API on the host.
	uv run --directory apps/tokenizer --no-sync uvicorn monitube_tokenizer.main:app --host 127.0.0.1 --port "$${TOKENIZER_PORT:-8010}" --reload

worker: env ## Run the worker on the host after the API dependencies are installed.
	@set -a; . ./.env; set +a; PYTHONPATH=apps/api:apps/worker uv run --project apps/api --no-sync python -m monitube_worker.worker

rollup-backfill-rust: env ## Run the resumable Rust comment-rollup maintenance command.
	$(COMPOSE) --profile rust-maintenance run --rm maintenance-rust rollup-backfill

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

check-rust: ## Run formatting, lint, and unit tests for the Rust workspace.
	cargo fmt --all --check
	cargo clippy --workspace --all-targets --all-features -- -D warnings
	cargo test --workspace --all-targets

check-tokenizer: ## Run the isolated tokenizer contract and parity tests.
	uv run --directory apps/tokenizer --extra dev pytest

check: check-rust check-tokenizer ## Run API tests and verify the production web build.
	uv run --project apps/api --extra dev ruff check apps/api/monitube_api apps/api/tests --select F821
	uv run --directory apps/api --extra dev pytest
	cd apps/web && npm run typecheck
	cd apps/web && npm run build

verify: ## Show service status and probe the API health endpoint.
	@set -a; . ./.env 2>/dev/null || true; set +a; $(COMPOSE) ps
	@set -a; . ./.env 2>/dev/null || true; set +a; curl --fail --silent --show-error http://localhost:$${API_PORT:-8000}/health

reset-local: ## DESTRUCTIVE: stop services and delete all local Docker volumes.
	$(COMPOSE) down --volumes --remove-orphans
