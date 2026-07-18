# Database backup and restore runbook

Use this runbook for the custom-format archives created by
`scripts/deploy_remote.sh`. A deployment that changes migrations or PostgreSQL
runtime configuration writes three files under `/data/psyche/backups/monitube`:

- `*.dump`: custom-format PostgreSQL archive, mode 0600
- `*.dump.sha256`: archive checksum
- `*.dump.list`: output of `pg_restore --list`

The deployment also records the selected archive path in its mode-0700 release
state directory. Archives are never deleted automatically.

## Verify an archive

Set `BACKUP_PATH` to one exact reviewed archive. Do not use a wildcard.

```sh
BACKUP_PATH=/data/psyche/backups/monitube/monitube-pre-change-YYYYMMDDTHHMMSSZ.dump
sha256sum --check "${BACKUP_PATH}.sha256"
test -s "${BACKUP_PATH}.list"
```

`pg_restore --list` was already run during deployment. To repeat it with the
same PostgreSQL tool version without exposing a password:

```sh
cd /data/psyche/Projects/monitube
docker compose exec -T postgres pg_restore --list < "$BACKUP_PATH" > /tmp/monitube-restore-list.txt
test -s /tmp/monitube-restore-list.txt
```

## Restore drill in a disposable database

Choose a unique database name containing only letters, digits, and underscores.
The commands below do not modify the production `monitube` database.

```sh
RESTORE_DB=monitube_restore_verify_YYYYMMDD
cd /data/psyche/Projects/monitube
docker compose exec -T postgres sh -ceu 'createdb -U "$POSTGRES_USER" "$1"' sh "$RESTORE_DB"
docker compose exec -T postgres sh -ceu 'pg_restore -U "$POSTGRES_USER" -d "$1" --no-owner --no-privileges --exit-on-error' sh "$RESTORE_DB" < "$BACKUP_PATH"
docker compose exec -T postgres sh -ceu 'psql -X -U "$POSTGRES_USER" -d "$1" -At -c "SELECT (SELECT count(*) FROM videos), (SELECT count(*) FROM comments);"' sh "$RESTORE_DB"
```

Compare the restored row counts with `database-counts.before` in the matching
release state directory. Inspect constraints and the migration ledger before
declaring the drill successful.

Dropping the disposable database is destructive. Confirm that `RESTORE_DB` is
the exact drill database and never `monitube` before running:

```sh
test "$RESTORE_DB" != monitube
docker compose exec -T postgres sh -ceu 'dropdb -U "$POSTGRES_USER" "$1"' sh "$RESTORE_DB"
```

## Production restore policy

Do not restore over the active database and do not add `--clean` to an ad-hoc
command. Stop API and workers, restore into a newly named database, reconcile
counts and application smoke tests, then switch `DATABASE_URL_DOCKER` during an
approved maintenance window. A logical backup does not include Redis or MinIO;
those are handled independently.

Copy every pre-change archive and checksum to approved off-host storage. Verify
the copied checksum there. Backup retention and off-host deletion require a
separate reviewed policy; this deploy script intentionally performs no cleanup.
