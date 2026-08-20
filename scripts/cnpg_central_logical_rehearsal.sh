#!/bin/sh
# Set up an isolated logical-replication rehearsal from legacy Monitube into
# central CNPG. It never uses the production central `monitube` database and
# never changes application endpoints. It deliberately retains the rehearsal
# database, publication, subscription and slot as evidence until an operator
# records the result and performs the explicit cleanup printed at the end.
set -eu

source_namespace="${SOURCE_NAMESPACE:-monitube-prod}"
source_pod="${SOURCE_POD:-monitube-postgres-0}"
# The StatefulSet pod name is not a Service DNS record. Replication from the
# database namespace must use the legacy PostgreSQL Service's cluster DNS.
source_host="${SOURCE_HOST:-postgres.monitube-prod.svc.cluster.local}"
target_namespace="${TARGET_NAMESPACE:-database}"
target_cluster="${TARGET_CLUSTER:-central-pg-data}"
target_database="${TARGET_DATABASE:-monitube}"
rehearsal_database="${1:-monitube_logical_rehearsal_$(date -u +%Y%m%d_%H%M%S)}"
sync_timeout_seconds="${SYNC_TIMEOUT_SECONDS:-14400}"
sync_poll_seconds="${SYNC_POLL_SECONDS:-30}"

case "$rehearsal_database" in
  monitube_logical_rehearsal_[0-9][0-9][0-9][0-9][0-9][0-9][0-9][0-9]_[0-9][0-9][0-9][0-9][0-9][0-9]) ;;
  *) echo "rehearsal database must use monitube_logical_rehearsal_YYYYMMDD_HHMMSS" >&2; exit 2 ;;
esac

require_command() {
  command -v "$1" >/dev/null 2>&1 || {
    echo "missing required command: $1" >&2
    exit 2
  }
}

source_sql() {
  kubectl -n "$source_namespace" exec "$source_pod" -- sh -ec \
    'psql -X -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d "$POSTGRES_DB" -Atc "$1"' sh "$1"
}

target_sql() {
  kubectl -n "$target_namespace" exec "$target_primary" -c postgres -- \
    psql -X -v ON_ERROR_STOP=1 -U postgres -d "$rehearsal_database" -Atc "$1"
}

require_command kubectl
require_command openssl
require_command date

target_primary="$(kubectl -n "$target_namespace" get cluster "$target_cluster" -o jsonpath='{.status.currentPrimary}')"
[ -n "$target_primary" ] || { echo "target cluster has no reported primary" >&2; exit 1; }

# PostgreSQL identifiers below are generated from a timestamp and use only
# lower-case letters, digits and underscores. Keeping them deterministic makes
# evidence collection and cleanup possible without exposing credentials.
suffix="${rehearsal_database#monitube_logical_rehearsal_}"
publication="monitube_central_rehearsal_${suffix}"
subscription="monitube_central_rehearsal_${suffix}"
slot_name="monitube_central_rehearsal_${suffix}"
replication_role="monitube_repl_rehearsal_${suffix}"
role_password="$(openssl rand -hex 32)"

source_wal_level="$(source_sql 'SHOW wal_level')"
[ "$source_wal_level" = "logical" ] || {
  echo "source wal_level=${source_wal_level}; apply the separately approved logicalReplication chart switch and wait for the source restart before running this rehearsal" >&2
  exit 1
}

target_logical_workers="$(kubectl -n "$target_namespace" exec "$target_primary" -c postgres -- psql -X -U postgres -d postgres -Atc 'SHOW max_logical_replication_workers')"
target_sync_workers="$(kubectl -n "$target_namespace" exec "$target_primary" -c postgres -- psql -X -U postgres -d postgres -Atc 'SHOW max_sync_workers_per_subscription')"
target_worker_processes="$(kubectl -n "$target_namespace" exec "$target_primary" -c postgres -- psql -X -U postgres -d postgres -Atc 'SHOW max_worker_processes')"
[ "$target_sync_workers" -ge 1 ] || { echo "target max_sync_workers_per_subscription=$target_sync_workers; initial copy is disabled" >&2; exit 1; }
[ "$target_logical_workers" -ge $((target_sync_workers + 1)) ] || {
  echo "target max_logical_replication_workers=$target_logical_workers is below leader plus sync-worker demand" >&2
  exit 1
}
[ "$target_worker_processes" -ge $((target_logical_workers + 1)) ] || {
  echo "target max_worker_processes=$target_worker_processes lacks logical replication headroom" >&2
  exit 1
}
source_slot_demand=$((target_sync_workers + 1))
for setting in max_replication_slots max_wal_senders; do
  value="$(source_sql "SHOW $setting")"
  [ "$value" -ge "$source_slot_demand" ] || {
    echo "source $setting=$value; at least $source_slot_demand is required for leader plus initial-sync slots" >&2
    exit 1
  }
done

missing_replica_identity="$(source_sql "
  SELECT count(*)
    FROM pg_class c
    JOIN pg_namespace n ON n.oid = c.relnamespace
   WHERE c.relkind = 'r' AND n.nspname = 'public'
     AND c.relreplident = 'n'
     AND NOT EXISTS (
       SELECT 1 FROM pg_index i WHERE i.indrelid = c.oid AND i.indisprimary
     )")"
[ "$missing_replica_identity" = "0" ] || {
  echo "${missing_replica_identity} public tables lack a primary key/replica identity; do not create the publication" >&2
  exit 1
}

production_table_count="$(kubectl -n "$target_namespace" exec "$target_primary" -c postgres -- \
  psql -X -U postgres -d "$target_database" -Atc "SELECT count(*) FROM pg_tables WHERE schemaname = 'public'")"
