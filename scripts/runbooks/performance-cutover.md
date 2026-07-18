# Performance feature cutover runbook

All performance flags default to `false`. The remote deploy promotes the
foundation-safe subset only after migration and schema checks:

- source overview v2 and video keyset pagination;
- comment batch writes and rollup dual-write;
- target summary writes and the analysis worker;
- trigram search after document/index reconciliation;
- target summary reads only when every active target has a completed result for
  its current data version.

Rollup reads, Explore rollups, and Redis-derived cache remain disabled until a
separate reconciliation gate passes.

Set `MONITUBE_PROMOTE_SAFE_FLAGS=false` in the deployment command environment
to perform a code/schema rollout without any automatic flag promotion. This is
a deploy-process control and is intentionally not read from the application
`.env`.

After the deploy has enabled dual-write, start or resume the bounded backfill as
a one-off worker. The durable cursor and advisory lock make this restart-safe:

```sh
cd /data/psyche/Projects/monitube
export MONITUBE_IMAGE_TAG="$(git rev-parse HEAD)"
export MONITUBE_YOUTUBE_SECRET_ENV_FILE=/data/psyche/.config/monitube/youtube.env
docker compose run --rm --no-deps worker python -m monitube_worker.rollup_backfill
```

Use `ROLLUP_BACKFILL_BATCH_SIZE`, `ROLLUP_BACKFILL_SLEEP_SECONDS`,
`ROLLUP_BACKFILL_LOCK_TIMEOUT_MS`, and
`ROLLUP_BACKFILL_MAX_RECONCILE_PASSES` as command-environment overrides when
collection latency requires a slower pass. Do not run two copies intentionally.

## Rollup read gate

Run during a low-traffic window after the resumable backfill reports `ready`.
The result must be `t|0|0` before enabling a rollup read.

```sh
cd /data/psyche/Projects/monitube
docker compose exec -T postgres sh -ceu 'psql -X -U "$POSTGRES_USER" -d "$POSTGRES_DB" -At -F"|" -c "
WITH raw AS (
  SELECT video_id,
         count(*)::bigint AS stored_count,
         count(*) FILTER (WHERE youtube_parent_comment_id IS NULL)::bigint AS top_level_count,
         count(*) FILTER (WHERE youtube_parent_comment_id IS NOT NULL)::bigint AS reply_count,
         max(COALESCE(published_at, source_fetched_at)) AS latest_published_at
  FROM comments
  GROUP BY video_id
), mismatch AS (
  SELECT rollup.video_id
  FROM video_comment_rollups rollup
  LEFT JOIN raw ON raw.video_id = rollup.video_id
  WHERE rollup.stored_count <> COALESCE(raw.stored_count, 0)
     OR rollup.top_level_count <> COALESCE(raw.top_level_count, 0)
     OR rollup.reply_count <> COALESCE(raw.reply_count, 0)
     OR rollup.latest_published_at IS DISTINCT FROM raw.latest_published_at
)
SELECT
  EXISTS (SELECT 1 FROM maintenance_backfills WHERE name = '"'"'video_comment_rollups'"'"' AND state = '"'"'ready'"'"'),
  (SELECT count(*) FROM videos video LEFT JOIN video_comment_rollups rollup ON rollup.video_id = video.id WHERE rollup.video_id IS NULL),
  (SELECT count(*) FROM mismatch);
"'
```

After passing the gate, set only `ENABLE_COMMENT_ROLLUP_READ=true`, recreate the
API, and monitor p95, errors, pool wait, mismatch checks, queue age, and disk.
Rollback is setting that flag to `false` and recreating API; keep the table and
indexes for diagnosis.

## Target summary read gate

The analysis worker seeds current target/source versions after deployment. Do
not enable summary reads merely because the worker is running. The following
query must return `0`; failed versions remain terminal and require diagnosis or
a newer collection version rather than automatic infinite requeueing.

```sh
docker compose exec -T postgres sh -ceu 'psql -X -U "$POSTGRES_USER" -d "$POSTGRES_DB" -At -c "
SELECT count(*)
FROM collection_targets target
WHERE EXISTS (SELECT 1 FROM collection_sources source WHERE source.target_id = target.id)
  AND NOT EXISTS (
    SELECT 1
    FROM analysis_runs run
    JOIN analysis_results result ON result.analysis_run_id = run.id
    WHERE run.target_id = target.id
      AND run.data_version = target.data_version
      AND run.pipeline_version = '"'"'deterministic-v2'"'"'
      AND run.state = '"'"'completed'"'"'
      AND result.result_kind = '"'"'basic_summary'"'"'
      AND result.deleted_at IS NULL
      AND (result.expires_at IS NULL OR result.expires_at > now())
  );
"'
```

After the result is zero, set `ENABLE_TARGET_SUMMARY_READ=true`, recreate only
API, and verify that exact counts still match raw SQL while `topWordsStatus` is
`fresh`. Turning the flag off returns immediately to exact SQL plus the previous
top-word fallback.

## Explore rollup and Redis cache gates

Enable `ENABLE_EXPLORE_ROLLUP=true` only after the rollup read gate has passed.
Compare at least one multi-target user's channel/video/comment totals with the
raw owner-visible DISTINCT-video query before and after the switch.

Redis is a bounded, regenerable cache (`maxmemory=512mb`, `allkeys-lru`, AOF
off), never an ACL or source-of-truth store. Before setting
`ENABLE_REDIS_DERIVED_CACHE=true`, confirm `redis-cli ping`, API `/ready`, owner
scope tests, TTL expiry, and PostgreSQL fallback with Redis stopped. Monitor the
readiness cache status and hit/miss/error counters. A cache incident is rolled
back by setting only this flag to `false` and recreating API; no Redis restore is
required.

## Query-statistics baseline

The deployment enables `pg_stat_statements`, I/O timing, 500 ms slow-query logs,
and 64 MiB temp-file logs. It does not reset statistics unless
`MONITUBE_RESET_PG_STAT_STATEMENTS=true` is explicitly supplied. Record the reset
timestamp, workload window, route p95/p99, rows, temp blocks, WAL growth, pool
wait, and API/PostgreSQL RSS together; cumulative values without a time window
are not a valid before/after comparison.
