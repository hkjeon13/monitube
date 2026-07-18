# Monitube

Monitube is a local-first scaffold for collecting YouTube channel, keyword-search, or direct-video results, then processing permitted video metadata and public comments. The service is designed to keep collection work durable: a worker can persist its checkpoint, wait for an allowed quota window, and resume without changing its assigned server runtime configuration.

The implementation plan and policy constraints live in [IMPLEMENTATION_PLAN.md](IMPLEMENTATION_PLAN.md).
The production database/read-path design and current cutover status live in
[DATABASE_PERFORMANCE_OPTIMIZATION_PLAN.md](docs/DATABASE_PERFORMANCE_OPTIMIZATION_PLAN.md).

## Current implementation boundary

This first vertical slice is runnable for the collection-console flow: it validates
channel/keyword/video inputs, creates sources and jobs, exposes the `waiting_quota` state,
and provides the PostgreSQL schema required by the durable design. When
`DATABASE_URL` is configured—as it is in Docker Compose—the API uses the
PostgreSQL repository, persists source/job state across API restarts, and bootstraps
only a secret reference and key fingerprint for the server-managed runtime
configuration. The in-memory repository remains an intentional fallback for isolated
tests or an API started without a database. The worker is a polling-collector runtime
backed by the same durable schema; it performs live collection only when a configured
adapter and server-managed key are available. The current MVP is centered on metadata
and public-comment collection; caption collection is out of scope. Exact counts
and timestamps come from bounded SQL/rollups, while top-word frequencies are
produced by a separately leased, bounded analysis worker; future
model/LLM-derived analytics are not performed by this scaffold. When no server-managed
`YOUTUBE_API_KEY` is injected, the API and worker intentionally remain in
fixture/no-op collection mode; this is a healthy, deployable state, not a
reason to prompt a user for a key.

## Local development

Requirements:

