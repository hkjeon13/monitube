#!/bin/sh
# Read-only inventory for the Monitube -> central-pg-data migration.
# It never creates resources, changes a Secret, or writes to either database.
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

printf '%s\n' "source=${source_namespace}/${source_pod} target=${target_namespace}/${target_cluster}/${target_database}"
printf '%s\n' '--- workload and storage status ---'
kubectl -n "$source_namespace" get pod "$source_pod"
kubectl -n "$source_namespace" get pvc monitube-postgres
kubectl get pv monitube-postgres-recovery
kubectl -n "$source_namespace" get cronjob

printf '%s\n' '--- source database baseline ---'
kubectl -n "$source_namespace" exec "$source_pod" -- sh -ec '
  psql -X -U "$POSTGRES_USER" -d "$POSTGRES_DB" -Atc "
    SELECT current_setting(chr(115)||chr(101)||chr(114)||chr(118)||chr(101)||chr(114)||chr(95)||chr(118)||chr(101)||chr(114)||chr(115)||chr(105)||chr(111)||chr(110));
    SELECT (SELECT count(*) FROM collection_sources),
           (SELECT count(*) FROM channels),
           (SELECT count(*) FROM videos),
           (SELECT count(*) FROM comments),
           (SELECT count(*) FROM monitube_schema_migrations),
           (SELECT count(*) FROM nlp_documents),
           (SELECT count(*) FROM sync_jobs),
           pg_size_pretty(pg_database_size(current_database()));
  "
'

printf '%s\n' '--- source writer quiesce signals ---'
kubectl -n "$source_namespace" exec "$source_pod" -- sh -ec '
  psql -X -U "$POSTGRES_USER" -d "$POSTGRES_DB" -Atc "
    SELECT state || chr(124) || count(*)
      FROM sync_jobs
     GROUP BY state
     ORDER BY state;
    SELECT chr(97)||chr(99)||chr(116)||chr(105)||chr(118)||chr(101)||chr(95)||chr(116)||chr(114)||chr(97)||chr(110)||chr(115)||chr(97)||chr(99)||chr(116)||chr(105)||chr(111)||chr(110)||chr(115)||chr(124) || count(*)
      FROM pg_stat_activity
     WHERE datname = current_database()
       AND pid <> pg_backend_pid()
       AND state <> chr(105)||chr(100)||chr(108)||chr(101);
    SELECT chr(119)||chr(97)||chr(105)||chr(116)||chr(105)||chr(110)||chr(103)||chr(95)||chr(108)||chr(111)||chr(99)||chr(107)||chr(115)||chr(124) || count(*)
      FROM pg_locks
     WHERE NOT granted;
  "
'

printf '%s\n' '--- central cluster, database, and backup state ---'
kubectl -n "$target_namespace" get cluster "$target_cluster"
kubectl -n "$target_namespace" get pooler,scheduledbackup,backup
kubectl -n "$target_namespace" exec "$target_primary" -c postgres -- \
  psql -X -U postgres -d postgres -Atc "
    SELECT current_setting('server_version');
    SELECT datname || '|' || datdba::regrole FROM pg_database WHERE datname = '${target_database}';
    SELECT rolname || '|' || rolcanlogin || '|' || rolsuper || '|' || rolconnlimit
      FROM pg_roles WHERE rolname = '${target_database}';
    SELECT count(*) FROM pg_tables WHERE schemaname = 'public'
      AND tableowner = '${target_database}';
  "

printf '%s\n' '--- central connection budget ---'
kubectl -n "$target_namespace" exec "$target_primary" -c postgres -- \
  psql -X -U postgres -d postgres -Atc '
    SHOW max_connections;
    SELECT count(*) FROM pg_stat_activity;
  '
kubectl -n "$target_namespace" get pooler "$target_cluster-pooler-rw" \
  -o jsonpath='instances={.spec.instances} mode={.spec.pgbouncer.poolMode} parameters={.spec.pgbouncer.parameters}{"\n"}'

printf '%s\n' '--- namespace-local central application Secret contract ---'
if kubectl -n "$source_namespace" get secret monitube-central-db >/dev/null 2>&1; then
  kubectl -n "$source_namespace" get secret monitube-central-db \
    -o go-template='{{range $key, $_ := .data}}{{$key}}{{"\n"}}{{end}}' | sort
else
  printf '%s\n' "MISSING: ${source_namespace}/monitube-central-db (expected before central mode is rendered)"
fi

printf '%s\n' 'Preflight is read-only. Do not treat this as cutover approval.'
