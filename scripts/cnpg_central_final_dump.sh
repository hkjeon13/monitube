#!/bin/sh
# Create the final, writer-fenced logical dump used by the central-CNPG
# cutover.  It does not alter either database.  The dump is published only
# after pg_restore can read its TOC and a checksum/manifest are written.
set -eu

output_dir="${1:?usage: $0 /absolute/path/to/output-directory --confirm-writer-fenced}"
confirmation="${2:-}"
source_namespace="${SOURCE_NAMESPACE:-monitube-prod}"
source_pod="${SOURCE_POD:-monitube-postgres-0}"
target_namespace="${TARGET_NAMESPACE:-database}"
target_cluster="${TARGET_CLUSTER:-central-pg-data}"
timestamp="$(date -u +%Y%m%dT%H%M%SZ)"
basename="monitube-final-${timestamp}"
partial_dump="${output_dir}/.${basename}.pg_dump.partial"
dump_path="${output_dir}/${basename}.pg_dump"
quiesce_path="${output_dir}/${basename}.quiesce.txt"
manifest_path="${output_dir}/${basename}.manifest.txt"

[ "$confirmation" = "--confirm-writer-fenced" ] || {
  echo "refusing final dump without --confirm-writer-fenced" >&2
  exit 2
}
case "$output_dir" in
  /*) ;;
  *) echo "output directory must be an absolute path" >&2; exit 2 ;;
esac
command -v kubectl >/dev/null 2>&1 || { echo "missing required command: kubectl" >&2; exit 2; }
command -v sha256sum >/dev/null 2>&1 || { echo "missing required command: sha256sum" >&2; exit 2; }
[ -d "$output_dir" ] || { echo "output directory does not exist: $output_dir" >&2; exit 2; }
[ ! -e "$dump_path" ] || { echo "refusing to overwrite existing dump: $dump_path" >&2; exit 2; }
[ ! -e "$quiesce_path" ] || { echo "refusing to overwrite existing quiesce evidence: $quiesce_path" >&2; exit 2; }
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

# This checks the deployed API fence, all worker replicas, drain state, and
# two identical source snapshots.  Keep its evidence beside the final dump.
SOURCE_NAMESPACE="$source_namespace" SOURCE_POD="$source_pod" \
  ./scripts/cnpg_central_quiesce_verify.sh > "$quiesce_path"

echo "creating writer-fenced logical dump: $dump_path"
kubectl -n "$source_namespace" exec "$source_pod" -- sh -ec \
  'exec pg_dump -Fc --no-owner --no-acl -U "$POSTGRES_USER" -d "$POSTGRES_DB"' \
  > "$partial_dump"

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
  echo "source_namespace=$source_namespace"
  echo "source_pod=$source_pod"
  echo "dump_path=$dump_path"
  echo "dump_bytes=$dump_bytes"
  echo "dump_sha256=$dump_sha"
  echo "quiesce_evidence=$quiesce_path"
  echo "pg_restore_list_verified=true"
} > "$manifest_path"

echo "final_dump=$dump_path"
echo "final_dump_sha256=$dump_sha"
echo "final_dump_manifest=$manifest_path"
