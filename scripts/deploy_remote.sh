#!/usr/bin/env bash
set -Eeuo pipefail
set +x

# Run on the deployment host. The fixed paths and service-scoped secret file
# prevent repository configuration from becoming a credential store.
readonly TARGET_DIR="/data/psyche/Projects/monitube"
readonly ENV_FILE="${TARGET_DIR}/.env"
readonly ENV_TEMPLATE="${TARGET_DIR}/.env.example"
readonly YOUTUBE_SECRET_ENV_FILE="${MONITUBE_YOUTUBE_SECRET_ENV_FILE:-/data/psyche/.config/monitube/youtube.env}"
readonly BACKUP_DIR="${MONITUBE_BACKUP_DIR:-/data/psyche/backups/monitube}"
readonly BRANCH="${MONITUBE_BRANCH:-main}"
WORKER_REPLICAS="${MONITUBE_WORKER_REPLICAS:-}"
NLP_WORKER_REPLICAS="${MONITUBE_NLP_WORKER_REPLICAS:-}"
ANALYSIS_WORKER_REPLICAS="${MONITUBE_ANALYSIS_WORKER_REPLICAS:-}"
readonly RUN_DEPLOY_CHECKS="${MONITUBE_RUN_DEPLOY_CHECKS:-true}"
readonly PROMOTE_SAFE_FLAGS="${MONITUBE_PROMOTE_SAFE_FLAGS:-true}"
readonly RESET_QUERY_STATS="${MONITUBE_RESET_PG_STAT_STATEMENTS:-false}"
readonly MIN_FREE_DISK_GB="${MONITUBE_MIN_FREE_DISK_GB:-10}"
readonly CUTOVER_SOAK_SECONDS="${MONITUBE_CUTOVER_SOAK_SECONDS:-60}"

log() {
  printf '[monitube deploy] %s\n' "$*"
}

warn() {
  printf '[monitube deploy] warning: %s\n' "$*" >&2
}

die() {
  printf '[monitube deploy] error: %s\n' "$*" >&2
  exit 1
}

validate_boolean() {
  case "$2" in
    true|false) ;;
    *) die "$1 must be true or false." ;;
  esac
}

[[ "$BRANCH" =~ ^[A-Za-z0-9._/-]+$ ]] || die "MONITUBE_BRANCH contains unsupported characters."
[[ "$MIN_FREE_DISK_GB" =~ ^[1-9][0-9]*$ ]] || die "MONITUBE_MIN_FREE_DISK_GB must be a positive integer."
[[ "$CUTOVER_SOAK_SECONDS" =~ ^[0-9]+$ ]] || die "MONITUBE_CUTOVER_SOAK_SECONDS must be a non-negative integer."
validate_boolean MONITUBE_RUN_DEPLOY_CHECKS "$RUN_DEPLOY_CHECKS"
validate_boolean MONITUBE_PROMOTE_SAFE_FLAGS "$PROMOTE_SAFE_FLAGS"
validate_boolean MONITUBE_RESET_PG_STAT_STATEMENTS "$RESET_QUERY_STATS"

command -v docker >/dev/null 2>&1 || die "Docker is required on the deployment host."
command -v git >/dev/null 2>&1 || die "Git is required on the deployment host."
command -v sha256sum >/dev/null 2>&1 || die "sha256sum is required on the deployment host."
docker compose version >/dev/null 2>&1 || die "Docker Compose v2 is required on the deployment host."

pre_update_sha=""
if [[ ! -d "$TARGET_DIR" ]]; then
  repo_url="${MONITUBE_REPO_URL:-}"
  [[ -n "$repo_url" ]] || die "Set MONITUBE_REPO_URL before the first clone."
  install -d -m 0750 "$(dirname "$TARGET_DIR")"
  git clone --quiet --branch "$BRANCH" --single-branch "$repo_url" "$TARGET_DIR"
elif [[ ! -d "${TARGET_DIR}/.git" ]]; then
  die "${TARGET_DIR} exists but is not a Git checkout; refusing to modify it."
else
  [[ -z "$(git -C "$TARGET_DIR" status --porcelain)" ]] || die "working tree is not clean; commit, stash, or resolve changes before deployment."
  pre_update_sha="$(git -C "$TARGET_DIR" rev-parse HEAD)"
  git -C "$TARGET_DIR" fetch --quiet origin "$BRANCH"
  if git -C "$TARGET_DIR" show-ref --verify --quiet "refs/heads/${BRANCH}"; then
    git -C "$TARGET_DIR" checkout --quiet "$BRANCH"
    git -C "$TARGET_DIR" pull --ff-only --quiet origin "$BRANCH"
  else
    git -C "$TARGET_DIR" checkout --quiet --track -b "$BRANCH" "origin/$BRANCH"
  fi
fi

current_sha="$(git -C "$TARGET_DIR" rev-parse HEAD)"
previous_sha="${MONITUBE_PREVIOUS_SHA_OVERRIDE:-$pre_update_sha}"

# A deploy can update this script itself. Re-exec once so the checked-out
# release, rather than the pre-pull shell process, controls the deployment.
if [[ "${MONITUBE_DEPLOY_REEXEC:-false}" != "true" ]]; then
  reexec_environment=(env MONITUBE_DEPLOY_REEXEC=true "MONITUBE_PREVIOUS_SHA_OVERRIDE=$previous_sha")
  exec "${reexec_environment[@]}" "$TARGET_DIR/scripts/deploy_remote.sh"
fi

readonly CURRENT_SHA="$current_sha"
readonly PREVIOUS_SHA="$previous_sha"
readonly DEPLOY_TIMESTAMP="$(date -u +%Y%m%dT%H%M%SZ)"
readonly RELEASE_STATE_DIR="${BACKUP_DIR}/releases/${DEPLOY_TIMESTAMP}-${CURRENT_SHA:0:12}"
readonly ROLLBACK_IMAGE_TAG="rollback-${DEPLOY_TIMESTAMP}-${CURRENT_SHA:0:12}"

cd "$TARGET_DIR"
[[ -f "$ENV_TEMPLATE" ]] || die "missing committed .env.example template."
[[ -f scripts/apply_migrations.sh ]] || die "missing migration runner."
for repository_secret_name in YOUTUBE_API_KEY YOUTUBE_API_KEYS YOUTUBE_API_KEY_ENCRYPTION_KEY YOUTUBE_KEY_REGISTRATION_TOKEN SEARCH_API_KEY; do
  grep -qx "${repository_secret_name}=" "$ENV_TEMPLATE" || die ".env.example must keep ${repository_secret_name} blank."
done

if [[ -L "$ENV_FILE" ]]; then
  die ".env must not be a symbolic link."
elif [[ ! -e "$ENV_FILE" ]]; then
  umask 077
  cp "$ENV_TEMPLATE" "$ENV_FILE"
  chmod 600 "$ENV_FILE"
  log "Created .env from the committed template. Replace example production credentials before continuing."
