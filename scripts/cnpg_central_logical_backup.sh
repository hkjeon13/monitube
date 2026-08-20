#!/bin/sh
# Create a verified custom-format logical backup of the canonical central
# Monitube database. The target cluster is only read; a short-lived hidden
# copy is staged solely to ask the in-container pg_restore to list the TOC.
set -eu

output_dir="${1:?usage: $0 /absolute/path/to/output-directory}"
target_namespace="${TARGET_NAMESPACE:-database}"
target_cluster="${TARGET_CLUSTER:-central-pg-data}"
target_database="${TARGET_DATABASE:-monitube}"
timestamp="$(date -u +%Y%m%dT%H%M%SZ)"
basename="${target_database}-central-${timestamp}"
partial_dump="${output_dir}/.${basename}.pg_dump.partial"
dump_path="${output_dir}/${basename}.pg_dump"
manifest_path="${output_dir}/${basename}.manifest.txt"

case "$output_dir" in
  /*) ;;
  *) echo "output directory must be an absolute path" >&2; exit 2 ;;
esac
case "$target_database" in
  monitube) ;;
  *) echo "target database must be monitube" >&2; exit 2 ;;
esac
command -v kubectl >/dev/null 2>&1 || { echo "missing required command: kubectl" >&2; exit 2; }
command -v sha256sum >/dev/null 2>&1 || { echo "missing required command: sha256sum" >&2; exit 2; }
[ -d "$output_dir" ] || { echo "output directory does not exist: $output_dir" >&2; exit 2; }
[ ! -e "$dump_path" ] || { echo "refusing to overwrite existing dump: $dump_path" >&2; exit 2; }
[ ! -e "$manifest_path" ] || { echo "refusing to overwrite existing manifest: $manifest_path" >&2; exit 2; }

umask 077
target_primary="$(kubectl -n "$target_namespace" get cluster "$target_cluster" -o jsonpath='{.status.currentPrimary}')"
[ -n "$target_primary" ] || { echo "target cluster has no reported primary" >&2; exit 1; }
remote_toc_probe="/var/lib/postgresql/data/.${basename}.toc-check.pg_dump"
cleanup_staging() {
  rm -f "$partial_dump"
  kubectl -n "$target_namespace" exec "$target_primary" -c postgres -- \
    rm -f "$remote_toc_probe" >/dev/null 2>&1 || true
}
trap cleanup_staging EXIT HUP INT TERM

echo "creating central logical backup: $dump_path"
kubectl -n "$target_namespace" exec "$target_primary" -c postgres -- \
  pg_dump -Fc --no-owner --no-acl -U postgres -d "$target_database" > "$partial_dump"

kubectl -n "$target_namespace" cp "$partial_dump" "$target_primary:$remote_toc_probe" -c postgres
kubectl -n "$target_namespace" exec "$target_primary" -c postgres -- \
  pg_restore --list "$remote_toc_probe" >/dev/null
kubectl -n "$target_namespace" exec "$target_primary" -c postgres -- \
  rm -f "$remote_toc_probe" >/dev/null 2>&1 || true
mv "$partial_dump" "$dump_path"
trap - EXIT HUP INT TERM

dump_sha="$(sha256sum "$dump_path" | awk '{print $1}')"
dump_bytes="$(wc -c < "$dump_path" | tr -d ' ')"
{
  echo "created_at_utc=$(date -u +%FT%TZ)"
  echo "target_namespace=$target_namespace"
  echo "target_cluster=$target_cluster"
  echo "target_primary=$target_primary"
  echo "target_database=$target_database"
  echo "dump_path=$dump_path"
  echo "dump_bytes=$dump_bytes"
  echo "dump_sha256=$dump_sha"
  echo "pg_restore_list_verified=true"
} > "$manifest_path"

echo "central_logical_backup=$dump_path"
echo "central_logical_backup_sha256=$dump_sha"
echo "central_logical_backup_manifest=$manifest_path"
