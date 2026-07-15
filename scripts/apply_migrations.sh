#!/bin/sh
set -eu

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

psql_cmd -q -c '
  CREATE TABLE IF NOT EXISTS monitube_schema_migrations (
    filename TEXT PRIMARY KEY,
    applied_at TIMESTAMPTZ NOT NULL DEFAULT now()
  )
'

is_applied() {
  psql_cmd -Atq -v migration="$1" \
    -c "SELECT 1 FROM monitube_schema_migrations WHERE filename = :'migration' LIMIT 1" \
    | grep -qx '1'
}

mark_applied() {
  psql_cmd -q -v migration="$1" \
    -c "INSERT INTO monitube_schema_migrations (filename) VALUES (:'migration') ON CONFLICT (filename) DO NOTHING"
}

has_initial_schema() {
  psql_cmd -Atq -c "SELECT to_regclass('public.collection_sources') IS NOT NULL" | grep -qx 't'
}

apply_migration() {
  migration_path="$1"
  migration_name="$2"

  # The migration and its ledger entry commit atomically. Keep committed SQL
  # migrations transaction-safe; a future non-transactional migration needs a
  # dedicated runner change rather than silently weakening this guarantee.
  {
    cat "$migration_path"
    printf "\nINSERT INTO monitube_schema_migrations (filename) VALUES (:'migration') ON CONFLICT (filename) DO NOTHING;\n"
  } | psql_cmd --single-transaction -v migration="$migration_name"
}

if [ ! -f /migrations/001_initial_schema.sql ]; then
  echo "Missing required baseline migration: 001_initial_schema.sql" >&2
  exit 1
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

echo "Database migrations are current."
