#!/bin/sh
# Restore one final logical dump into the empty production central `monitube`
# database. This is intentionally unusable until the API write fence and all
# three worker scale-downs are observable in the live cluster.
set -eu

dump_path="${1:?usage: $0 /absolute/path/to/final.pg_dump --confirm-writer-fenced}"
confirmation="${2:-}"
source_namespace="${SOURCE_NAMESPACE:-monitube-prod}"
source_pod="${SOURCE_POD:-monitube-postgres-0}"
target_namespace="${TARGET_NAMESPACE:-database}"
target_cluster="${TARGET_CLUSTER:-central-pg-data}"
target_database="${TARGET_DATABASE:-monitube}"
parallel_jobs="${RESTORE_JOBS:-4}"

[ "$confirmation" = "--confirm-writer-fenced" ] || {
  echo "refusing production restore without --confirm-writer-fenced" >&2
  exit 2
}
case "$parallel_jobs" in
  ''|*[!0-9]*|0) echo "RESTORE_JOBS must be a positive integer" >&2; exit 2 ;;
esac
command -v kubectl >/dev/null 2>&1 || { echo "missing required command: kubectl" >&2; exit 2; }
command -v sha256sum >/dev/null 2>&1 || { echo "missing required command: sha256sum" >&2; exit 2; }
[ -f "$dump_path" ] || { echo "dump does not exist: $dump_path" >&2; exit 2; }

SOURCE_NAMESPACE="$source_namespace" SOURCE_POD="$source_pod" \
  ./scripts/cnpg_central_quiesce_verify.sh

target_primary="$(kubectl -n "$target_namespace" get cluster "$target_cluster" -o jsonpath='{.status.currentPrimary}')"
[ -n "$target_primary" ] || { echo "target cluster has no reported primary" >&2; exit 1; }
target_table_count="$(kubectl -n "$target_namespace" exec "$target_primary" -c postgres -- \
  psql -X -U postgres -d "$target_database" -Atc "SELECT count(*) FROM pg_tables WHERE schemaname = 'public'")"
[ "$target_table_count" = "0" ] || {
  echo "production target ${target_database} is not empty (${target_table_count} public tables); refusing restore" >&2
  exit 1
}

dump_sha="$(sha256sum "$dump_path" | awk '{print $1}')"
remote_dump="/var/lib/postgresql/data/.${target_database}.final.pg_dump"
remote_toc="/var/lib/postgresql/data/.${target_database}.final.toc"
remote_filtered_toc="/var/lib/postgresql/data/.${target_database}.final.filtered.toc"
cleanup_staging() {
  kubectl -n "$target_namespace" exec "$target_primary" -c postgres -- \
    rm -f "$remote_dump" "$remote_toc" "$remote_filtered_toc" >/dev/null 2>&1 || true
}
trap cleanup_staging EXIT HUP INT TERM

echo "final_dump_sha256=$dump_sha"
echo "restoring final dump into empty production target: ${target_namespace}/${target_cluster}/${target_database}"
echo "creating source-required extensions with central DBA authority"
kubectl -n "$target_namespace" exec "$target_primary" -c postgres -- \
  psql -X -v ON_ERROR_STOP=1 -U postgres -d "$target_database" -c \
  "CREATE EXTENSION IF NOT EXISTS pgcrypto; CREATE EXTENSION IF NOT EXISTS pg_trgm; CREATE EXTENSION IF NOT EXISTS pg_stat_statements;"
kubectl -n "$target_namespace" cp "$dump_path" "$target_primary:$remote_dump" -c postgres
kubectl -n "$target_namespace" exec "$target_primary" -c postgres -- sh -ec \
  "pg_restore --list '$remote_dump' > '$remote_toc'; grep -Ev 'EXTENSION (pg_stat_statements|pg_trgm|pgcrypto)' '$remote_toc' > '$remote_filtered_toc'"
kubectl -n "$target_namespace" exec "$target_primary" -c postgres -- \
  pg_restore -U postgres --role=monitube --no-owner --no-privileges --exit-on-error \
  --jobs="$parallel_jobs" --use-list="$remote_filtered_toc" --dbname="$target_database" "$remote_dump"
kubectl -n "$target_namespace" exec "$target_primary" -c postgres -- \
  vacuumdb -U postgres --analyze-in-stages --jobs="$parallel_jobs" "$target_database"

echo "final restore completed; run bounded parity before switching database.mode=central"
TARGET_DATABASE="$target_database" ./scripts/cnpg_central_parity.sh