elif [[ ! -f "$ENV_FILE" ]]; then
  die ".env exists but is not a regular file; refusing to replace it."
fi

for repository_secret_name in YOUTUBE_API_KEY YOUTUBE_API_KEYS YOUTUBE_API_KEY_ENCRYPTION_KEY YOUTUBE_KEY_REGISTRATION_TOKEN SEARCH_API_KEY; do
  if ! grep -qE "^${repository_secret_name}=" "$ENV_FILE"; then
    printf '\n%s=\n' "$repository_secret_name" >> "$ENV_FILE"
  fi
  grep -qx "${repository_secret_name}=" "$ENV_FILE" || die ".env must keep ${repository_secret_name} blank; use the server-only secret env file instead."
done
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

chmod 600 "$YOUTUBE_SECRET_ENV_FILE"
invalid_secret_lines="$(grep -cvE '^[[:space:]]*(#|$)|^YOUTUBE_API_KEY=[^[:space:]]*$|^YOUTUBE_API_KEYS=[^[:space:]]*$|^YOUTUBE_API_KEY_ENCRYPTION_KEY=[^[:space:]]*$|^YOUTUBE_KEY_REGISTRATION_TOKEN=[^[:space:]]*$|^SEARCH_API_KEY=[^[:space:]]*$' "$YOUTUBE_SECRET_ENV_FILE" || true)"
[[ "$invalid_secret_lines" == "0" ]] || die "server secret env file contains an unsupported entry."

secret_value() {
  awk -F= -v key="$1" '$1 == key { sub(/^[^=]*=/, ""); value=$0 } END { print value }' "$YOUTUBE_SECRET_ENV_FILE"
}

youtube_key_secret="$(secret_value YOUTUBE_API_KEY)"
youtube_keys_secret="$(secret_value YOUTUBE_API_KEYS)"
encryption_key_secret="$(secret_value YOUTUBE_API_KEY_ENCRYPTION_KEY)"
registration_token_secret="$(secret_value YOUTUBE_KEY_REGISTRATION_TOKEN)"
search_api_key_secret="$(secret_value SEARCH_API_KEY)"
[[ -n "$youtube_key_secret" || -n "$youtube_keys_secret" ]] || die "server secret env file must provide YOUTUBE_API_KEY or YOUTUBE_API_KEYS."
[[ -n "$encryption_key_secret" ]] || die "server secret env file must provide YOUTUBE_API_KEY_ENCRYPTION_KEY."
[[ -n "$registration_token_secret" ]] || die "server secret env file must provide YOUTUBE_KEY_REGISTRATION_TOKEN."
[[ -n "$search_api_key_secret" ]] || die "server secret env file must provide SEARCH_API_KEY."
unset youtube_key_secret youtube_keys_secret encryption_key_secret registration_token_secret search_api_key_secret

dotenv_has_key() {
  grep -qE "^$1=" "$ENV_FILE"
}

dotenv_value() {
  awk -F= -v key="$1" '$1 == key { sub(/^[^=]*=/, ""); value=$0 } END { print value }' "$ENV_FILE"
}

if [[ -z "$WORKER_REPLICAS" ]]; then
  WORKER_REPLICAS="$(dotenv_value MONITUBE_WORKER_REPLICAS)"
  WORKER_REPLICAS="${WORKER_REPLICAS:-2}"
fi
if [[ -z "$ANALYSIS_WORKER_REPLICAS" ]]; then
  ANALYSIS_WORKER_REPLICAS="$(dotenv_value MONITUBE_ANALYSIS_WORKER_REPLICAS)"
  ANALYSIS_WORKER_REPLICAS="${ANALYSIS_WORKER_REPLICAS:-1}"
fi
if [[ -z "$NLP_WORKER_REPLICAS" ]]; then
  NLP_WORKER_REPLICAS="$(dotenv_value MONITUBE_NLP_WORKER_REPLICAS)"
  NLP_WORKER_REPLICAS="${NLP_WORKER_REPLICAS:-1}"
fi
[[ "$WORKER_REPLICAS" =~ ^[1-9][0-9]*$ ]] || die "MONITUBE_WORKER_REPLICAS must be a positive integer."
[[ "$NLP_WORKER_REPLICAS" =~ ^[1-9][0-9]*$ ]] || die "MONITUBE_NLP_WORKER_REPLICAS must be a positive integer."
[[ "$ANALYSIS_WORKER_REPLICAS" =~ ^[1-9][0-9]*$ ]] || die "MONITUBE_ANALYSIS_WORKER_REPLICAS must be a positive integer."
readonly WORKER_REPLICAS NLP_WORKER_REPLICAS ANALYSIS_WORKER_REPLICAS

ensure_env_setting() {
  local key="$1"
  local value="$2"
  if ! dotenv_has_key "$key"; then
    printf '\n%s=%s\n' "$key" "$value" >> "$ENV_FILE"
  fi
}

set_env_setting() {
  local key="$1"
  local value="$2"
  local temporary_file
  temporary_file="$(mktemp "${TARGET_DIR}/.env.deploy.XXXXXX")"
  chmod 600 "$temporary_file"
  awk -v key="$key" -v value="$value" 'BEGIN { found=0 } index($0, key "=") == 1 { print key "=" value; found=1; next } { print } END { if (!found) print key "=" value }' "$ENV_FILE" > "$temporary_file"
  mv "$temporary_file" "$ENV_FILE"
  chmod 600 "$ENV_FILE"
}

require_loopback_binding() {
  local key="$1"
  local fallback="$2"
  local value
  value="$(dotenv_value "$key")"
  value="${value:-$fallback}"
  case "$value" in
    127.0.0.1|localhost|::1) ;;
    *) die "$key must remain loopback-bound in production." ;;
  esac
}