[ "$production_table_count" = "0" ] || {
  echo "production target ${target_database} is not empty; logical rehearsal must not proceed" >&2
  exit 1
}

target_exists="$(kubectl -n "$target_namespace" exec "$target_primary" -c postgres -- \
  psql -X -U postgres -d postgres -Atc "SELECT EXISTS (SELECT 1 FROM pg_database WHERE datname = '$rehearsal_database')")"
[ "$target_exists" = "f" ] || { echo "rehearsal database already exists: $rehearsal_database" >&2; exit 1; }

remote_dump="/var/lib/postgresql/data/.${rehearsal_database}.schema.pg_dump"
remote_toc="/var/lib/postgresql/data/.${rehearsal_database}.schema.toc"
remote_filtered_toc="/var/lib/postgresql/data/.${rehearsal_database}.schema.filtered.toc"
local_dump="$(mktemp "${TMPDIR:-/tmp}/${rehearsal_database}.XXXXXX.pg_dump")"
cleanup_staging() {
  rm -f "$local_dump"
  kubectl -n "$target_namespace" exec "$target_primary" -c postgres -- \
    rm -f "$remote_dump" "$remote_toc" "$remote_filtered_toc" >/dev/null 2>&1 || true
}
trap cleanup_staging EXIT HUP INT TERM

echo "creating isolated logical rehearsal database: $rehearsal_database"
kubectl -n "$target_namespace" exec "$target_primary" -c postgres -- \
  createdb -U postgres --owner=monitube "$rehearsal_database"
echo "creating source-required extensions with central DBA authority"
target_sql 'CREATE EXTENSION IF NOT EXISTS pgcrypto; CREATE EXTENSION IF NOT EXISTS pg_trgm; CREATE EXTENSION IF NOT EXISTS pg_stat_statements;'

echo "exporting source schema only"
kubectl -n "$source_namespace" exec "$source_pod" -- sh -ec \
  'pg_dump -Fc --schema-only --no-owner --no-privileges -U "$POSTGRES_USER" -d "$POSTGRES_DB"' > "$local_dump"
echo "restoring schema into isolated target"
kubectl -n "$target_namespace" cp "$local_dump" "$target_primary:$remote_dump" -c postgres
kubectl -n "$target_namespace" exec "$target_primary" -c postgres -- sh -ec \
  "pg_restore --list '$remote_dump' > '$remote_toc'; grep -Ev 'EXTENSION (pg_stat_statements|pg_trgm|pgcrypto)' '$remote_toc' > '$remote_filtered_toc'"
kubectl -n "$target_namespace" exec "$target_primary" -c postgres -- \
  pg_restore -U postgres --role=monitube --no-owner --no-privileges --exit-on-error \
  --use-list="$remote_filtered_toc" --dbname="$rehearsal_database" "$remote_dump"

echo "creating least-privilege source replication role and publication"
source_sql "CREATE ROLE $replication_role WITH LOGIN REPLICATION PASSWORD '$role_password';
  GRANT CONNECT ON DATABASE monitube TO $replication_role;
  GRANT USAGE ON SCHEMA public TO $replication_role;
  GRANT SELECT ON ALL TABLES IN SCHEMA public TO $replication_role;
  CREATE PUBLICATION $publication FOR ALL TABLES;"

echo "creating target subscription and starting initial copy"
# role_password is generated as hex, so it cannot break the quoted conninfo;
# it is intentionally never printed or written to any repository file.
target_sql "CREATE SUBSCRIPTION $subscription
  CONNECTION 'host=$source_host port=5432 dbname=monitube user=$replication_role password=$role_password sslmode=disable'
  PUBLICATION $publication
  WITH (copy_data = true, create_slot = true, enabled = true, slot_name = '$slot_name', disable_on_error = true);"

started_at="$(date +%s)"
while :; do
  pending="$(target_sql "SELECT count(*) FROM pg_subscription_rel
    WHERE srsubid = (SELECT oid FROM pg_subscription WHERE subname = '$subscription')
      AND srsubstate <> 'r'")"
  status="$(target_sql "SELECT COALESCE(string_agg(srsubstate, ',' ORDER BY srsubstate), '')
    FROM pg_subscription_rel
    WHERE srsubid = (SELECT oid FROM pg_subscription WHERE subname = '$subscription')")"
  printf '%s\n' "subscription=$subscription pending_tables=$pending states=$status"
  [ "$pending" = "0" ] && break
  [ $(( $(date +%s) - started_at )) -lt "$sync_timeout_seconds" ] || {
    echo "initial copy timeout; leave the subscription disabled or clean it up explicitly before retrying" >&2
    exit 1
  }
  sleep "$sync_poll_seconds"
done

target_sql "SELECT subname || '|enabled=' || subenabled || '|slot=' || subslotname
  FROM pg_subscription WHERE subname = '$subscription';
  SELECT received_lsn || '|' || latest_end_lsn || '|' || latest_end_time
  FROM pg_stat_subscription WHERE subname = '$subscription';"
source_sql "SELECT slot_name || '|active=' || active || '|confirmed_flush=' || confirmed_flush_lsn
  FROM pg_replication_slots WHERE slot_name = '$slot_name';"

echo "logical replication initial copy completed: $rehearsal_database"
echo "do not treat this as final cutover parity while source writers continue."
echo "record subscription/slot lag, then explicitly remove subscription, publication and role after evidence review."
echo "subscription=$subscription publication=$publication role=$replication_role slot=$slot_name"
