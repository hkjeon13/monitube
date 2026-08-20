#!/bin/sh
# Read-only evidence collector for one bounded CNPG physical restore drill.
set -eu

namespace="${TARGET_NAMESPACE:-database}"
drill_name="${1:-}"

case "$drill_name" in
  central-pg-data-restore-[0-9][0-9][0-9][0-9][0-9][0-9][0-9][0-9]-[0-9][0-9][0-9][0-9][0-9][0-9]) ;;
  *) echo "drill name must use central-pg-data-restore-YYYYMMDD-HHMMSS" >&2; exit 2 ;;
esac
command -v kubectl >/dev/null 2>&1 || { echo "missing required command: kubectl" >&2; exit 2; }
command -v date >/dev/null 2>&1 || { echo "missing required command: date" >&2; exit 2; }

phase="$(kubectl -n "$namespace" get cluster "$drill_name" -o jsonpath='{.status.phase}')"
ready="$(kubectl -n "$namespace" get cluster "$drill_name" -o jsonpath='{.status.readyInstances}')"
primary="$(kubectl -n "$namespace" get cluster "$drill_name" -o jsonpath='{.status.currentPrimary}')"
target_time="$(kubectl -n "$namespace" get cluster "$drill_name" -o jsonpath='{.spec.bootstrap.recovery.recoveryTarget.targetTime}')"
created_at="$(kubectl -n "$namespace" get cluster "$drill_name" -o jsonpath='{.metadata.creationTimestamp}')"

[ "$phase" = "Cluster in healthy state" ] || {
  echo "restore drill is not healthy: phase=$phase ready=${ready:-0} primary=${primary:-none}" >&2
  exit 1
}
[ "$ready" = "1" ] || { echo "restore drill is not ready: ready=${ready:-0}" >&2; exit 1; }
[ -n "$primary" ] || { echo "restore drill has no primary" >&2; exit 1; }
[ -n "$target_time" ] || { echo "restore drill lacks recoveryTarget.targetTime" >&2; exit 1; }

created_epoch="$(date -u -d "$created_at" +%s 2>/dev/null || date -j -u -f '%Y-%m-%dT%H:%M:%SZ' "$created_at" +%s)"
observed_epoch="$(date -u +%s)"
elapsed_seconds=$((observed_epoch - created_epoch))

echo "physical_restore_drill=$namespace/$drill_name"
echo "recovery_target_time=$target_time"
echo "created_at=$created_at"
echo "observed_at=$(date -u +%Y-%m-%dT%H:%M:%SZ)"
echo "observed_rto_seconds=$elapsed_seconds"
echo "primary=$primary"

# `pg_is_in_recovery = f` proves the configured recovery target was reached
# and promotion completed. Database names/sizes are operational metadata only;
# no table rows or credentials are read or emitted.
kubectl -n "$namespace" exec "$primary" -c postgres -- \
  psql -X -v ON_ERROR_STOP=1 -U postgres -d postgres -Atc \
  "SELECT 'pg_is_in_recovery=' || pg_is_in_recovery();
   SELECT 'server_version=' || current_setting('server_version');
   SELECT 'database=' || datname || '|size=' || pg_size_pretty(pg_database_size(datname))
     FROM pg_database
    WHERE datallowconn
    ORDER BY datname;"