# Reviewed initial candidates for the 62 GiB production host. An explicit
# remote .env value always wins.
ensure_env_setting POSTGRES_SHARED_PRELOAD_LIBRARIES pg_stat_statements
ensure_env_setting POSTGRES_SHARED_BUFFERS 4GB
ensure_env_setting POSTGRES_EFFECTIVE_CACHE_SIZE 32GB
ensure_env_setting POSTGRES_WORK_MEM 8MB
ensure_env_setting POSTGRES_MAINTENANCE_WORK_MEM 512MB
ensure_env_setting POSTGRES_AUTOVACUUM_WORK_MEM 256MB
ensure_env_setting POSTGRES_MAX_CONNECTIONS 60
ensure_env_setting POSTGRES_RANDOM_PAGE_COST 1.1
ensure_env_setting POSTGRES_EFFECTIVE_IO_CONCURRENCY 200
ensure_env_setting POSTGRES_MAX_WAL_SIZE 8GB
ensure_env_setting POSTGRES_CHECKPOINT_COMPLETION_TARGET 0.9
ensure_env_setting POSTGRES_WAL_COMPRESSION on
ensure_env_setting POSTGRES_TRACK_IO_TIMING on
ensure_env_setting POSTGRES_IDLE_IN_TRANSACTION_SESSION_TIMEOUT 30s
ensure_env_setting POSTGRES_LOG_MIN_DURATION_STATEMENT 500ms
ensure_env_setting POSTGRES_LOG_TEMP_FILES 64MB
ensure_env_setting POSTGRES_LOG_PARAMETER_MAX_LENGTH 0
ensure_env_setting POSTGRES_LOG_PARAMETER_MAX_LENGTH_ON_ERROR 0
ensure_env_setting POSTGRES_SHM_SIZE 1gb
ensure_env_setting REDIS_APPENDONLY no
ensure_env_setting REDIS_MAXMEMORY 512mb
ensure_env_setting REDIS_MAXMEMORY_POLICY allkeys-lru
ensure_env_setting DISCOVERY_PROVIDER searchapi
ensure_env_setting SEARCHAPI_BASE_URL https://www.searchapi.io/api/v1/search
ensure_env_setting SEARCHAPI_TIMEOUT_SECONDS 20
ensure_env_setting SEARCHAPI_GL kr
ensure_env_setting SEARCHAPI_HL ko
ensure_env_setting SEARCHAPI_ZERO_RETENTION false
ensure_env_setting SEARCHAPI_CHANNEL_TOKEN_POST_THRESHOLD_BYTES 1800
ensure_env_setting TRANSCRIPT_COLLECTION_ENABLED true
ensure_env_setting TRANSCRIPT_PRIMARY_LANGUAGE ko
ensure_env_setting TRANSCRIPT_FALLBACK_LANGUAGE en
ensure_env_setting TRANSCRIPT_TYPE_PREFERENCE manual
ensure_env_setting TRANSCRIPT_MAX_SEGMENTS 100000
ensure_env_setting MONITUBE_WEB_API_PROXY_TARGET_DOCKER http://api:8000

performance_flags=(
  ENABLE_SOURCE_OVERVIEW_V2
  ENABLE_TARGET_SUMMARY_WRITE
  ENABLE_TARGET_SUMMARY_READ
  ENABLE_ANALYSIS_WORKER
  ENABLE_VIDEO_KEYSET_PAGINATION
  ENABLE_COMMENT_BATCH_WRITE
  ENABLE_COMMENT_ROLLUP_DUAL_WRITE
  ENABLE_COMMENT_ROLLUP_READ
  ENABLE_EXPLORE_ROLLUP
  ENABLE_SEARCH_TRIGRAM
  ENABLE_REDIS_DERIVED_CACHE
)
for feature_flag in "${performance_flags[@]}"; do
  ensure_env_setting "$feature_flag" false
done

postgres_password_config="$(dotenv_value POSTGRES_PASSWORD)"
minio_password_config="$(dotenv_value MINIO_ROOT_PASSWORD)"
s3_secret_config="$(dotenv_value S3_SECRET_KEY)"
database_url_config="$(dotenv_value DATABASE_URL_DOCKER)"
[[ -n "$postgres_password_config" && "$postgres_password_config" != "change-me-local-only" ]] || die "replace the example PostgreSQL password in the server .env before production deployment."
[[ -n "$minio_password_config" && "$minio_password_config" != "change-me-minio-local-only" ]] || die "replace the example MinIO root password in the server .env before production deployment."
[[ -n "$s3_secret_config" && "$s3_secret_config" != "change-me-minio-local-only" ]] || die "replace the example S3 secret in the server .env before production deployment."
[[ -n "$database_url_config" && "$database_url_config" != *change-me-local-only* ]] || die "replace the example DATABASE_URL_DOCKER credential in the server .env before production deployment."
unset postgres_password_config minio_password_config s3_secret_config database_url_config
require_loopback_binding POSTGRES_BIND_ADDRESS 127.0.0.1
require_loopback_binding REDIS_BIND_ADDRESS 127.0.0.1
require_loopback_binding MINIO_BIND_ADDRESS 127.0.0.1
require_loopback_binding MINIO_CONSOLE_BIND_ADDRESS 127.0.0.1
require_loopback_binding API_BIND_ADDRESS 127.0.0.1
require_loopback_binding RUST_API_BIND_ADDRESS 127.0.0.1
require_loopback_binding WEB_BIND_ADDRESS 127.0.0.1

export MONITUBE_YOUTUBE_SECRET_ENV_FILE="$YOUTUBE_SECRET_ENV_FILE"
export MONITUBE_IMAGE_TAG="$CURRENT_SHA"
export APP_ENV=production

compose() {
  docker compose --project-directory "$TARGET_DIR" --env-file "$ENV_FILE" \
    --profile rust-migration --profile rust-production --profile rust-maintenance \
    -f "$TARGET_DIR/docker-compose.yml" "$@"
}

previous_compose() {
  docker compose --project-directory "$TARGET_DIR" --env-file "$ENV_FILE" \
    --profile rust-migration --profile rust-production --profile rust-maintenance \
    -f "$RELEASE_STATE_DIR/docker-compose.previous.yml" "$@"
}

previous_postgres_compose() {
  docker compose --project-directory "$TARGET_DIR" --env-file "$ENV_FILE" -f "$RELEASE_STATE_DIR/docker-compose.previous.yml" -f "$RELEASE_STATE_DIR/postgres-runtime.previous.yml" "$@"
}

compose config --quiet
install -d -m 0700 "$BACKUP_DIR" "$RELEASE_STATE_DIR"

for feature_flag in "${performance_flags[@]}"; do
  printf '%s=%s\n' "$feature_flag" "$(dotenv_value "$feature_flag")" >> "$RELEASE_STATE_DIR/feature-flags.previous.env"
done
chmod 600 "$RELEASE_STATE_DIR/feature-flags.previous.env"
printf 'MONITUBE_WEB_API_PROXY_TARGET_DOCKER=%s\n' \
  "$(dotenv_value MONITUBE_WEB_API_PROXY_TARGET_DOCKER)" \
  > "$RELEASE_STATE_DIR/runtime-settings.previous.env"
chmod 600 "$RELEASE_STATE_DIR/runtime-settings.previous.env"

ensure_disk_headroom() {
  local path="$1"
  local used_percent
  local available_kb
  used_percent="$(df -Pk "$path" | awk 'NR == 2 { gsub(/%/, "", $5); print $5 }')"
  available_kb="$(df -Pk "$path" | awk 'NR == 2 { print $4 }')"
  [[ "$used_percent" =~ ^[0-9]+$ ]] || die "could not determine disk use for $path."
  [[ "$available_kb" =~ ^[0-9]+$ ]] || die "could not determine free disk for $path."
  (( used_percent < 90 )) || die "disk containing $path is ${used_percent}% full; deployment gate is below 90%."
  (( available_kb >= MIN_FREE_DISK_GB * 1024 * 1024 )) || die "disk containing $path has less than ${MIN_FREE_DISK_GB} GiB free."
}

