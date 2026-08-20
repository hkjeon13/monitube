#!/usr/bin/env bash
set -Eeuo pipefail

# Publishes infra/k8s/monitube to the in-cluster Helm repository that Devtron
# installs from.
#
# That repository is not ChartMuseum and has no upload API. It is an nginx
# Deployment (codex-helm-repo, namespace devtroncd) serving the ConfigMap
# codex-helm-repo-content from /usr/share/nginx/html. Publishing therefore means
# editing that ConfigMap.
#
# The tarball MUST go into binaryData. Two existing keys,
# platform-database-0.3.0.tgz and platform-database-0.4.0.tgz, were written to
# `data` instead and are 0 bytes as a result: a gzip archive is not valid UTF-8,
# so the API server stored nothing. Those keys are left untouched here.
#
# Requires kubectl with write access to the devtroncd namespace, and helm.

readonly CHART_DIR="infra/k8s/monitube"
readonly NAMESPACE="devtroncd"
readonly CONFIGMAP="codex-helm-repo-content"
readonly REPO_URL="http://codex-helm-repo.devtroncd.svc.cluster.local"
# The API server rejects a ConfigMap larger than 1 MiB.
readonly MAX_CONFIGMAP_BYTES=$((1024 * 1024))

log() { printf '[publish-chart] %s\n' "$*"; }
die() { printf '[publish-chart] error: %s\n' "$*" >&2; exit 1; }

command -v helm >/dev/null 2>&1 || die "helm is required."
command -v kubectl >/dev/null 2>&1 || die "kubectl is required."
command -v python3 >/dev/null 2>&1 || die "python3 is required."
[ -f "${CHART_DIR}/Chart.yaml" ] || die "run this from the repository root."

kubectl -n "$NAMESPACE" get configmap "$CONFIGMAP" >/dev/null \
  || die "cannot read ${NAMESPACE}/${CONFIGMAP}. Check the kubeconfig context."

version="$(helm show chart "$CHART_DIR" | python3 -c 'import sys,yaml; print(yaml.safe_load(sys.stdin)["version"])')"
tarball="monitube-${version}.tgz"
log "chart version ${version}"

# Devtron will not pick up a re-uploaded tarball at a version it already indexes.
if kubectl -n "$NAMESPACE" get configmap "$CONFIGMAP" \
     -o "jsonpath={.binaryData.${tarball//./\\.}}" | grep -q .; then
  die "${tarball} is already published. Bump version in ${CHART_DIR}/Chart.yaml."
fi

workdir="$(mktemp -d)"
trap 'rm -rf "$workdir"' EXIT

helm lint "$CHART_DIR"
helm package "$CHART_DIR" --destination "$workdir" >/dev/null
[ -f "${workdir}/${tarball}" ] || die "expected ${tarball} after packaging."

# Merge into the live index rather than regenerating it. The index references 35
# tarballs while only two are actually present, and regenerating from the
# directory would silently drop every entry whose file is missing.
kubectl -n "$NAMESPACE" get configmap "$CONFIGMAP" \
  -o 'jsonpath={.data.index\.yaml}' > "${workdir}/old-index.yaml"
[ -s "${workdir}/old-index.yaml" ] || die "index.yaml is empty in the ConfigMap."

( cd "$workdir" && helm repo index . --merge old-index.yaml --url "$REPO_URL" )

python3 - "$workdir" "$tarball" "$MAX_CONFIGMAP_BYTES" <<'PY' > "${workdir}/patch.json"
import base64, json, os, sys
workdir, tarball, max_bytes = sys.argv[1], sys.argv[2], int(sys.argv[3])
index = open(os.path.join(workdir, "index.yaml"), encoding="utf-8").read()
encoded = base64.b64encode(open(os.path.join(workdir, tarball), "rb").read()).decode()
patch = {"data": {"index.yaml": index}, "binaryData": {tarball: encoded}}
added = len(index) + len(encoded)
if added > max_bytes:
    sys.exit(f"chart plus index is {added} bytes, over the ConfigMap limit")
json.dump(patch, sys.stdout)
PY

log "patching ${NAMESPACE}/${CONFIGMAP}"
kubectl -n "$NAMESPACE" patch configmap "$CONFIGMAP" \
  --type merge --patch-file "${workdir}/patch.json"

size="$(kubectl -n "$NAMESPACE" get configmap "$CONFIGMAP" -o json | wc -c | tr -d ' ')"
log "ConfigMap is now ~${size} bytes of ${MAX_CONFIGMAP_BYTES}"

cat <<EOF

Published ${tarball}.

nginx serves the ConfigMap as a whole-volume mount, so the kubelet refreshes it
without a restart. Allow about a minute, then use "Refetch Charts" in Devtron
before deploying.

Verify it is actually fetchable before relying on it:

  kubectl -n ${NAMESPACE} exec deploy/codex-helm-repo -- \\
    wget -qS -O /dev/null ${REPO_URL}/${tarball}
EOF
