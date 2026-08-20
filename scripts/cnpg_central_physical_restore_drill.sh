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
backup_id="${RECOVERY_BACKUP_ID:-}"
drill_name="${1:-central-pg-data-restore-$(date -u +%Y%m%d-%H%M%S)}"

case "$drill_name" in
  central-pg-data-restore-[0-9][0-9][0-9][0-9][0-9][0-9][0-9][0-9]-[0-9][0-9][0-9][0-9][0-9][0-9]) ;;
  *) echo "drill name must use central-pg-data-restore-YYYYMMDD-HHMMSS" >&2; exit 2 ;;
esac
command -v kubectl >/dev/null 2>&1 || { echo "missing required command: kubectl" >&2; exit 2; }
[ -n "$backup_id" ] || {
  echo "RECOVERY_BACKUP_ID is required (Barman backup ID, for example 20260819T180000)" >&2
  exit 2
}
case "$backup_id" in
  [0-9][0-9][0-9][0-9][0-9][0-9][0-9][0-9]T[0-9][0-9][0-9][0-9][0-9][0-9]) ;;
  *) echo "RECOVERY_BACKUP_ID must use Barman ID YYYYMMDDTHHMMSS" >&2; exit 2 ;;
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
      # Restore exactly the named completed base backup and promote as soon as
      # it reaches a consistent state. This never follows the active WAL tail.
      recoveryTarget:
        backupID: "$backup_id"
        targetImmediate: true
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
