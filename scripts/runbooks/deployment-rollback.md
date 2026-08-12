# Rust conversion deployment rollback runbook

`scripts/deploy_remote.sh` creates a release state directory at:

```text
/data/psyche/backups/monitube/releases/<UTC timestamp>-<current SHA prefix>
```

It records previous/current SHAs, immutable rollback image tags, the previous
Compose file, previous Python and Rust application image IDs and replica counts,
the previous web API proxy target, previous feature flags, previous PostgreSQL
command and `shm_size`, PostgreSQL settings, migration checks, row counts, and
the verified backup path.

## Automatic behavior

After cutover begins, an API liveness/readiness, tokenizer, maintenance,
worker-start, lease invariant, or soak failure causes the script to:

1. restore the pre-deploy performance flags and previous web API proxy target;
2. stop the new Rust API and queue consumers;
3. restore the previous PostgreSQL command and `shm_size` when they changed;
4. recreate the previous Python or Rust API and verify its health;
5. recreate web, tokenizer, and the exact prior Python/Rust worker replica counts.

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
saved Compose definition with the live server `.env`. The following example is
for a previous Python runtime; use the recorded Rust service names and counts if
the prior release was already Rust:

```sh
export MONITUBE_IMAGE_TAG=rollback-YYYYMMDDTHHMMSSZ-abcdef123456
export MONITUBE_YOUTUBE_SECRET_ENV_FILE=/data/psyche/.config/monitube/youtube.env
cd /data/psyche/Projects/monitube
COMPOSE="docker compose --project-directory $PWD --env-file .env -f $STATE_DIR/docker-compose.previous.yml"
$COMPOSE up --detach --force-recreate --no-deps api
$COMPOSE exec -T api python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=3)"
$COMPOSE up --detach --force-recreate --no-deps web
$COMPOSE up --detach --force-recreate --no-deps --scale worker=2 worker
$COMPOSE up --detach --force-recreate --no-deps --scale analysis-worker=1 analysis-worker
```

Use every recorded prior replica count rather than assuming the example values.
If the prior runtime was Rust, enable the saved Compose profiles and restore
`api-rust`, `nlp-worker-rust`, `collection-worker-rust`, and
`analysis-worker-rust`. Do not run Python and Rust consumers for the same queue
at the same time.

Before recreating web manually, restore
`MONITUBE_WEB_API_PROXY_TARGET_DOCKER` from
`runtime-settings.previous.env`; otherwise web can point at the failed API.

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
inspect active and queued jobs, confirm lease recovery, check WAL and disk use,
and run the endpoint/auth smoke tests.

The `023_pure_frequency_ranking.sql` migration is expand/replace-only from the
application perspective and remains after rollback. It removes the obsolete
DF-first ranking index and installs the raw-frequency index; neither the Rust nor
the corrected Python read path calculates an IDF-derived score.
