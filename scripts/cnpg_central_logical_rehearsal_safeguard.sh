#!/bin/sh
# Stop one isolated logical-replication rehearsal before temporary table-sync
# slots can exhaust the legacy source volume. Cleanup is an explicit opt-in
# and is restricted to the timestamped rehearsal namespace.
set -eu

rehearsal_database="${1:?usage: $0 monitube_logical_rehearsal_YYYYMMDD_HHMMSS --confirm-cleanup-on-low-space}"
confirmation="${2:-}"
source_namespace="${SOURCE_NAMESPACE:-monitube-prod}"
source_pod="${SOURCE_POD:-monitube-postgres-0}"
min_free_bytes="${SOURCE_MIN_FREE_BYTES:-68719476736}" # 64 GiB

case "$rehearsal_database" in
  monitube_logical_rehearsal_[0-9][0-9][0-9][0-9][0-9][0-9][0-9][0-9]_[0-9][0-9][0-9][0-9][0-9][0-9]) ;;
  *) echo "rehearsal database must use monitube_logical_rehearsal_YYYYMMDD_HHMMSS" >&2; exit 2 ;;
esac
[ "$confirmation" = "--confirm-cleanup-on-low-space" ] || {
  echo "refusing automatic rehearsal cleanup without --confirm-cleanup-on-low-space" >&2
  exit 2
}
case "$min_free_bytes" in
  ''|*[!0-9]*|0) echo "SOURCE_MIN_FREE_BYTES must be a positive integer" >&2; exit 2 ;;
esac
command -v kubectl >/dev/null 2>&1 || { echo "missing required command: kubectl" >&2; exit 2; }

free_bytes="$(kubectl -n "$source_namespace" exec "$source_pod" -- \
  df -PB1 /var/lib/postgresql/data | awk 'NR == 2 { print $4 }')"
case "$free_bytes" in
  ''|*[!0-9]*) echo "could not read source free bytes: $free_bytes" >&2; exit 1 ;;
esac

echo "source_free_bytes=$free_bytes"
echo "source_min_free_bytes=$min_free_bytes"
if [ "$free_bytes" -ge "$min_free_bytes" ]; then
  echo "low_space_cleanup_needed=false"
  exit 0
fi

echo "low_space_cleanup_needed=true"
echo "cleaning only isolated rehearsal resources for $rehearsal_database" >&2
SOURCE_NAMESPACE="$source_namespace" SOURCE_POD="$source_pod" \
  ./scripts/cnpg_central_logical_rehearsal_cleanup.sh "$rehearsal_database" --confirm
