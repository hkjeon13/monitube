#!/bin/sh
# Read-only post-failover gate for the canonical central Monitube database.
# It never switches endpoints, starts workers, or changes database objects.
set -eu

namespace="${TARGET_NAMESPACE:-database}"
cluster="${TARGET_CLUSTER:-central-pg-data}"
database="${TARGET_DATABASE:-monitube}"
expected_lsn="${EXPECTED_RECOVERY_LSN:-}"
# The current Monitube schema has two historical NOT VALID constraints.  A
# recovery gate must compare with that approved baseline, not incorrectly
# treat their pre-existing state as data loss or a new migration failure.
expected_unvalidated_constraints="${EXPECTED_UNVALIDATED_CONSTRAINTS:-2}"

[ -n "$expected_lsn" ] || {
  echo 'EXPECTED_RECOVERY_LSN is required (the former primary clean-shutdown checkpoint LSN).' >&2
  exit 2
}
case "$expected_unvalidated_constraints" in
  ''|*[!0-9]*) echo 'EXPECTED_UNVALIDATED_CONSTRAINTS must be a non-negative integer' >&2; exit 2 ;;
esac
command -v kubectl >/dev/null 2>&1 || { echo 'missing required command: kubectl' >&2; exit 2; }

phase="$(kubectl -n "$namespace" get cluster "$cluster" -o jsonpath='{.status.phase}')"
ready="$(kubectl -n "$namespace" get cluster "$cluster" -o jsonpath='{.status.readyInstances}')"
primary="$(kubectl -n "$namespace" get cluster "$cluster" -o jsonpath='{.status.currentPrimary}')"

[ "$phase" = 'Cluster in healthy state' ] || {
  echo "central cluster is not healthy: phase=${phase:-none}" >&2
  exit 1
}
[ "$ready" = '3' ] || {
  echo "central cluster is not fully ready: ready=${ready:-0}" >&2
  exit 1
}
[ -n "$primary" ] || { echo 'central cluster has no current primary' >&2; exit 1; }

rw_addresses="$(kubectl -n "$namespace" get endpoints "${cluster}-rw" \
  -o jsonpath='{range .subsets[*].addresses[*]}{.ip}{" "}{end}')"
[ -n "$rw_addresses" ] || { echo 'central RW endpoint has no ready address' >&2; exit 1; }

sql() {
  kubectl -n "$namespace" exec "$primary" -c postgres -- \
    psql -X -v ON_ERROR_STOP=1 -U postgres -d "$database" -Atc "$1"
}

in_recovery="$(sql 'SELECT pg_is_in_recovery()')"
[ "$in_recovery" = 'f' ] || {
  echo "primary is still in recovery: pg_is_in_recovery=${in_recovery:-unknown}" >&2
  exit 1
}

# Comparing the numeric LSN proves recovered history reached the old primary's
# clean-shutdown checkpoint. Promotion emits later records, so current LSN may
# legitimately be higher than the final replay LSN.
lsn_reached="$(sql "SELECT pg_current_wal_lsn() >= '${expected_lsn}'::pg_lsn")"
[ "$lsn_reached" = 't' ] || {
  echo "recovery LSN has not reached former primary checkpoint: expected=$expected_lsn" >&2
  exit 1
}

schema_present="$(sql "SELECT to_regclass('public.monitube_schema_migrations') IS NOT NULL")"
[ "$schema_present" = 't' ] || { echo 'Monitube schema is absent on recovered primary' >&2; exit 1; }

integrity="$(sql '
  SELECT count(*) FROM pg_index WHERE NOT indisvalid OR NOT indisready;
  SELECT count(*) FROM pg_constraint
   WHERE contype IN (chr(112), chr(102), chr(117), chr(99)) AND NOT convalidated;
  SELECT count(*) FROM source_videos sv
   LEFT JOIN collection_sources cs ON cs.id = sv.source_id
   LEFT JOIN videos v ON v.id = sv.video_id
   WHERE cs.id IS NULL OR v.id IS NULL;
  SELECT count(*) FROM comments c LEFT JOIN videos v ON v.id = c.video_id WHERE v.id IS NULL;
')"
expected_integrity='0
'"$expected_unvalidated_constraints"'
0
0'
[ "$integrity" = "$expected_integrity" ] || {
  echo "recovered database integrity gate failed: ${integrity}" >&2
  exit 1
}

metrics="$(sql '
  SELECT (SELECT count(*) FROM collection_sources),
         (SELECT count(*) FROM channels),
         (SELECT count(*) FROM videos),
         (SELECT count(*) FROM comments),
         (SELECT count(*) FROM monitube_schema_migrations),
         (SELECT count(*) FROM nlp_documents),
         (SELECT count(*) FROM nlp_document_terms),
         (SELECT count(*) FROM nlp_daily_term_stats),
         (SELECT count(*) FROM sync_jobs),
         (SELECT count(*) FROM sync_checkpoints),
         (SELECT count(*) FROM outbox_events);
  SELECT pg_current_wal_lsn();
  SELECT pg_last_wal_replay_lsn();
  SELECT pg_last_xact_replay_timestamp();
')"

printf '%s\n' "cluster=$namespace/$cluster"
printf '%s\n' "primary=$primary"
printf '%s\n' "rw_addresses=$rw_addresses"
printf '%s\n' "former_primary_checkpoint_lsn=$expected_lsn"
printf '%s\n' "recovered_metrics=$metrics"
echo 'post_failover_gate=true'
