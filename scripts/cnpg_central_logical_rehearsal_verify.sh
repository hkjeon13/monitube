#!/bin/sh
# Read-only evidence collector for one completed isolated logical rehearsal.
set -eu

source_namespace="${SOURCE_NAMESPACE:-monitube-prod}"
source_pod="${SOURCE_POD:-monitube-postgres-0}"
target_namespace="${TARGET_NAMESPACE:-database}"
target_cluster="${TARGET_CLUSTER:-central-pg-data}"
rehearsal_database="${1:-}"
max_slot_lag_bytes="${MAX_SLOT_LAG_BYTES:-67108864}"

case "$rehearsal_database" in
  monitube_logical_rehearsal_[0-9][0-9][0-9][0-9][0-9][0-9][0-9][0-9]_[0-9][0-9][0-9][0-9][0-9][0-9]) ;;
  *) echo "rehearsal database must use monitube_logical_rehearsal_YYYYMMDD_HHMMSS" >&2; exit 2 ;;
esac
case "$max_slot_lag_bytes" in
  ''|*[!0-9]*) echo "MAX_SLOT_LAG_BYTES must be a non-negative integer" >&2; exit 2 ;;
esac
command -v kubectl >/dev/null 2>&1 || { echo "missing required command: kubectl" >&2; exit 2; }

source_sql() {
  kubectl -n "$source_namespace" exec "$source_pod" -- sh -ec \
    'psql -X -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d "$POSTGRES_DB" -Atc "$1"' sh "$1"
}

target_primary="$(kubectl -n "$target_namespace" get cluster "$target_cluster" -o jsonpath='{.status.currentPrimary}')"
[ -n "$target_primary" ] || { echo "target cluster has no reported primary" >&2; exit 1; }
suffix="${rehearsal_database#monitube_logical_rehearsal_}"
subscription="monitube_central_rehearsal_${suffix}"
slot_name="monitube_central_rehearsal_${suffix}"

target_exists="$(kubectl -n "$target_namespace" exec "$target_primary" -c postgres -- \
  psql -X -U postgres -d postgres -Atc "SELECT EXISTS (SELECT 1 FROM pg_database WHERE datname = '$rehearsal_database')")"
[ "$target_exists" = "t" ] || { echo "isolated rehearsal database is absent: $rehearsal_database" >&2; exit 1; }

pending="$(kubectl -n "$target_namespace" exec "$target_primary" -c postgres -- \
  psql -X -U postgres -d "$rehearsal_database" -Atc "SELECT count(*) FROM pg_subscription_rel WHERE srsubid = (SELECT oid FROM pg_subscription WHERE subname = '$subscription') AND srsubstate <> 'r'")"
[ "$pending" = "0" ] || {
  echo "initial copy is incomplete: pending_relations=$pending" >&2
  exit 1
}

slot_state="$(source_sql "SELECT active || '|' || pg_wal_lsn_diff(pg_current_wal_lsn(), confirmed_flush_lsn) FROM pg_replication_slots WHERE slot_name = '$slot_name'")"
[ -n "$slot_state" ] || { echo "source replication slot is absent: $slot_name" >&2; exit 1; }
slot_active="${slot_state%%|*}"
slot_lag_bytes="${slot_state#*|}"
[ "$slot_active" = "true" ] || { echo "source replication slot is inactive" >&2; exit 1; }
[ "$slot_lag_bytes" -le "$max_slot_lag_bytes" ] || {
  echo "source slot lag exceeds threshold: ${slot_lag_bytes} > ${max_slot_lag_bytes}" >&2
  exit 1
}

echo "logical_rehearsal=$rehearsal_database"
echo "subscription=$subscription"
echo "initial_copy_pending_relations=$pending"
echo "source_slot_lag_bytes=$slot_lag_bytes"
echo "source_slot_active=$slot_active"

kubectl -n "$target_namespace" exec "$target_primary" -c postgres -- \
  psql -X -v ON_ERROR_STOP=1 -U postgres -d "$rehearsal_database" -Atc \
  "SELECT 'subscription=' || subname || '|enabled=' || subenabled || '|slot=' || subslotname FROM pg_subscription WHERE subname = '$subscription';
   SELECT 'apply_lsn=' || COALESCE(received_lsn::text, '') || '|latest_lsn=' || COALESCE(latest_end_lsn::text, '') || '|latest_time=' || COALESCE(latest_end_time::text, '') FROM pg_stat_subscription WHERE subname = '$subscription' AND relid IS NULL;
   SELECT 'invalid_indexes=' || count(*) FROM pg_index WHERE NOT indisvalid OR NOT indisready;
   SELECT 'target_count=' || relname || '|' || n_live_tup FROM pg_stat_user_tables WHERE relname IN ('collection_sources', 'channels', 'videos', 'comments', 'nlp_documents', 'sync_jobs') ORDER BY relname;"

source_sql "SELECT 'source_count=' || relname || '|' || n_live_tup FROM pg_stat_user_tables WHERE schemaname = 'public' AND relname IN ('collection_sources', 'channels', 'videos', 'comments', 'nlp_documents', 'sync_jobs') ORDER BY relname;"