ensure_disk_headroom "$TARGET_DIR"
ensure_disk_headroom "$BACKUP_DIR"
docker_root_dir="$(docker info --format '{{.DockerRootDir}}')"
[[ -n "$docker_root_dir" ]] || die "could not determine Docker's data root."
ensure_disk_headroom "$(dirname "$docker_root_dir")"

service_container() {
  compose ps -q "$1" 2>/dev/null | awk 'NR == 1 { print; exit }'
}

container_is_running() {
  [[ -n "$1" ]] && [[ "$(docker inspect --format '{{.State.Running}}' "$1" 2>/dev/null || true)" == "true" ]]
}

wait_for_postgres() {
  local attempt
  for attempt in $(seq 1 60); do
    if compose exec -T postgres sh -ceu 'pg_isready -U "$POSTGRES_USER" -d "$POSTGRES_DB"' >/dev/null 2>&1; then
      return 0
    fi
    sleep 2
  done
  return 1
}

postgres_container="$(service_container postgres)"
database_preexisting=false
if [[ -n "$postgres_container" ]]; then
  database_preexisting=true
  if ! container_is_running "$postgres_container"; then
    log "Starting the existing PostgreSQL container without recreating its configuration."
    compose start postgres
  fi
  wait_for_postgres || die "existing PostgreSQL did not become ready."
fi

count_running_replicas() {
  local service="$1"
  local count=0
  local container_id
  for container_id in $(compose ps -q "$service" 2>/dev/null); do
    if container_is_running "$container_id"; then
      count=$((count + 1))
    fi
  done
  printf '%s\n' "$count"
}

previous_api_running="$(count_running_replicas api)"
previous_web_running="$(count_running_replicas web)"
previous_tokenizer_running="$(count_running_replicas tokenizer)"
previous_worker_replicas="$(count_running_replicas worker)"
previous_analysis_replicas="$(count_running_replicas analysis-worker)"
previous_rust_api_replicas="$(count_running_replicas api-rust)"
previous_rust_nlp_replicas="$(count_running_replicas nlp-worker-rust)"
previous_rust_collection_replicas="$(count_running_replicas collection-worker-rust)"
previous_rust_analysis_replicas="$(count_running_replicas analysis-worker-rust)"

if [[ -n "$PREVIOUS_SHA" ]] && git cat-file -e "${PREVIOUS_SHA}:docker-compose.yml" 2>/dev/null; then
  git show "${PREVIOUS_SHA}:docker-compose.yml" > "$RELEASE_STATE_DIR/docker-compose.previous.yml"
else
  cp "$TARGET_DIR/docker-compose.yml" "$RELEASE_STATE_DIR/docker-compose.previous.yml"
fi
chmod 600 "$RELEASE_STATE_DIR/docker-compose.previous.yml"

tag_previous_service_image() {
  local service="$1"
  local repository="$2"
  local container_id
  local image_id
  container_id="$(service_container "$service")"
  [[ -n "$container_id" ]] || return 0
  image_id="$(docker inspect --format '{{.Image}}' "$container_id")"
  docker image tag "$image_id" "${repository}:${ROLLBACK_IMAGE_TAG}"
  printf '%s=%s\n' "$service" "$image_id" >> "$RELEASE_STATE_DIR/application-images.previous"
}

tag_previous_service_image api monitube-api
tag_previous_service_image web monitube-web
tag_previous_service_image worker monitube-worker
tag_previous_service_image tokenizer monitube-tokenizer
tag_previous_service_image api-rust monitube-api-rust
tag_previous_service_image nlp-worker-rust monitube-nlp-worker-rust
tag_previous_service_image collection-worker-rust monitube-collection-worker-rust
tag_previous_service_image analysis-worker-rust monitube-analysis-worker-rust
[[ -s "$RELEASE_STATE_DIR/application-images.previous" ]] || : > "$RELEASE_STATE_DIR/application-images.previous"
chmod 600 "$RELEASE_STATE_DIR/application-images.previous"

if [[ "$database_preexisting" == "true" ]]; then
  postgres_command_json="$(docker inspect --format '{{json .Config.Cmd}}' "$postgres_container")"
  postgres_shm_size="$(docker inspect --format '{{.HostConfig.ShmSize}}' "$postgres_container")"
  printf 'services:\n  postgres:\n    command: %s\n    shm_size: %s\n' "$postgres_command_json" "$postgres_shm_size" > "$RELEASE_STATE_DIR/postgres-runtime.previous.yml"
  chmod 600 "$RELEASE_STATE_DIR/postgres-runtime.previous.yml"
  compose exec -T postgres sh -ceu 'psql -X -U "$POSTGRES_USER" -d "$POSTGRES_DB" -At -F= -c "SELECT name, setting FROM pg_settings WHERE name IN ('"'"'shared_preload_libraries'"'"', '"'"'shared_buffers'"'"', '"'"'effective_cache_size'"'"', '"'"'work_mem'"'"', '"'"'maintenance_work_mem'"'"', '"'"'autovacuum_work_mem'"'"', '"'"'max_connections'"'"', '"'"'random_page_cost'"'"', '"'"'effective_io_concurrency'"'"', '"'"'max_wal_size'"'"', '"'"'checkpoint_completion_target'"'"', '"'"'wal_compression'"'"', '"'"'track_io_timing'"'"', '"'"'idle_in_transaction_session_timeout'"'"', '"'"'log_min_duration_statement'"'"', '"'"'log_temp_files'"'"', '"'"'log_parameter_max_length'"'"', '"'"'log_parameter_max_length_on_error'"'"') ORDER BY name;"' > "$RELEASE_STATE_DIR/postgres-settings.previous"
  chmod 600 "$RELEASE_STATE_DIR/postgres-settings.previous"
else
  printf 'services:\n  postgres:\n    command: null\n' > "$RELEASE_STATE_DIR/postgres-runtime.previous.yml"
  chmod 600 "$RELEASE_STATE_DIR/postgres-runtime.previous.yml"
fi

printf 'previous_sha=%s\ncurrent_sha=%s\ndeployed_at=%s\nrollback_image_tag=%s\n' "$PREVIOUS_SHA" "$CURRENT_SHA" "$DEPLOY_TIMESTAMP" "$ROLLBACK_IMAGE_TAG" > "$RELEASE_STATE_DIR/release.env"
chmod 600 "$RELEASE_STATE_DIR/release.env"

database_counts() {
  compose exec -T postgres sh -ceu 'psql -X -U "$POSTGRES_USER" -d "$POSTGRES_DB" -At -c "SELECT concat_ws('"'"'|'"'"', (SELECT count(*) FROM collection_sources), (SELECT count(*) FROM sync_jobs), (SELECT count(*) FROM channels), (SELECT count(*) FROM videos), (SELECT count(*) FROM comments));"'
}

