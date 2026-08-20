# CloudNativePG migration runbook

Moves the `monitube` database from the `postgres:16-alpine` StatefulSet in
`monitube-prod` to a CloudNativePG cluster, without losing the data already
there. The chart is `infra/k8s/monitube`.

## What is actually running

Read from the cluster on 2026-08-20. Confirm anything that looks stale before
acting on it.

| | |
|---|---|
| Cluster | `default_cluster`, one k3s node (`mobichat-k3s-1`) |
| Namespace | `monitube-prod`, Devtron Helm release `monitube` |
| Database | StatefulSet `monitube-postgres`, `postgres:16-alpine`, Service `postgres:5432` |
| Storage | PVC `monitube-postgres`, 100Gi, StorageClass `local-path`, Bound |
| Credentials | Secret `monitube-postgres-auth` (`POSTGRES_DB`/`USER`/`PASSWORD`) |
| App config | Secret `monitube-runtime-env`, holds `DATABASE_URL` |
| CNPG | Operator and CRDs already installed, including the Barman Cloud plugin |

Two things about the current state are worth knowing before you start.

**The deployed chart cannot be re-fetched.** Devtron reports
`monitube-0.1.6.tgz : 404 Not Found` from `codex-helm-repo.devtroncd`. The
running release works, but Devtron cannot render or upgrade it. The chart in
this repository replaces it and reproduces the live workloads exactly.

**The `local-path` StorageClass no longer exists.** Only
`database-local-retain` and `mobichat-local-retain` are defined. The legacy PVC
is already Bound so it keeps working, but it cannot be recreated. Do not delete
it until the migration is finished and verified.

## Prerequisites

Confirm the database name and user, which the CNPG import needs and which this
runbook assumes are both `monitube`:

```sh
kubectl -n monitube-prod get secret monitube-postgres-auth -o jsonpath='{.data.POSTGRES_USER}' | base64 -d; echo
```

If they differ, set `legacyPostgres.user` and `legacyPostgres.database` in
values before deploying.

## 1. Record the expected state

These are the acceptance criteria for step 4.

```sh
kubectl -n monitube-prod exec monitube-postgres-0 -- psql -X -U monitube -d monitube -At -c "SELECT (SELECT count(*) FROM videos), (SELECT count(*) FROM comments), (SELECT count(*) FROM monitube_schema_migrations)"
```

The ledger should hold one row per file in `database/migrations/` — 25 as of
`025_whitespace_token_metrics.sql`.

Take an independent archive as well. The import does not modify the source, but
this is the only restore point that survives a mistake on the PVC:

```sh
kubectl -n monitube-prod exec monitube-postgres-0 -- pg_dump -U monitube -Fc monitube > monitube-pre-cnpg.dump
```

## 2. Publish the chart

Devtron installs from the `local-services` repository, so the chart has to be
packaged and uploaded before it can be deployed. See
`infra/k8s/monitube/README.md` for the exact commands.

## 3. Create the CNPG cluster and import

Deploy with CNPG enabled but the applications still pointing at the legacy
database:

```yaml
cnpg: {enabled: true}
database: {useCnpg: false}
legacyPostgres: {enabled: true}
```

Nothing changes for the running application at this step. CNPG creates
`monitube-db`, runs `pg_dump`/`pg_restore` from the `postgres` Service, and
generates the secret `monitube-db-app` holding a fresh password and a ready-made
`uri` key.

```sh
kubectl -n monitube-prod get cluster monitube-db -w
```

The import runs before the instance reports ready, so expect it to take about as
long as a manual dump and restore. If it stalls:

```sh
kubectl -n monitube-prod logs -l cnpg.io/jobRole=full-recovery --tail=200
```

Note that this crosses a major version: the source is PostgreSQL 16 and the
house image is 17. `pg_dump`/`pg_restore` supports that direction, but it is a
real upgrade — step 4 is not optional.

## 4. Verify before cutting over

```sh
kubectl -n monitube-prod exec monitube-db-1 -- psql -X -U monitube -d monitube
```

Confirm every one of these.

- Row counts and the ledger count match step 1 exactly.
- `SELECT extname FROM pg_extension` returns `pgcrypto`, `pg_trgm`, and
  `pg_stat_statements`.
- `SHOW shared_preload_libraries` includes `pg_stat_statements`. Worth checking
  explicitly — if it is missing the extension still exists but collects nothing,
  and the performance runbooks quietly stop working.
- `SHOW max_connections` returns 60, `SHOW log_parameter_max_length` returns 0.
- Korean text in `videos.title` renders correctly. The legacy database was
  initialised without an explicit locale; a mismatch shows up here first.

Do not continue if any check fails. A partial import cannot be repaired in
place — delete the `Cluster`, fix the cause, and repeat step 3.

## 5. Cut over

```yaml
database: {useCnpg: true}
```

This adds an explicit `DATABASE_URL` to the api and the three workers, sourced
from `monitube-db-app`. Kubernetes gives an explicit `env` entry precedence over
the same key arriving through `envFrom`, so the stale value inside
`monitube-runtime-env` is overridden without anyone editing that secret or
handling the password.

Workers hold row leases and deploy with the `Recreate` strategy, so each one
stops before its replacement starts. Watch the rollout, then confirm the api is
serving from the new database:

```sh
kubectl -n monitube-prod rollout status deploy/monitube-api deploy/monitube-collection-worker deploy/monitube-nlp-worker deploy/monitube-analysis-worker
```

Leave the legacy StatefulSet running but idle. It is the fastest rollback
available, and it still holds every row as of step 1.

## 6. Clean up, only after a full collection cycle

```yaml
legacyPostgres: {enabled: false}
```

Then tidy the parts the chart does not own:

- Update `DATABASE_URL` inside `monitube-runtime-env` so the secret stops
  carrying a value that no longer resolves.
- Keep PVC `monitube-postgres` until you are certain. Its StorageClass no longer
  exists, so deleting it is irreversible.
- Consider a `ScheduledBackup` and an `ObjectStore` for `monitube-db`. The
  `database` namespace already does this through `central-pg-tokyo-s3`;
  `monitube-db` currently has no backup configured at all.

## Rollback

Before step 5, rollback is just redeploying with `cnpg.enabled: false` — nothing
touched the legacy database.

After step 5, the two databases diverge as soon as a worker writes. Rolling back
then means setting `database.useCnpg: false` and accepting the loss of anything
written to CNPG in the interim. Decide which way the data should flow before
restarting the workers, not after.
