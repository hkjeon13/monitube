#!/bin/sh
set -eu

usage() {
  echo "usage: apply_migrations.sh [--check|--status]" >&2
  exit 2
}

mode="apply"
case "${1:-}" in
  "") ;;
  --check) mode="check" ;;
  --status) mode="status" ;;
  *) usage ;;
esac
[ "$#" -le 1 ] || usage

bootstrap_pg_stat_statements="${MONITUBE_BOOTSTRAP_PG_STAT_STATEMENTS:-true}"
case "$bootstrap_pg_stat_statements" in
  true|false) ;;
  *)
    echo "MONITUBE_BOOTSTRAP_PG_STAT_STATEMENTS must be true or false." >&2
    exit 2
    ;;
esac

# This script runs inside the Compose `migrate` service. It deliberately uses
# PG* variables instead of a URL so connection credentials are never echoed.
export PGHOST="${POSTGRES_HOST:-postgres}"
export PGPORT="${POSTGRES_PORT:-5432}"
export PGDATABASE="${POSTGRES_DB:?POSTGRES_DB is required}"
export PGUSER="${POSTGRES_USER:?POSTGRES_USER is required}"
export PGPASSWORD="${POSTGRES_PASSWORD:?POSTGRES_PASSWORD is required}"

psql_cmd() {
  psql -X -v ON_ERROR_STOP=1 "$@"
}

attempt=0
until psql_cmd -q -c 'SELECT 1' >/dev/null 2>&1; do
  attempt=$((attempt + 1))
  if [ "$attempt" -ge 60 ]; then
    echo "PostgreSQL did not become ready for migrations." >&2
    exit 1
  fi
  sleep 1
done

ledger_exists() {
  psql_cmd -Atq -c "SELECT to_regclass('public.monitube_schema_migrations') IS NOT NULL" | grep -qx 't'
}

is_applied() {
  ledger_exists || return 1
  psql_cmd -Atq -c 'SELECT filename FROM monitube_schema_migrations' | grep -Fqx "$1"
}

extension_installed() {
  extension_literal=$(sql_literal "$1")
  psql_cmd -Atq -c "SELECT EXISTS (SELECT 1 FROM pg_extension WHERE extname = '$extension_literal')" | grep -qx 't'
}

sql_literal() {
  # Migration filenames are committed, predictable inputs, but quote them before
  # placing them in SQL so the runner stays correct if a future name contains an
  # apostrophe. Avoid psql variable interpolation here: it is not available for
  # all command-input paths used by the Alpine image.
  printf '%s' "$1" | sed "s/'/''/g"
}

mark_applied() {
  migration_literal=$(sql_literal "$1")
  psql_cmd -q -c "INSERT INTO monitube_schema_migrations (filename) VALUES ('$migration_literal') ON CONFLICT (filename) DO NOTHING"
}

has_initial_schema() {
  psql_cmd -Atq -c "SELECT to_regclass('public.collection_sources') IS NOT NULL" | grep -qx 't'
}

apply_migration() {
  migration_path="$1"
  migration_name="$2"
  migration_literal=$(sql_literal "$migration_name")

  # The migration and its ledger entry commit atomically. Keep committed SQL
  # migrations transaction-safe; a future non-transactional migration needs a
  # dedicated runner change rather than silently weakening this guarantee.
  {
    cat "$migration_path"
    printf "\nINSERT INTO monitube_schema_migrations (filename) VALUES ('%s') ON CONFLICT (filename) DO NOTHING;\n" "$migration_literal"
  } | psql_cmd --single-transaction
}

if [ ! -f /migrations/001_initial_schema.sql ]; then
  echo "Missing required baseline migration: 001_initial_schema.sql" >&2
  exit 1
fi

if [ "$mode" = "check" ] || [ "$mode" = "status" ]; then
  pending=0

  if [ "$bootstrap_pg_stat_statements" = "true" ] && ! extension_installed pg_stat_statements; then
    echo "pending extension: pg_stat_statements"
    pending=$((pending + 1))
  fi

  for migration_path in /migrations/[0-9][0-9][0-9]_*.sql; do
    [ -f "$migration_path" ] || continue
    migration_name=$(basename "$migration_path")
    if is_applied "$migration_name"; then
      [ "$mode" = "status" ] && echo "applied migration: $migration_name"
    else
      echo "pending migration: $migration_name"
      pending=$((pending + 1))
    fi
  done

  if [ "$pending" -gt 0 ]; then
    echo "Database has $pending pending migration or extension item(s)."
    [ "$mode" = "check" ] && exit 10
  else
    echo "Database migrations and required extensions are current."
  fi
  exit 0
fi

psql_cmd -q -c '
  CREATE TABLE IF NOT EXISTS monitube_schema_migrations (
    filename TEXT PRIMARY KEY,
    applied_at TIMESTAMPTZ NOT NULL DEFAULT now()
  )
'

if [ "$bootstrap_pg_stat_statements" = "true" ]; then
  # Loading the library is a PostgreSQL startup setting. Creating the extension
  # here is idempotent and lets the deploy script verify both halves after a
  # configuration restart.
  psql_cmd -q -c 'CREATE EXTENSION IF NOT EXISTS pg_stat_statements'
fi

for migration_path in /migrations/[0-9][0-9][0-9]_*.sql; do
  [ -f "$migration_path" ] || continue
  migration_name=$(basename "$migration_path")

  if is_applied "$migration_name"; then
    continue
  fi

  # PostgreSQL's image executes 001 on a newly created volume before this
  # service runs. Existing deployments predate the ledger, so record that
  # baseline instead of attempting to execute its non-idempotent DDL again.
  if [ "$migration_name" = "001_initial_schema.sql" ] && has_initial_schema; then
    mark_applied "$migration_name"
    echo "Recorded existing baseline migration: $migration_name"
    continue
  fi

  echo "Applying migration: $migration_name"
  apply_migration "$migration_path" "$migration_name"
done

echo "Database migrations and required extensions are current."