backup_database() {
  local backup_path
  local checksum_path
  local list_path
  backup_path="$BACKUP_DIR/monitube-pre-change-${DEPLOY_TIMESTAMP}.dump"
  checksum_path="${backup_path}.sha256"
  list_path="${backup_path}.list"

  # Custom-format pg_dump uses one consistent MVCC snapshot while the old
  # application is still serving. No application credential is printed.
  compose exec -T postgres sh -ceu 'pg_dump -U "$POSTGRES_USER" -d "$POSTGRES_DB" --format=custom --no-owner --no-privileges' > "$backup_path"
  [[ -s "$backup_path" ]] || die "database backup is empty; refusing to continue."
  chmod 600 "$backup_path"

  sha256sum "$backup_path" > "$checksum_path"
  chmod 600 "$checksum_path"
  sha256sum --check "$checksum_path" >/dev/null || die "database backup checksum verification failed."

  compose exec -T postgres pg_restore --list < "$backup_path" > "$list_path"
  [[ -s "$list_path" ]] || die "pg_restore could not list the database backup."
  chmod 600 "$list_path"

  printf '%s\n' "$backup_path" > "$RELEASE_STATE_DIR/database-backup.path"
  chmod 600 "$RELEASE_STATE_DIR/database-backup.path"
  log "Created and verified pre-change database backup: $backup_path"
}

postgres_config_changed() {
  local current_hash
  local desired_hash
  local hash_output
  [[ "$database_preexisting" == "true" ]] || return 0
  current_hash="$(docker inspect --format '{{ index .Config.Labels "com.docker.compose.config-hash" }}' "$postgres_container" 2>/dev/null || true)"
  hash_output="$(compose config --hash postgres 2>/dev/null || true)"
  desired_hash="$(printf '%s\n' "$hash_output" | awk 'NF { print $NF; exit }')"

  # Older Compose versions do not expose config --hash. Conservatively use the
  # DB-change path rather than recreate PostgreSQL before taking a backup.
  if [[ -n "$current_hash" && -n "$desired_hash" && "$current_hash" == "$desired_hash" ]]; then
    return 1
  fi
  return 0
}

migration_pending=false
migration_check_log="$RELEASE_STATE_DIR/migration-check.log"
postgres_runtime_change=false
if [[ "$database_preexisting" == "true" ]]; then
  if compose run --rm --no-deps migrate --check > "$migration_check_log" 2>&1; then
    migration_pending=false
  else
    migration_check_status=$?
    if [[ "$migration_check_status" == "10" ]]; then
      migration_pending=true
    else
      die "migration preflight failed; inspect $migration_check_log on the host."
    fi
  fi
  chmod 600 "$migration_check_log"
  if postgres_config_changed; then
    postgres_runtime_change=true
  fi
fi

database_change_required=false
if [[ "$database_preexisting" != "true" || "$migration_pending" == "true" || "$postgres_runtime_change" == "true" ]]; then
  database_change_required=true
fi

if [[ "$database_preexisting" == "true" ]]; then
  database_counts > "$RELEASE_STATE_DIR/database-counts.before"
  chmod 600 "$RELEASE_STATE_DIR/database-counts.before"
  log "Taking a verified database backup before the Rust ownership cutover."
  backup_database
else
  log "No existing database was found; the new instance will be initialized from committed migrations."
fi

log "Building immutable Python rollback, tokenizer, Rust, maintenance, and web images before pausing writes."
MONITUBE_WEB_API_PROXY_TARGET_DOCKER=http://api-rust:8001 \
  compose build api worker tokenizer api-rust-shadow api-rust nlp-worker-rust collection-worker-rust analysis-worker-rust maintenance-rust web
ensure_disk_headroom "$TARGET_DIR"

if [[ "$RUN_DEPLOY_CHECKS" == "true" ]]; then
  log "Running image-level import and bytecode checks before pausing writes."
  compose run --rm --no-deps -e PYTHONPYCACHEPREFIX=/tmp/monitube-pycache \
    api python -m compileall -q /workspace/apps/api
  compose run --rm --no-deps -e PYTHONPYCACHEPREFIX=/tmp/monitube-pycache \
    tokenizer python -m compileall -q /workspace/apps/tokenizer
  compose run --rm --no-deps tokenizer python -c "from monitube_tokenizer.analyzer import MecabNltkNounAnalyzer, analyzer_health; analyzer_health(MecabNltkNounAnalyzer())"
  compose run --rm --no-deps -e PYTHONPYCACHEPREFIX=/tmp/monitube-pycache \
    worker python -m compileall -q /workspace/apps/api /workspace/apps/worker
  for rust_service in api-rust nlp-worker-rust collection-worker-rust analysis-worker-rust maintenance-rust; do
    case "$rust_service" in
      api-rust) rust_repository=monitube-api-rust ;;
      nlp-worker-rust) rust_repository=monitube-nlp-worker-rust ;;
      collection-worker-rust) rust_repository=monitube-collection-worker-rust ;;
      analysis-worker-rust) rust_repository=monitube-analysis-worker-rust ;;
      maintenance-rust) rust_repository=monitube-maintenance-rust ;;
      *) die "no immutable image mapping exists for ${rust_service}." ;;
    esac
    rust_image="${rust_repository}:${CURRENT_SHA}"
    docker image inspect "$rust_image" >/dev/null 2>&1 || die "${rust_service} image was not built as ${rust_image}."
    rust_user="$(docker image inspect --format '{{.Config.User}}' "$rust_image")"
    [[ -n "$rust_user" && "$rust_user" != "0" && "$rust_user" != "root" ]] || die "${rust_service} image must run as a non-root user."
  done
fi

ROLLBACK_ARMED=false
POSTGRES_CONFIG_APPLIED=false
DEPLOY_SUCCEEDED=false

restore_performance_flags() {
  local feature_flag
  local previous_value
  for feature_flag in "${performance_flags[@]}"; do
    previous_value="$(awk -F= -v key="$feature_flag" '$1 == key { sub(/^[^=]*=/, ""); print; exit }' "$RELEASE_STATE_DIR/feature-flags.previous.env")"
    case "$previous_value" in
      true|false) set_env_setting "$feature_flag" "$previous_value" ;;
      *) set_env_setting "$feature_flag" false ;;
    esac
  done
}

restore_runtime_settings() {
  local previous_proxy_target
  previous_proxy_target="$(awk -F= '$1 == "MONITUBE_WEB_API_PROXY_TARGET_DOCKER" { sub(/^[^=]*=/, ""); print; exit }' "$RELEASE_STATE_DIR/runtime-settings.previous.env")"
  set_env_setting MONITUBE_WEB_API_PROXY_TARGET_DOCKER "${previous_proxy_target:-http://api:8000}"
}

