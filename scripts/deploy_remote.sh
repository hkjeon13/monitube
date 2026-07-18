#!/usr/bin/env bash
set -Eeuo pipefail
set +x

# Run this script on the deployment host. It is intentionally fixed to the
# approved remote path and never copies, prints, or writes YOUTUBE_API_KEY.
readonly TARGET_DIR="/data/psyche/Projects/monitube"
readonly ENV_FILE="${TARGET_DIR}/.env"
readonly ENV_TEMPLATE="${TARGET_DIR}/.env.example"
readonly YOUTUBE_SECRET_ENV_FILE="${MONITUBE_YOUTUBE_SECRET_ENV_FILE:-/data/psyche/.config/monitube/youtube.env}"
readonly BACKUP_DIR="${MONITUBE_BACKUP_DIR:-/data/psyche/backups/monitube}"
readonly BRANCH="${MONITUBE_BRANCH:-main}"
readonly WORKER_REPLICAS="${MONITUBE_WORKER_REPLICAS:-2}"

log() {
  printf '[monitube deploy] %s\n' "$*"
}

die() {
  printf '[monitube deploy] error: %s\n' "$*" >&2
  exit 1
}

if [[ ! "$BRANCH" =~ ^[A-Za-z0-9._/-]+$ ]]; then
  die "MONITUBE_BRANCH contains unsupported characters."
fi
if [[ ! "$WORKER_REPLICAS" =~ ^[1-9][0-9]*$ ]]; then
  die "MONITUBE_WORKER_REPLICAS must be a positive integer."
fi

command -v docker >/dev/null 2>&1 || die "Docker is required on the deployment host."
docker compose version >/dev/null 2>&1 || die "Docker Compose v2 is required on the deployment host."

if [[ ! -d "$TARGET_DIR" ]]; then
  repo_url="${MONITUBE_REPO_URL:-}"
  [[ -n "$repo_url" ]] || die "Set MONITUBE_REPO_URL before the first clone."

  install -d -m 0750 "$(dirname "$TARGET_DIR")"
  # --quiet avoids echoing a remote URL. Do not embed credentials in the URL.
  git clone --quiet --branch "$BRANCH" --single-branch "$repo_url" "$TARGET_DIR"
elif [[ ! -d "${TARGET_DIR}/.git" ]]; then
  die "${TARGET_DIR} exists but is not a Git checkout; refusing to modify it."
else
  if [[ -n "$(git -C "$TARGET_DIR" status --porcelain)" ]]; then
    die "working tree is not clean; commit, stash, or resolve changes before deployment."
  fi

  git -C "$TARGET_DIR" fetch --quiet origin "$BRANCH"
  if git -C "$TARGET_DIR" show-ref --verify --quiet "refs/heads/${BRANCH}"; then
    git -C "$TARGET_DIR" checkout --quiet "$BRANCH"
    git -C "$TARGET_DIR" pull --ff-only --quiet origin "$BRANCH"
  else
    git -C "$TARGET_DIR" checkout --quiet --track -b "$BRANCH" "origin/$BRANCH"
  fi
fi

cd "$TARGET_DIR"
[[ -f "$ENV_TEMPLATE" ]] || die "missing committed .env.example template."
[[ -f scripts/apply_migrations.sh ]] || die "missing migration runner."

# The committed template is deliberately key-free. Refuse to create a remote
# config if that guarantee has been lost.
grep -qx 'YOUTUBE_API_KEY=' "$ENV_TEMPLATE" \
  || die ".env.example must retain a blank YOUTUBE_API_KEY line."

if [[ -L "$ENV_FILE" ]]; then
  die ".env must not be a symbolic link."
elif [[ ! -e "$ENV_FILE" ]]; then
  umask 077
  cp "$ENV_TEMPLATE" "$ENV_FILE"
  chmod 600 "$ENV_FILE"
  log "Created .env from the committed template; its YouTube key is intentionally blank."
elif [[ ! -f "$ENV_FILE" ]]; then
  die ".env exists but is not a regular file; refusing to replace it."
fi

# Keys must come from the deployment host's secret-injection mechanism as an
# external server-only file. They are never allowed in the repository .env.
grep -qx 'YOUTUBE_API_KEY=' "$ENV_FILE" \
  || die ".env must keep YOUTUBE_API_KEY blank; use the server-only secret env file instead."
