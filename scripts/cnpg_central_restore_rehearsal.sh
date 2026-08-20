#!/bin/sh
# Restore one logical Monitube dump into an isolated central-CNPG rehearsal DB.
# This never drops a database and never touches the production target `monitube`.
set -eu

dump_path="${1:?usage: $0 /absolute/path/to/source.pg_dump [rehearsal_database]}"
target_namespace="${TARGET_NAMESPACE:-database}"
target_cluster="${TARGET_CLUSTER:-central-pg-data}"
target_database="${TARGET_DATABASE:-monitube}"
rehearsal_database="${2:-monitube_rehearsal_$(date -u +%Y%m%d_%H%M%S)}"
parallel_jobs="${RESTORE_JOBS:-4}"

case "$rehearsal_database" in
  monitube_rehearsal_[0-9][0-9][0-9][0-9][0-9][0-9][0-9][0-9]_[0-9][0-9][0-9][0-9][0-9][0-9]) ;;
  *) echo "rehearsal database must use monitube_rehearsal_YYYYMMDD_HHMMSS" >&2; exit 2 ;;
esac
command -v kubectl >/dev/null 2>&1 || { echo "missing required command: kubectl" >&2; exit 2; }
[ -f "$dump_path" ] || { echo "dump does not exist: $dump_path" >&2; exit 2; }

target_primary="$(kubectl -n "$target_namespace" get cluster "$target_cluster" -o jsonpath='{.status.currentPrimary}')"
[ -n "$target_primary" ] || { echo "target cluster has no reported primary" >&2; exit 1; }
production_table_count="$(kubectl -n "$target_namespace" exec "$target_primary" -c postgres -- psql -X -U postgres -d "$target_database" -Atc "SELECT count(*) FROM pg_tables WHERE schemaname = 'public'")"
[ "$production_table_count" = "0" ] || { echo "production target ${target_database} is not empty; rehearsal must not proceed" >&2; exit 1; }
exists="$(kubectl -n "$target_namespace" exec "$target_primary" -c postgres -- psql -X -U postgres -d postgres -Atc "SELECT EXISTS (SELECT 1 FROM pg_database WHERE datname = '$rehearsal_database')")"
[ "$exists" = "f" ] || { echo "rehearsal database already exists: $rehearsal_database" >&2; exit 1; }

remote_dump="/tmp/${rehearsal_database}.pg_dump"
cleanup() {
  kubectl -n "$target_namespace" exec "$target_primary" -c postgres -- rm -f "$remote_dump" >/dev/null 2>&1 || true
}
trap cleanup EXIT HUP INT TERM

echo "creating isolated rehearsal database: $rehearsal_database"
kubectl -n "$target_namespace" exec "$target_primary" -c postgres -- createdb -U postgres --owner=monitube "$rehearsal_database"
echo "copying dump to current central primary"
kubectl -n "$target_namespace" cp "$dump_path" "$target_primary:$remote_dump" -c postgres
echo "restoring with no owner/ACL replay and role=monitube"
kubectl -n "$target_namespace" exec "$target_primary" -c postgres -- pg_restore -U postgres --role=monitube --no-owner --no-privileges --exit-on-error --jobs="$parallel_jobs" --dbname="$rehearsal_database" "$remote_dump"
echo "analyzing restored database"
kubectl -n "$target_namespace" exec "$target_primary" -c postgres -- vacuumdb -U postgres --role=monitube --analyze-in-stages --jobs="$parallel_jobs" "$rehearsal_database"
kubectl -n "$target_namespace" exec "$target_primary" -c postgres -- psql -X -U postgres -d "$rehearsal_database" -Atc "SELECT current_database(); SELECT count(*) FROM pg_tables WHERE schemaname = 'public'; SELECT count(*) FROM monitube_schema_migrations; SELECT pg_size_pretty(pg_database_size(current_database()));"
echo "rehearsal restore completed: $rehearsal_database"
