#!/bin/sh
# Read-only, bounded baseline comparison between the legacy and central DBs.
# Exit 20 means the two snapshots differ; no database objects are changed.
set -eu

source_namespace="${SOURCE_NAMESPACE:-monitube-prod}"
source_pod="${SOURCE_POD:-monitube-postgres-0}"
target_namespace="${TARGET_NAMESPACE:-database}"
target_cluster="${TARGET_CLUSTER:-central-pg-data}"
target_database="${TARGET_DATABASE:-monitube}"

require_command() {
  command -v "$1" >/dev/null 2>&1 || {
    echo "missing required command: $1" >&2
    exit 2
  }
}

require_command kubectl

target_primary="$(kubectl -n "$target_namespace" get cluster "$target_cluster" \
  -o jsonpath='{.status.currentPrimary}')"
[ -n "$target_primary" ] || {
  echo "target cluster has no reported primary: ${target_namespace}/${target_cluster}" >&2
  exit 1
}

metrics_sql='
  SELECT (SELECT count(*) FROM collection_sources),
         (SELECT count(*) FROM videos),
         (SELECT count(*) FROM comments),
         (SELECT count(*) FROM monitube_schema_migrations),
         (SELECT count(*) FROM nlp_documents),
         (SELECT count(*) FROM sync_jobs);
'

target_schema_present="$(kubectl -n "$target_namespace" exec "$target_primary" -c postgres -- \
  psql -X -U postgres -d "$target_database" -Atc \
  "SELECT to_regclass('public.monitube_schema_migrations') IS NOT NULL")"
if [ "$target_schema_present" != "t" ]; then
  echo 'Target schema is not restored. Do not change the application database endpoint.' >&2
  exit 20
fi

source_metrics="$(kubectl -n "$source_namespace" exec "$source_pod" -- sh -ec \
  "psql -X -U \"\$POSTGRES_USER\" -d \"\$POSTGRES_DB\" -Atc \"$metrics_sql\"")"
target_metrics="$(kubectl -n "$target_namespace" exec "$target_primary" -c postgres -- \
  psql -X -U postgres -d "$target_database" -Atc "$metrics_sql")"

printf '%s\n' "source=${source_metrics}"
printf '%s\n' "target=${target_metrics}"

if [ "$source_metrics" != "$target_metrics" ]; then
  echo 'Parity mismatch. Do not change the application database endpoint.' >&2
  exit 20
fi

echo 'Bounded parity baseline matches. This does not replace cutover-time fencing or full integrity checks.'
