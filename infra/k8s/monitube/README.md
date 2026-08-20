# monitube chart

Kubernetes deployment for Monitube, installed through Devtron into the
`monitube-prod` namespace.

The chart reproduces the workloads running in `monitube-prod` today — image
tags, environment, ports, probes, and resource requests were read from the live
cluster and match it exactly. It exists because the chart the release was
installed from, `monitube-0.1.6.tgz`, is no longer present in
`codex-helm-repo.devtroncd`: the release runs, but Devtron can neither render
nor upgrade it. This is not specific to Monitube — of the 35 tarballs that
repository's index references, only two are actually stored.

## Layout

| Template | Contents |
|---|---|
| `workloads.yaml` | web, api, tokenizer, redis, and the three Rust workers |
| `services.yaml` | `api`, `api-rust`, `tokenizer`, `redis`, `postgres`, and the NodePort `monitube` |
| `legacy-postgres.yaml` | the `postgres:16-alpine` StatefulSet holding the data today |
| `cnpg-cluster.yaml` | older dedicated-Cluster path; not used for the central DB migration |
| `job-migrate.yaml` | `apply_migrations.sh` as a post-install/post-upgrade hook |

Service names are deliberately bare. `DATABASE_URL`, `TOKENIZER_BASE_URL`, and
`REDIS_URL` inside the `monitube-runtime-env` secret resolve `postgres`,
`tokenizer`, and `redis` by those names — renaming a Service breaks the release.

## The database switch

The central migration uses one explicit application switch. See
`scripts/runbooks/cnpg-migration.md` for the gated sequence.

| Value | Default | Meaning |
|---|---|---|
| `database.mode` | `legacy` | keep the legacy URL from `monitube-runtime-env` |
| `database.mode` | `central` | use `database.central.secretName/uriKey` in `monitube-prod` |
| `legacyPostgres.enabled` | `true` | retain the legacy DB through restore drill and soak |

The defaults describe what is running now, so a deploy with no value changes is
a no-op against the live cluster.

The older release persisted `database.useCnpg` rather than `database.mode`.
When Helm upgrades with `--reuse-values`, an absent `database.mode` is treated
as `legacy` explicitly; it never selects central by accident. Set
`database.mode=central` only in the approved cutover release.

Central mode adds an explicit `DATABASE_URL` sourced from a Monitube-specific
Secret in the **same namespace** as the workloads. Kubernetes gives an explicit
`env` entry precedence over the same key arriving through `envFrom`, so the
legacy value stays intact for rollback. The chart never reads a Secret from the
`database` namespace directly.

## Secrets

The chart creates none. It expects these to already exist in `monitube-prod`:

- `monitube-runtime-env` — application configuration and API credentials
- `monitube-postgres-auth` — legacy database credentials; CNPG also reads
  `POSTGRES_PASSWORD` from it to authenticate the import
- `monitube-central-db` — provisioned separately in `monitube-prod` before
  central mode; it must have `uri`, `host`, `port`, `dbname`, `username`, and
  `password` keys, but the chart never creates or stores their values
- `devtron-local-registry-pull` — pull secret for `192.168.219.103:30500`

## Validating a change

```sh
helm lint infra/k8s/monitube
```

```sh
helm template monitube infra/k8s/monitube --set database.mode=central
```

## Publishing to Devtron

Devtron installs from the `local-services` repository, served by
`codex-helm-repo` in the `devtroncd` namespace. It is **not** ChartMuseum and
has no upload API: it is an nginx Deployment serving the ConfigMap
`codex-helm-repo-content` from `/usr/share/nginx/html`. Publishing means editing
that ConfigMap.

`scripts/publish_chart.sh` does this. Bump `version` in `Chart.yaml` first —
Devtron will not pick up a re-uploaded tarball at a version it already indexes,
and the script refuses to overwrite one.

```sh
./scripts/publish_chart.sh
```

It needs `kubectl` with write access to `devtroncd`, which is not configured on
the workstation this chart was written on.

Three things about that repository are easy to get wrong by hand, and the script
handles all three:

- **The tarball goes in `binaryData`, never `data`.** A gzip archive is not
  valid UTF-8, so a `data` key silently stores nothing. That is exactly what
  happened to `platform-database-0.3.0.tgz` and `platform-database-0.4.0.tgz`,
  which are both 0 bytes.
- **`index.yaml` must be merged, not regenerated.** It references 35 tarballs
  while only two are present; regenerating from a directory would drop every
  entry whose file is missing.
- **The ConfigMap cannot exceed 1 MiB.** It currently sits near 76 KB, and this
  chart adds about 10 KB.

nginx mounts the ConfigMap as a whole volume, so the kubelet refreshes it
without a restart. Wait about a minute, then use **Refetch Charts** in Devtron.

## Known rough edges in the running release

Neither is caused by this chart, and neither is fixed by it.

- `monitube-runtime-env` contains container build variables that were captured
  along with the real configuration — `GPG_KEY`, `PYTHON_SHA256`, `LD_LIBRARY_PATH`,
  `PATH`, and similar. They are inert, but they make the secret hard to read and
  hard to audit.
- `monitube-web` and `monitube-api` bind `hostPort` on 127.0.0.1 (13000 and
  18000) alongside their Services. Nothing in the chart depends on those
  bindings, but they are reproduced under `hostPorts` so an upgrade does not
  silently remove them. Set `hostPorts.enabled: false` once you have confirmed
  nothing on the node uses them.