wait_for_python_api_path() {
  local path="$1"
  local attempt
  for attempt in $(seq 1 45); do
    if compose exec -T api python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000${path}', timeout=3)" >/dev/null 2>&1; then
      return 0
    fi
    sleep 2
  done
  return 1
}

wait_for_rust_api_path() {
  local service="$1"
  local path="$2"
  local attempt
  for attempt in $(seq 1 45); do
    if compose exec -T "$service" curl --fail --silent "http://127.0.0.1:8001${path}" >/dev/null 2>&1; then
      return 0
    fi
    sleep 2
  done
  return 1
}

wait_for_previous_api_health() {
  local attempt
  for attempt in $(seq 1 30); do
    if previous_compose exec -T api python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=3)" >/dev/null 2>&1; then
      return 0
    fi
    sleep 2
  done
  return 1
}

wait_for_previous_rust_api_health() {
  local attempt
  for attempt in $(seq 1 30); do
    if previous_compose exec -T api-rust curl --fail --silent http://127.0.0.1:8001/ready >/dev/null 2>&1; then
      return 0
    fi
    sleep 2
  done
  return 1
}

wait_for_web() {
  local attempt
  for attempt in $(seq 1 30); do
    if compose exec -T web node -e "fetch('http://127.0.0.1:3000').then((response) => { if (!response.ok) process.exit(1) })" >/dev/null 2>&1; then
      return 0
    fi
    sleep 2
  done
  return 1
}

wait_for_web_api() {
  local attempt
  for attempt in $(seq 1 30); do
    if compose exec -T web node -e "fetch('http://127.0.0.1:3000/api/ready').then((response) => { if (!response.ok) process.exit(1) })" >/dev/null 2>&1; then
      return 0
    fi
    sleep 2
  done
  return 1
}

wait_for_tokenizer() {
  local attempt
  for attempt in $(seq 1 30); do
    if compose exec -T tokenizer python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8010/ready', timeout=3)" >/dev/null 2>&1; then
      return 0
    fi
    sleep 2
  done
  return 1
}

rollback_deployment() {
  local rollback_failed=false
  set +e
  warn "Deployment failed after cutover began; attempting application and PostgreSQL-config rollback."
  restore_performance_flags
  restore_runtime_settings
  export MONITUBE_IMAGE_TAG="$ROLLBACK_IMAGE_TAG"

  compose stop api-rust api-rust-shadow nlp-worker-rust collection-worker-rust analysis-worker-rust

  if [[ "$POSTGRES_CONFIG_APPLIED" == "true" && "$database_preexisting" == "true" ]]; then
    warn "Restoring the previous PostgreSQL command and shared-memory configuration."
    previous_postgres_compose up --detach --force-recreate --no-deps postgres
    wait_for_postgres || rollback_failed=true
  fi
  if (( previous_rust_api_replicas > 0 )); then
    previous_compose up --detach --force-recreate --no-deps api-rust
    wait_for_previous_rust_api_health || rollback_failed=true
  elif (( previous_api_running > 0 )); then
    previous_compose up --detach --force-recreate --no-deps api
    wait_for_previous_api_health || rollback_failed=true
  fi
  if (( previous_web_running > 0 )); then
    previous_compose up --detach --force-recreate --no-deps web
  fi
  if (( previous_tokenizer_running > 0 )); then
    previous_compose up --detach --force-recreate --no-deps tokenizer
  fi
  if (( previous_worker_replicas > 0 )); then
    previous_compose up --detach --force-recreate --no-deps --scale "worker=${previous_worker_replicas}" worker
  fi
  if (( previous_analysis_replicas > 0 )); then
    previous_compose up --detach --force-recreate --no-deps --scale "analysis-worker=${previous_analysis_replicas}" analysis-worker
  fi
  if (( previous_rust_nlp_replicas > 0 )); then
    previous_compose up --detach --force-recreate --no-deps --scale "nlp-worker-rust=${previous_rust_nlp_replicas}" nlp-worker-rust
  fi
  if (( previous_rust_collection_replicas > 0 )); then
    previous_compose up --detach --force-recreate --no-deps --scale "collection-worker-rust=${previous_rust_collection_replicas}" collection-worker-rust
  fi
  if (( previous_rust_analysis_replicas > 0 )); then
    previous_compose up --detach --force-recreate --no-deps --scale "analysis-worker-rust=${previous_rust_analysis_replicas}" analysis-worker-rust
  fi
  if (( previous_tokenizer_running == 0 )); then
    compose stop tokenizer
  fi

  if [[ "$rollback_failed" == "true" ]]; then
    warn "Automatic rollback was incomplete. Use scripts/runbooks/deployment-rollback.md and $RELEASE_STATE_DIR."
  else
    warn "Previous application services were restored. Expand-only migrations were intentionally retained."
  fi
  set -e
}

deployment_exit() {
  local status=$?
  if [[ "$status" -ne 0 && "$ROLLBACK_ARMED" == "true" && "$DEPLOY_SUCCEEDED" != "true" ]]; then
    rollback_deployment
  fi
  exit "$status"
}
trap deployment_exit EXIT

if [[ "$database_preexisting" != "true" ]]; then
  log "Starting a new PostgreSQL instance with the reviewed runtime configuration."
  compose up --detach --no-deps postgres
  wait_for_postgres || die "new PostgreSQL instance did not become ready."
  postgres_container="$(service_container postgres)"
  POSTGRES_CONFIG_APPLIED=true
elif [[ "$database_change_required" == "true" ]]; then
  ROLLBACK_ARMED=true
  log "Gracefully pausing API and workers before migration or configuration cutover."
  compose stop api worker analysis-worker api-rust api-rust-shadow \
    nlp-worker-rust collection-worker-rust analysis-worker-rust

  if [[ "$postgres_runtime_change" == "true" ]]; then
    log "Recreating PostgreSQL with the reviewed command and shm_size."
    POSTGRES_CONFIG_APPLIED=true
    compose up --detach --force-recreate --no-deps postgres
    wait_for_postgres || die "PostgreSQL did not become ready with the new runtime configuration."
    postgres_container="$(service_container postgres)"
  fi

  # Apply large indexes only after the new maintenance_work_mem, I/O timing,
  # preload library, and shared-memory configuration are active.
  if [[ "$migration_pending" == "true" ]]; then
    log "Applying committed expand-only database migrations."
    compose run --rm --no-deps migrate
  fi
fi

# New databases need all migrations. A post-config run also confirms the
# pg_stat_statements extension after PostgreSQL has loaded the library.
if [[ "$database_preexisting" != "true" || "$POSTGRES_CONFIG_APPLIED" == "true" ]]; then
  compose run --rm --no-deps migrate
fi