chmod 600 "$ENV_FILE"

if [[ -L "$YOUTUBE_SECRET_ENV_FILE" ]]; then
  die "server secret env file must not be a symbolic link."
elif [[ -e "$YOUTUBE_SECRET_ENV_FILE" && ! -f "$YOUTUBE_SECRET_ENV_FILE" ]]; then
  die "server secret env file must be a regular file."
elif [[ ! -e "$YOUTUBE_SECRET_ENV_FILE" ]]; then
  install -d -m 0700 "$(dirname "$YOUTUBE_SECRET_ENV_FILE")"
  umask 077
  : > "$YOUTUBE_SECRET_ENV_FILE"
fi

# The file may be empty for fixture mode. It may carry the legacy single key or
# a comma-separated same-project key pool plus its server-only encryption and
# registration secrets; it is never printed by this script.
chmod 600 "$YOUTUBE_SECRET_ENV_FILE"
invalid_secret_lines="$(grep -cvE '^[[:space:]]*(#|$)|^YOUTUBE_API_KEY=[^[:space:]]*$|^YOUTUBE_API_KEYS=[^[:space:]]*$|^YOUTUBE_API_KEY_ENCRYPTION_KEY=[^[:space:]]*$|^YOUTUBE_KEY_REGISTRATION_TOKEN=[^[:space:]]*$' "$YOUTUBE_SECRET_ENV_FILE" || true)"
if [[ "$invalid_secret_lines" != "0" ]]; then
  die "server secret env file contains an unsupported entry."
fi

# Docker Compose loads this only into API and worker through the service-scoped
# env_file contract in docker-compose.yml.
export MONITUBE_YOUTUBE_SECRET_ENV_FILE="$YOUTUBE_SECRET_ENV_FILE"

compose() {
  docker compose -f docker-compose.yml "$@"
}

database_counts() {
  compose exec -T postgres sh -ceu '
    psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -At -c "
      SELECT concat_ws(
        '"'"'|'"'"',
        (SELECT count(*) FROM collection_sources),
        (SELECT count(*) FROM sync_jobs),
        (SELECT count(*) FROM channels),
        (SELECT count(*) FROM videos),
        (SELECT count(*) FROM comments)
      );
    "
  '
}

backup_database() {
  timestamp="$(date -u +%Y%m%dT%H%M%SZ)"
  install -d -m 0700 "$BACKUP_DIR"
  backup_path="$BACKUP_DIR/monitube-pre-migrate-${timestamp}.dump"

  # Stream a custom-format dump from the running PostgreSQL container without
  # reading or printing credentials. A failed/empty dump aborts before migration.
  compose exec -T postgres sh -ceu 'pg_dump -U "$POSTGRES_USER" -d "$POSTGRES_DB" --format=custom --no-owner --no-privileges' > "$backup_path"
  [[ -s "$backup_path" ]] || die "database backup is empty; refusing to migrate."
  chmod 600 "$backup_path"
  log "Created pre-migration database backup: $backup_path"
}

# This selects production process behavior without editing the key-free .env.
export APP_ENV=production

log "Building and starting infrastructure services."
compose up --build --detach postgres redis minio minio-init

# Freeze application writes while the target backfill and its unique constraints
# are installed. Running jobs retain their leases/checkpoints and the recreated
# worker will reclaim them after its normal lease window.
log "Pausing API and collection worker for a consistent backup and migration."
compose stop api worker

log "Recording current database row counts: $(database_counts)"
backup_database

log "Applying committed database migrations."
compose run --rm --no-deps migrate
log "Database row counts after migrations: $(database_counts)"

log "Building application images."
compose build api worker web

log "Recreating API with the current server-managed credential."
compose up --detach --force-recreate --no-deps api

for attempt in $(seq 1 30); do
  if compose exec -T api python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=3)" >/dev/null 2>&1; then
    log "API health check passed."
    log "Recreating web and worker services."
    compose up --detach --force-recreate --no-deps --scale "worker=${WORKER_REPLICAS}" web worker
    compose ps
    exit 0
  fi
  sleep 2
done

die "API did not become healthy; inspect service status on the host without printing secrets."
