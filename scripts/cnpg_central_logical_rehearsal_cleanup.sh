#!/bin/sh
# Remove one completed, isolated logical-replication rehearsal. This is
# deliberately separate from cnpg_central_logical_rehearsal.sh so successful
# evidence is retained until an operator has reviewed it.
set -eu

source_namespace="${SOURCE_NAMESPACE:-monitube-prod}"
source_pod="${SOURCE_POD:-monitube-postgres-0}"
target_namespace="${TARGET_NAMESPACE:-database}"
target_cluster="${TARGET_CLUSTER:-central-pg-data}"
rehearsal_database="${1:-}"
confirmation="${2:-}"

case "$rehearsal_database" in
  monitube_logical_rehearsal_[0-9][0-9][0-9][0-9][0-9][0-9][0-9][0-9]_[0-9][0-9][0-9][0-9][0-9][0-9]) ;;
  *) echo "rehearsal database must use monitube_logical_rehearsal_YYYYMMDD_HHMMSS" >&2; exit 2 ;;
esac
[ "$confirmation" = "--confirm" ] || {
  echo "refusing cleanup without --confirm; review the recorded subscription and slot evidence first" >&2
  exit 2
}
command -v kubectl >/dev/null 2>&1 || { echo "missing required command: kubectl" >&2; exit 2; }

source_sql() {
  kubectl -n "$source_namespace" exec "$source_pod" -- sh -ec \
    'psql -X -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d "$POSTGRES_DB" -Atc "$1"' sh "$1"
}

target_primary="$(kubectl -n "$target_namespace" get cluster "$target_cluster" -o jsonpath='{.status.currentPrimary}')"
[ -n "$target_primary" ] || { echo "target cluster has no reported primary" >&2; exit 1; }
suffix="${rehearsal_database#monitube_logical_rehearsal_}"
publication="monitube_central_rehearsal_${suffix}"
subscription="monitube_central_rehearsal_${suffix}"
slot_name="monitube_central_rehearsal_${suffix}"
replication_role="monitube_repl_rehearsal_${suffix}"

target_exists="$(kubectl -n "$target_namespace" exec "$target_primary" -c postgres -- \
  psql -X -U postgres -d postgres -Atc "SELECT EXISTS (SELECT 1 FROM pg_database WHERE datname = '$rehearsal_database')")"
[ "$target_exists" = "t" ] || { echo "isolated rehearsal database is absent: $rehearsal_database" >&2; exit 1; }
subscription_oid="$(kubectl -n "$target_namespace" exec "$target_primary" -c postgres -- \
  psql -X -U postgres -d "$rehearsal_database" -Atc "SELECT oid FROM pg_subscription WHERE subname = '$subscription'")"
[ -n "$subscription_oid" ] || { echo "target subscription is absent: $subscription" >&2; exit 1; }

echo "dropping target subscription (also requests remote slot removal): $subscription"
kubectl -n "$target_namespace" exec "$target_primary" -c postgres -- \
  psql -X -v ON_ERROR_STOP=1 -U postgres -d "$rehearsal_database" -c "DROP SUBSCRIPTION IF EXISTS $subscription;"

echo "dropping source publication, residual slots, and temporary role"
source_sql "DROP PUBLICATION IF EXISTS $publication;
  SELECT pg_drop_replication_slot(slot_name)
    FROM pg_replication_slots
   WHERE slot_name = '$slot_name'
      OR slot_name LIKE 'pg_${subscription_oid}_sync_%';
  REVOKE ALL PRIVILEGES ON ALL TABLES IN SCHEMA public FROM $replication_role;
  REVOKE ALL PRIVILEGES ON ALL SEQUENCES IN SCHEMA public FROM $replication_role;
  REVOKE ALL PRIVILEGES ON SCHEMA public FROM $replication_role;
  REVOKE CONNECT ON DATABASE monitube FROM $replication_role;
  DROP ROLE IF EXISTS $replication_role;"

echo "dropping isolated target database: $rehearsal_database"
kubectl -n "$target_namespace" exec "$target_primary" -c postgres -- \
  dropdb -U postgres "$rehearsal_database"

echo "logical rehearsal cleanup completed: database=$rehearsal_database subscription=$subscription publication=$publication slot=$slot_name temporary_sync_slot_prefix=pg_${subscription_oid}_sync_ role=$replication_role"
