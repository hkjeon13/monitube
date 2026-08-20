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

# Keep the result compact and PII-free.  These are the large, product-critical
# relations that make an accidental partial import immediately visible.  The
# script is intentionally a gate, not an exhaustive checksum of 11M comments.
metrics_sql='
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
  SELECT count(*) FROM pg_indexes
    WHERE schemaname = chr(112)||chr(117)||chr(98)||chr(108)||chr(105)||chr(99);
  SELECT count(*) FROM pg_index WHERE NOT indisvalid OR NOT indisready;
  SELECT count(*) FROM pg_constraint
    WHERE contype IN (chr(112), chr(102), chr(117), chr(99)) AND NOT convalidated;
  SELECT count(*) FROM pg_sequences WHERE schemaname = chr(112)||chr(117)||chr(98)||chr(108)||chr(105)||chr(99);
  SELECT count(*) FROM source_videos sv
    LEFT JOIN collection_sources cs ON cs.id = sv.source_id
    LEFT JOIN videos v ON v.id = sv.video_id
    WHERE cs.id IS NULL OR v.id IS NULL;
  SELECT count(*) FROM comments c LEFT JOIN videos v ON v.id = c.video_id
    WHERE v.id IS NULL;
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

echo 'Bounded parity baseline matches. Run it only after writer fencing for cutover parity.'