compose run --rm --no-deps migrate --check > "$RELEASE_STATE_DIR/migration-check.after.log"
chmod 600 "$RELEASE_STATE_DIR/migration-check.after.log"

postgres_observability_ready="$(compose exec -T postgres sh -ceu 'psql -X -U "$POSTGRES_USER" -d "$POSTGRES_DB" -At -c "SELECT position('"'"'pg_stat_statements'"'"' in current_setting('"'"'shared_preload_libraries'"'"')) > 0 AND EXISTS (SELECT 1 FROM pg_extension WHERE extname = '"'"'pg_stat_statements'"'"') AND current_setting('"'"'track_io_timing'"'"') = '"'"'on'"'"';"')"
[[ "$postgres_observability_ready" == "t" ]] || die "PostgreSQL observability bootstrap is incomplete."

if [[ "$RESET_QUERY_STATS" == "true" ]]; then
  compose exec -T postgres sh -ceu 'psql -X -U "$POSTGRES_USER" -d "$POSTGRES_DB" -q -c "SELECT pg_stat_statements_reset();"' >/dev/null
  log "Reset pg_stat_statements at the explicit operator request."
fi
printf 'measurement_started_at=%s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" > "$RELEASE_STATE_DIR/observability-baseline.env"
chmod 600 "$RELEASE_STATE_DIR/observability-baseline.env"

database_boolean() {
  compose exec -T postgres sh -ceu 'psql -X -U "$POSTGRES_USER" -d "$POSTGRES_DB" -At' <<< "$1"
}

foundation_schema_ready="$(database_boolean 'SELECT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='"'"'public'"'"' AND table_name='"'"'collection_targets'"'"' AND column_name='"'"'data_version'"'"') AND EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='"'"'public'"'"' AND table_name='"'"'analysis_runs'"'"' AND column_name='"'"'target_id'"'"') AND EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='"'"'public'"'"' AND table_name='"'"'analysis_runs'"'"' AND column_name='"'"'data_version'"'"') AND EXISTS (SELECT 1 FROM monitube_schema_migrations WHERE filename='"'"'023_pure_frequency_ranking.sql'"'"') AND to_regclass('"'"'public.nlp_term_stats_frequency_rank_idx'"'"') IS NOT NULL AND to_regclass('"'"'public.video_comment_rollups'"'"') IS NOT NULL AND to_regclass('"'"'public.maintenance_backfills'"'"') IS NOT NULL AND to_regclass('"'"'public.video_search_documents'"'"') IS NOT NULL;')"

if [[ "$database_preexisting" == "true" ]]; then
  ROLLBACK_ARMED=true
fi

if [[ "$PROMOTE_SAFE_FLAGS" == "true" && "$foundation_schema_ready" == "t" ]]; then
  log "Foundation schema validation passed; promoting the safe write and API feature subset."
  set_env_setting ENABLE_SOURCE_OVERVIEW_V2 true
  set_env_setting ENABLE_TARGET_SUMMARY_WRITE true
  set_env_setting ENABLE_ANALYSIS_WORKER true
  set_env_setting ENABLE_VIDEO_KEYSET_PAGINATION true
  set_env_setting ENABLE_COMMENT_BATCH_WRITE true
  set_env_setting ENABLE_COMMENT_ROLLUP_DUAL_WRITE true

  summary_read_ready="$(database_boolean 'SELECT NOT EXISTS (SELECT 1 FROM collection_targets target WHERE EXISTS (SELECT 1 FROM collection_sources source WHERE source.target_id = target.id) AND NOT EXISTS (SELECT 1 FROM analysis_runs run JOIN analysis_results result ON result.analysis_run_id = run.id WHERE run.target_id = target.id AND run.data_version = target.data_version AND run.pipeline_version = '"'"'deterministic-v3'"'"' AND run.state = '"'"'completed'"'"' AND result.result_kind = '"'"'basic_summary'"'"' AND result.deleted_at IS NULL AND (result.expires_at IS NULL OR result.expires_at > now())));')"
  if [[ "$summary_read_ready" == "t" ]]; then
    set_env_setting ENABLE_TARGET_SUMMARY_READ true
  else
    set_env_setting ENABLE_TARGET_SUMMARY_READ false
    log "Target summaries are still backfilling; summary read remains disabled."
  fi

  search_ready="$(database_boolean 'SELECT (SELECT count(*) FROM video_search_documents) = (SELECT count(*) FROM videos) AND to_regclass('"'"'public.video_search_documents_trgm_idx'"'"') IS NOT NULL AND to_regclass('"'"'public.comments_text_display_trgm_idx'"'"') IS NOT NULL;')"
  if [[ "$search_ready" == "t" ]]; then
    set_env_setting ENABLE_SEARCH_TRIGRAM true
  else
    set_env_setting ENABLE_SEARCH_TRIGRAM false
    log "Search backfill or index validation is incomplete; trigram reads remain disabled."
  fi

  # Reconciliation and cache gates are deliberately separate from the
  # foundation migration.
  set_env_setting ENABLE_COMMENT_ROLLUP_READ false
  set_env_setting ENABLE_EXPLORE_ROLLUP false
  set_env_setting ENABLE_REDIS_DERIVED_CACHE false
elif [[ "$PROMOTE_SAFE_FLAGS" == "true" ]]; then
  die "safe feature promotion was requested but foundation schema validation failed."
fi

database_counts > "$RELEASE_STATE_DIR/database-counts.after"
chmod 600 "$RELEASE_STATE_DIR/database-counts.after"

log "Ensuring Redis, MinIO, and the artifact bucket are ready."
compose up --detach --no-deps redis minio
compose run --rm --no-deps minio-init

ROLLBACK_ARMED=true
log "Starting the isolated tokenizer and Rust shadow API while current traffic remains authoritative."
compose up --detach --force-recreate --no-deps tokenizer
wait_for_tokenizer || die "tokenizer readiness check failed."
compose up --detach --force-recreate --no-deps api-rust-shadow
wait_for_rust_api_path api-rust-shadow /health || die "Rust shadow API liveness check failed."
wait_for_rust_api_path api-rust-shadow /ready || die "Rust shadow API readiness check failed."
shadow_auth_status="$(compose exec -T api-rust-shadow sh -ceu "curl --silent --output /dev/null --write-out '%{http_code}' http://127.0.0.1:8001/v1/auth/me")"
[[ "$shadow_auth_status" == "401" ]] || die "Rust shadow API authentication boundary returned ${shadow_auth_status}, expected 401."

log "Stopping every queue consumer before transferring lease ownership to Rust."
compose stop worker analysis-worker nlp-worker-rust collection-worker-rust analysis-worker-rust
[[ "$(count_running_replicas worker)" == "0" ]] || die "Python collection workers did not stop."
[[ "$(count_running_replicas analysis-worker)" == "0" ]] || die "Python analysis workers did not stop."
[[ "$(count_running_replicas nlp-worker-rust)" == "0" ]] || die "previous Rust NLP workers did not stop."
[[ "$(count_running_replicas collection-worker-rust)" == "0" ]] || die "previous Rust collection workers did not stop."
[[ "$(count_running_replicas analysis-worker-rust)" == "0" ]] || die "previous Rust analysis workers did not stop."