- Docker Desktop (or Docker Engine) with Docker Compose v2
- `make`
- Optional for host-mode development: Python 3.12+, [uv](https://docs.astral.sh/uv/), Node.js 22+, and npm

Start with containers; no host dependency installation is required:

```sh
cp .env.example .env
make up
make verify
```

The first build downloads Python and Node dependencies into container images.
`make` applies the local-development overlay, which bind-mounts source directories
and enables hot reload. If a dependency manifest changes, rebuild the relevant
image with `make build` (or rerun `make up`).

Local endpoints:

| Service | Address |
| --- | --- |
| Web | http://localhost:3000 |
| API | http://localhost:8000 |
| API health | http://localhost:8000/health |
| API readiness | http://localhost:8000/ready |
| PostgreSQL | `localhost:5432` |
| Redis | `localhost:6379` |
| MinIO S3 endpoint | http://localhost:9000 |
| MinIO Console | http://localhost:9001 |

Useful commands:

```sh
make logs SERVICE=api
make logs SERVICE=worker
make shell-api
make db-shell
make redis-cli
make down
```

`make reset-local` deletes the PostgreSQL, Redis, MinIO, and web dependency volumes. It is deliberately destructive and should only be used when local data may be discarded.

## Environment contract

Copy `.env.example` to `.env` and edit only local values. `.env` is ignored by Git.

| Variable group | Host-mode value | Container value | Purpose |
| --- | --- | --- | --- |
| `DATABASE_URL` / `DATABASE_URL_DOCKER` | `localhost` PostgreSQL URL | `postgres` PostgreSQL URL | API and worker database connection |
| `REDIS_URL` / `REDIS_URL_DOCKER` | `localhost` Redis URL | `redis` Redis URL | optional, reproducible derived-response cache |
| `S3_ENDPOINT_URL` / `S3_ENDPOINT_URL_DOCKER` | `localhost:9000` | `minio:9000` | analysis-artifact storage endpoint |
| `S3_ACCESS_KEY`, `S3_SECRET_KEY`, `S3_BUCKET` | same | same | local MinIO analysis-artifact bucket access |
| `*_BIND_ADDRESS` / `*_PORT` | `127.0.0.1` and local defaults | n/a | host binding and collision-safe published ports |
| `NEXT_PUBLIC_API_BASE_URL` | browser-reachable API URL | passed into the web build/runtime | public browser configuration only |
| `YOUTUBE_API_KEY` | optional local server secret | API/worker only | server-managed YouTube Data API access |
| `MONITUBE_WORKER_REPLICAS` | `2` | deployment script only | number of concurrent collection workers |
| `MONITUBE_ANALYSIS_WORKER_REPLICAS` | `1` | deployment script only | number of independent summary workers |
| `*_DB_POOL_*` | per-process values | same | bounded PostgreSQL connection-pool budgets |
| `ENABLE_*` performance flags | `false` by default | same | staged write/read/cache cutover controls |
| `ENABLE_DERIVED_ANALYTICS` | `false` by default | same | policy gate for future model-generated/derived analytics |

`NEXT_PUBLIC_*` values are intentionally browser-visible. Do not put API keys,
database URLs, or S3 secrets in them.

For fixture-only UI or API development, leave `YOUTUBE_API_KEY` blank. The
API/worker then run fixture/no-op collection mode and make no live YouTube request.
A live request requires a server-managed key with the appropriate API restriction;
users never provide a key or project. Local `.env` is a development convenience
only: in production, the backend receives the key through Secret Manager or another
secure deployment-time injection path, and PostgreSQL never stores the raw value.
Use a standard libpq-style `postgresql://...` URL; it is consumed directly by the
psycopg repository rather than through SQLAlchemy.

`ENABLE_DERIVED_ANALYTICS=false` does **not** disable the basic collection summary:
deterministic counts, timestamps, and top-word frequencies remain part of the core
MVP. The flag applies only to future model-generated or other policy-gated derived
analytics.

## Containers and build contracts

The root `docker-compose.yml` is production-shaped: it contains no source bind
mounts, runs the API without reload, builds the Next.js production bundle, and binds
all published ports to loopback by default. Local `make` commands layer
`infra/compose.dev.yaml` on top for hot reload.

The Compose stack builds three application images:

- `api` uses `infra/docker/api.Dockerfile`, installs `apps/api`, and starts `monitube_api.main:create_app` through Uvicorn with `--factory`.
- `worker` uses `infra/docker/worker.Dockerfile`, installs the API package for shared contracts, then starts `python -m monitube_worker.worker` with `apps/api` and `apps/worker` on `PYTHONPATH`.
- `analysis-worker` reuses the worker image but claims only bounded summary runs; it never competes for collection jobs.
- `web` uses `infra/docker/web.Dockerfile`, builds `apps/web` with npm, and starts the Next.js production server on port 3000.

The `migrate` one-shot service runs committed SQL migrations before API and worker
startup. API waits for PostgreSQL, optional Redis startup, MinIO bootstrap, and a
successful migration; worker and web wait for API readiness. Redis failure does not
fail API readiness because PostgreSQL remains the source of truth.

PostgreSQL receives `database/migrations/001_initial_schema.sql` only when its named
volume is first initialized. The `migrate` service records that initial baseline and
then applies `002_*.sql` and later migrations through `scripts/apply_migrations.sh`.
This makes a fresh volume receive both 001 and newer migrations, while an existing
deployment is upgraded without deleting its volume. Migrations must be
transaction-safe; a failed migration blocks application startup rather than allowing
a partially upgraded schema. `minio-init` creates the configured analysis-artifact
`S3_BUCKET` before API and worker services start.

## Host-mode development

Run infrastructure in containers and applications on the host when you need native tooling:

```sh
make env
make infra-up
cd apps/api && uv sync
cd ../web && npm install
```

Then use `make api`, `make worker`, or `make web` in separate terminals. Host-mode commands consume the `localhost` variants in `.env`; containerized services consume the `_DOCKER` variants.

Apply committed migrations to an existing local volume without deleting data:

```sh
make migrate
```

## Remote Docker Compose deployment

The checked-in deployment script is designed to run **on** the remote host and is
fixed to `/data/psyche/Projects/monitube`. It does not SSH anywhere and this project
does not perform remote changes automatically. The host needs Docker Engine with
Compose v2, Git, adequate disk space for image builds, and Git authentication that
does not embed credentials in a repository URL.

For a first deployment, place a reviewed copy of
[`scripts/deploy_remote.sh`](scripts/deploy_remote.sh) on the host through an approved
release/provisioning path. Give it the non-secret repository remote through
`MONITUBE_REPO_URL`; it clones the requested `MONITUBE_BRANCH` (default `main`) into
the fixed directory. Later deployments run the checked-out script directly. The
script refuses a dirty checkout, copies `.env.example` only when `.env` is missing,
sets restrictive file permissions, keeps `YOUTUBE_API_KEY=` blank in that repository
file, rejects example production credentials, checks disk headroom, verifies a
database backup before DB changes, applies expand-only migrations, recreates the
services, and verifies `/health` plus `/ready`. Operational procedures are in
[`scripts/runbooks`](scripts/runbooks).

Do not put a YouTube key in `.env`, a shell command, Git configuration, logs, or a
commit. The deployment host uses the regular, mode-`0600` file
`/data/psyche/.config/monitube/youtube.env` by default (override only with
`MONITUBE_YOUTUBE_SECRET_ENV_FILE`). It may contain one `YOUTUBE_API_KEY=...` entry
and is loaded only into API and worker; the deployment script validates it without
printing or sourcing the value. If the file is empty, deployment remains valid and
API/worker operate in fixture/no-op mode until live collection is intentionally
enabled.

### Remote ports and reverse proxy

All published ports default to `127.0.0.1`, so they do not expose PostgreSQL, Redis,
or MinIO to the network. On a shared host, edit only the non-secret port settings in
the remote `.env` before deployment—for example, choose unused `API_PORT`,
`WEB_PORT`, `POSTGRES_PORT`, `REDIS_PORT`, `MINIO_API_PORT`, and
`MINIO_CONSOLE_PORT` values. Keep database/cache/object-store bind addresses on
loopback. The Next web server exposes a same-origin `/api/*` rewrite to its internal
API target, so a public tunnel or reverse proxy only needs to publish the web service.

For a public deployment, set `NEXT_PUBLIC_API_BASE_URL=/api` before the web build.
The browser then never calls a server-local address: Next proxies `/api/v1/...` to the
private API container. `NEXT_PUBLIC_*` values are compiled into the Next.js bundle;
never put credentials in them. Set `CORS_ORIGINS` to the public web origin if direct
API access is also intentionally enabled.

For an existing remote volume, the deployment script invokes the same migration
runner as `make migrate`. It never runs `down --volumes`, so it will not reset
PostgreSQL, Redis, or MinIO data. If a migration fails, resolve the migration and rerun
the deployment; do not delete the volume to bypass the failure.

## Credential and quota boundary

API keys are server-managed secrets, not user input or a quota pool. The application keeps each job bound to an internal runtime configuration. A `quotaExceeded` response pauses the job, records its checkpoint and quota state in PostgreSQL, and waits for an allowed reset or an officially approved quota increase. It must not fall back to another Google account, API project, or key to bypass quota limits.

Same-project key replacement is permitted only for credential hygiene (for example, a compromised or retired secret); it does not increase quota. See the implementation plan for the full retention, server-side key-management, and YouTube policy requirements.
