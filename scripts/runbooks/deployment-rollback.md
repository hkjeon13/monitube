# Deployment rollback runbook

`scripts/deploy_remote.sh` creates a release state directory at:

```text
/data/psyche/backups/monitube/releases/<UTC timestamp>-<current SHA prefix>
```

It records previous/current SHAs, immutable rollback image tags, the previous
Compose file, previous application image IDs, previous feature flags, previous
PostgreSQL command and `shm_size`, PostgreSQL settings, migration checks, row
counts, and the verified backup path.

## Automatic behavior

After cutover begins, an API liveness/readiness or worker-start failure causes
the script to:

1. restore the pre-deploy performance flags;
2. restore the previous PostgreSQL command and `shm_size` when they changed;
3. recreate the previous API and wait for `/health`;
4. recreate web and the prior collection/analysis worker replica counts.

Expand-only migrations are retained. The database dump is never restored
automatically because doing so could discard writes committed after deployment.

## Manual application rollback

Select one exact state directory and inspect, rather than source, its files.
They contain no application credential but are mode 0600.

```sh
STATE_DIR=/data/psyche/backups/monitube/releases/YYYYMMDDTHHMMSSZ-abcdef123456
sed -n '1,20p' "$STATE_DIR/release.env"
sed -n '1,40p' "$STATE_DIR/application-images.previous"
```

Set `MONITUBE_IMAGE_TAG` to the recorded `rollback_image_tag`, then use the
saved Compose definition with the live server `.env`:

```sh
export MONITUBE_IMAGE_TAG=rollback-YYYYMMDDTHHMMSSZ-abcdef123456
export MONITUBE_YOUTUBE_SECRET_ENV_FILE=/data/psyche/.config/monitube/youtube.env
cd /data/psyche/Projects/monitube
docker compose --project-directory "$PWD" --env-file .env -f "$STATE_DIR/docker-compose.previous.yml" up --detach --force-recreate --no-deps api
docker compose --project-directory "$PWD" --env-file .env -f "$STATE_DIR/docker-compose.previous.yml" exec -T api python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=3)"
docker compose --project-directory "$PWD" --env-file .env -f "$STATE_DIR/docker-compose.previous.yml" up --detach --force-recreate --no-deps web
docker compose --project-directory "$PWD" --env-file .env -f "$STATE_DIR/docker-compose.previous.yml" up --detach --force-recreate --no-deps --scale worker=2 worker
```

Use the recorded prior replica count rather than assuming `2` if it differed.

## PostgreSQL runtime rollback

Only use the saved runtime override when the deployment changed or recreated
PostgreSQL. It restores the old container command and shared-memory size while
keeping the same named data volume.

```sh
docker compose --project-directory "$PWD" --env-file .env -f "$STATE_DIR/docker-compose.previous.yml" -f "$STATE_DIR/postgres-runtime.previous.yml" up --detach --force-recreate --no-deps postgres
docker compose --project-directory "$PWD" --env-file .env -f "$STATE_DIR/docker-compose.previous.yml" exec -T postgres sh -ceu 'pg_isready -U "$POSTGRES_USER" -d "$POSTGRES_DB"'
```

Do not delete new indexes immediately and do not reverse expand-only migrations
in the incident window. After rollback, compare row counts/latest timestamps,
inspect active and queued jobs, confirm two-worker lease recovery, check WAL and
disk use, and run the legacy endpoint/auth smoke tests.