log "Requeueing leases left by stopped processes at idempotent checkpoints."
compose exec -T postgres sh -ceu 'psql -X -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d "$POSTGRES_DB"' <<'SQL'
BEGIN;
UPDATE sync_jobs
SET state = 'queued', current_stage = 'queued_after_runtime_cutover',
    lease_owner = NULL, lease_expires_at = NULL,
    retry_at = NULL, resume_at = NULL, updated_at = now()
WHERE state = 'running';
UPDATE analysis_runs
SET state = 'queued', lease_owner = NULL, lease_expires_at = NULL,
    resume_at = now(), last_error = 'requeued_after_runtime_cutover'
WHERE state = 'running';
UPDATE nlp_documents
SET state = 'pending', lease_owner = NULL, lease_expires_at = NULL,
    updated_at = now(), error_code = NULL
WHERE state = 'running';
COMMIT;
SQL

set_env_setting ENABLE_COMMENT_ROLLUP_DUAL_WRITE true
log "Running resumable Rust rollup maintenance against the quiescent write path."
compose run --rm --no-deps maintenance-rust rollup-backfill
rollup_ready="$(database_boolean 'SELECT EXISTS (SELECT 1 FROM maintenance_backfills WHERE name='"'"'video_comment_rollups'"'"' AND state='"'"'ready'"'"' AND processed = total);')"
[[ "$rollup_ready" == "t" ]] || die "Rust rollup maintenance did not reach a reconciled ready state."

log "Starting Rust NLP, collection, and analysis workers with exclusive queue ownership."
compose up --detach --force-recreate --no-deps \
  --scale "nlp-worker-rust=${NLP_WORKER_REPLICAS}" \
  --scale "collection-worker-rust=${WORKER_REPLICAS}" \
  --scale "analysis-worker-rust=${ANALYSIS_WORKER_REPLICAS}" \
  nlp-worker-rust collection-worker-rust analysis-worker-rust
for attempt in $(seq 1 45); do
  if [[ "$(count_running_replicas nlp-worker-rust)" == "$NLP_WORKER_REPLICAS" \
     && "$(count_running_replicas collection-worker-rust)" == "$WORKER_REPLICAS" \
     && "$(count_running_replicas analysis-worker-rust)" == "$ANALYSIS_WORKER_REPLICAS" ]]; then
    break
  fi
  sleep 2
done
[[ "$(count_running_replicas nlp-worker-rust)" == "$NLP_WORKER_REPLICAS" ]] || die "Rust NLP worker replica count did not reach ${NLP_WORKER_REPLICAS}."
[[ "$(count_running_replicas collection-worker-rust)" == "$WORKER_REPLICAS" ]] || die "Rust collection worker replica count did not reach ${WORKER_REPLICAS}."
[[ "$(count_running_replicas analysis-worker-rust)" == "$ANALYSIS_WORKER_REPLICAS" ]] || die "Rust analysis worker replica count did not reach ${ANALYSIS_WORKER_REPLICAS}."

log "Switching the public API port and web proxy from Python to Rust."
compose stop api api-rust
compose up --detach --force-recreate --no-deps api-rust
wait_for_rust_api_path api-rust /health || die "Rust production API liveness check failed."
wait_for_rust_api_path api-rust /ready || die "Rust production API readiness check failed."
set_env_setting MONITUBE_WEB_API_PROXY_TARGET_DOCKER http://api-rust:8001
compose up --detach --force-recreate --no-deps web
wait_for_web || die "web root smoke check failed."
wait_for_web_api || die "web-to-Rust-API proxy smoke check failed."
compose stop api-rust-shadow

[[ "$(count_running_replicas api)" == "0" ]] || die "Python API is still running after cutover."
[[ "$(count_running_replicas worker)" == "0" ]] || die "Python collection worker is still running after cutover."
[[ "$(count_running_replicas analysis-worker)" == "0" ]] || die "Python analysis worker is still running after cutover."

runtime_invariants="$(database_boolean 'SELECT
  NOT EXISTS (SELECT 1 FROM nlp_corpus_stats WHERE document_count < 0 OR total_token_count < 0)
  AND NOT EXISTS (SELECT 1 FROM nlp_term_stats WHERE document_frequency < 0 OR total_term_frequency < 0)
  AND NOT EXISTS (SELECT 1 FROM sync_jobs WHERE state='"'"'running'"'"' AND (lease_owner IS NULL OR lease_expires_at IS NULL))
  AND NOT EXISTS (SELECT 1 FROM nlp_documents WHERE state='"'"'running'"'"' AND (lease_owner IS NULL OR lease_expires_at IS NULL))
  AND NOT EXISTS (SELECT 1 FROM analysis_runs WHERE state='"'"'running'"'"' AND (lease_owner IS NULL OR lease_expires_at IS NULL));')"
[[ "$runtime_invariants" == "t" ]] || die "post-cutover lease or BoW counter invariants failed."

log "Soaking the Rust cutover for ${CUTOVER_SOAK_SECONDS}s while checking health and restarts."
soak_elapsed=0
while (( soak_elapsed < CUTOVER_SOAK_SECONDS )); do
  wait_for_rust_api_path api-rust /ready || die "Rust API failed during cutover soak."
  wait_for_web_api || die "web API proxy failed during cutover soak."
  for service in api-rust nlp-worker-rust collection-worker-rust analysis-worker-rust tokenizer web; do
    for container_id in $(compose ps -q "$service"); do
      restart_count="$(docker inspect --format '{{.RestartCount}}' "$container_id")"
      [[ "$restart_count" == "0" ]] || die "${service} restarted during cutover soak."
    done
  done
  remaining=$((CUTOVER_SOAK_SECONDS - soak_elapsed))
  interval=5
  (( remaining < interval )) && interval="$remaining"
  (( interval > 0 )) && sleep "$interval"
  soak_elapsed=$((soak_elapsed + interval))
done

docker stats --no-stream --format '{{.Name}}|{{.CPUPerc}}|{{.MemUsage}}|{{.MemPerc}}' \
  $(compose ps -q api-rust nlp-worker-rust collection-worker-rust analysis-worker-rust tokenizer web) \
  > "$RELEASE_STATE_DIR/container-stats.after.txt"
chmod 600 "$RELEASE_STATE_DIR/container-stats.after.txt"

compose ps
DEPLOY_SUCCEEDED=true
ROLLBACK_ARMED=false
log "Rust conversion deployment completed. Only the isolated tokenizer remains Python. Release state: $RELEASE_STATE_DIR"
