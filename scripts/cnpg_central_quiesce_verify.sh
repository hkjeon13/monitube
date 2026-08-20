#!/bin/sh
# Prove that the legacy writer path is quiescent before the final central
# restore. It only reads Kubernetes metadata and the source database.
set -eu

source_namespace="${SOURCE_NAMESPACE:-monitube-prod}"
source_pod="${SOURCE_POD:-monitube-postgres-0}"
stability_seconds="${QUIESCE_STABILITY_SECONDS:-60}"

case "$stability_seconds" in
  ''|*[!0-9]*) echo "QUIESCE_STABILITY_SECONDS must be a non-negative integer" >&2; exit 2 ;;
esac
command -v kubectl >/dev/null 2>&1 || { echo "missing required command: kubectl" >&2; exit 2; }
command -v sleep >/dev/null 2>&1 || { echo "missing required command: sleep" >&2; exit 2; }

source_sql() {
  kubectl -n "$source_namespace" exec "$source_pod" -- sh -ec \
    'psql -X -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d "$POSTGRES_DB" -Atc "$1"' sh "$1"
}

api_fence="$(kubectl -n "$source_namespace" get deploy monitube-api \
  -o jsonpath='{.spec.template.spec.containers[0].env[?(@.name=="MONITUBE_MAINTENANCE_READ_ONLY")].value}')"
[ "$api_fence" = "true" ] || {
  echo "API write fence is not deployed; expected MONITUBE_MAINTENANCE_READ_ONLY=true" >&2
  exit 1
}
api_available="$(kubectl -n "$source_namespace" get deploy monitube-api -o jsonpath='{.status.availableReplicas}')"
[ "$api_available" = "1" ] || { echo "API fence deployment is not available" >&2; exit 1; }

for deployment in monitube-collection-worker monitube-nlp-worker monitube-analysis-worker; do
  replicas="$(kubectl -n "$source_namespace" get deploy "$deployment" -o jsonpath='{.spec.replicas}')"
  [ "$replicas" = "0" ] || {
    echo "worker is not scaled down: ${deployment} replicas=${replicas}" >&2
    exit 1
  }
done

# One compact snapshot is sufficient to prove both the stable source LSN and
# the product-critical count boundary without exposing any source content.
snapshot_sql="
  SELECT pg_current_wal_lsn();
  SELECT (SELECT count(*) FROM collection_sources),
         (SELECT count(*) FROM channels),
         (SELECT count(*) FROM videos),
         (SELECT count(*) FROM comments),
         (SELECT count(*) FROM monitube_schema_migrations),
         (SELECT count(*) FROM nlp_documents),
         (SELECT count(*) FROM sync_jobs),
         (SELECT count(*) FROM outbox_events);
  SELECT count(*) FROM pg_stat_activity
   WHERE datname = current_database() AND pid <> pg_backend_pid()
     AND backend_type = 'client backend' AND state <> 'idle';
  SELECT count(*) FROM pg_locks WHERE NOT granted;
  SELECT count(*) FROM sync_jobs
   WHERE (state = 'running' AND (lease_expires_at IS NULL OR lease_expires_at > now()))
      OR (state <> 'running' AND lease_expires_at > now());"

first="$(source_sql "$snapshot_sql")"
quiesce_tail="$(printf '%s\n' "$first" | tail -3)"
expected_quiesce='0
0
0'
[ "$quiesce_tail" = "$expected_quiesce" ] || {
  echo "source has active clients, waiting locks, or active leases: ${quiesce_tail}" >&2
  exit 1
}

if [ "$stability_seconds" -gt 0 ]; then
  sleep "$stability_seconds"
fi
second="$(source_sql "$snapshot_sql")"
[ "$first" = "$second" ] || {
  echo "source changed during the ${stability_seconds}s quiesce interval" >&2
  exit 1
}

echo "writer_quiesce_verified=true"
echo "stability_seconds=$stability_seconds"
echo "source_snapshot=$first"
stale_running_jobs="$(source_sql "SELECT count(*) FROM sync_jobs
  WHERE state = 'running' AND lease_expires_at IS NOT NULL AND lease_expires_at <= now()")"
echo "source_stale_running_jobs=$stale_running_jobs"
