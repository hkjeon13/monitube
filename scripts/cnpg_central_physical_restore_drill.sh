#!/bin/sh
# Create a disposable one-instance CNPG restore drill from central-pg-data's
# Barman ObjectStore. It never mutates the running central cluster.
set -eu

namespace="${TARGET_NAMESPACE:-database}"
source_cluster="${TARGET_CLUSTER:-central-pg-data}"
object_store="${OBJECT_STORE:-central-pg-tokyo-s3}"
image="${CNPG_IMAGE:-192.168.219.103:30500/cnpg-pg17-pgvector:17.11-0.8.2}"
storage_class="${CNPG_STORAGE_CLASS:-database-local-retain}"
storage_size="${CNPG_STORAGE_SIZE:-40Gi}"
recovery_target_time="${RECOVERY_TARGET_TIME:-}"
drill_name="${1:-central-pg-data-restore-$(date -u +%Y%m%d-%H%M%S)}"

case "$drill_name" in
  central-pg-data-restore-[0-9][0-9][0-9][0-9][0-9][0-9][0-9][0-9]-[0-9][0-9][0-9][0-9][0-9][0-9]) ;;
  *) echo "drill name must use central-pg-data-restore-YYYYMMDD-HHMMSS" >&2; exit 2 ;;
esac
command -v kubectl >/dev/null 2>&1 || { echo "missing required command: kubectl" >&2; exit 2; }
[ -n "$recovery_target_time" ] || {
  echo "RECOVERY_TARGET_TIME is required (RFC3339 UTC offset, for example 2026-08-20T06:15:00+00:00)" >&2
  exit 2
}
case "$recovery_target_time" in
  # CNPG writes this value verbatim to PostgreSQL's recovery_target_time.
  # PostgreSQL 17 accepts an explicit UTC offset here, but not a trailing Z.
  [0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9]+00:00) ;;
  *) echo "RECOVERY_TARGET_TIME must use RFC3339 UTC YYYY-MM-DDTHH:MM:SS+00:00" >&2; exit 2 ;;
esac

source_phase="$(kubectl -n "$namespace" get cluster "$source_cluster" -o jsonpath='{.status.phase}')"
[ "$source_phase" = "Cluster in healthy state" ] || {
  echo "source cluster is not healthy: $source_phase" >&2
  exit 1
}
[ "$(kubectl -n "$namespace" get objectstore "$object_store" -o name)" = "objectstore.barmancloud.cnpg.io/$object_store" ] || {
  echo "missing ObjectStore: $namespace/$object_store" >&2
  exit 1
}
kubectl -n "$namespace" get cluster "$drill_name" >/dev/null 2>&1 && {
  echo "restore drill already exists: $drill_name" >&2
  exit 1
}

cat <<EOF | kubectl -n "$namespace" apply -f -
apiVersion: postgresql.cnpg.io/v1
kind: Cluster
metadata:
  name: $drill_name
  labels:
    app.kubernetes.io/part-of: monitube-cnpg-migration
    monitube.fin-ally.net/purpose: physical-restore-drill
spec:
  instances: 1
  imageName: $image
  imagePullPolicy: IfNotPresent
  bootstrap:
    recovery:
      source: source
      # An unbounded recovery follows the WAL archive indefinitely and never
      # yields a promotable drill result while the source is active.
      recoveryTarget:
        targetTime: "$recovery_target_time"
  externalClusters:
    - name: source
      plugin:
        name: barman-cloud.cloudnative-pg.io
        parameters:
          barmanObjectName: $object_store
          serverName: $source_cluster
  storage:
    storageClass: $storage_class
    size: $storage_size
EOF

echo "created physical restore drill: $namespace/$drill_name"
echo "verify with: kubectl -n $namespace get cluster $drill_name -w"
echo "do not delete its retained storage until the restore evidence is recorded."
