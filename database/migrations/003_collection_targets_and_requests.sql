-- Canonical collection targets and request coalescing.
--
-- This is an expand-only migration.  Existing collection_sources, sync_jobs,
-- source_videos, and normalized YouTube entities remain authoritative legacy
-- history; no legacy row is deleted, truncated, or rewritten in place.  The
-- nullable target_id columns deliberately keep the old API deployable while
-- read/write paths move to the target/request model.

CREATE TABLE IF NOT EXISTS collection_targets (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  type collection_source_type NOT NULL,
  canonical_key TEXT NOT NULL,
  config JSONB NOT NULL DEFAULT '{}',
  coverage JSONB NOT NULL DEFAULT '{}',
  resolved_channel_id UUID REFERENCES channels(id) ON DELETE SET NULL,
  last_completed_at TIMESTAMPTZ,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  CONSTRAINT collection_targets_canonical_key_not_blank CHECK (btrim(canonical_key) <> ''),
  CONSTRAINT collection_targets_type_canonical_key_key UNIQUE (type, canonical_key)
);

-- A source remains a compatibility/audit record.  New collection code can use
-- this pointer while legacy routes continue to address the source by its UUID.
ALTER TABLE collection_sources
  ADD COLUMN IF NOT EXISTS target_id UUID REFERENCES collection_targets(id) ON DELETE SET NULL;

-- A job must eventually be owned by a canonical target.  It stays nullable so
-- in-flight duplicate legacy jobs can be retained without violating the active
-- target uniqueness constraint below.
ALTER TABLE sync_jobs
  ADD COLUMN IF NOT EXISTS target_id UUID REFERENCES collection_targets(id) ON DELETE SET NULL;

CREATE TABLE IF NOT EXISTS collection_target_aliases (
  target_id UUID NOT NULL REFERENCES collection_targets(id) ON DELETE CASCADE,
  target_type collection_source_type NOT NULL,
  alias_kind TEXT NOT NULL,
  alias_value TEXT NOT NULL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  PRIMARY KEY (target_id, alias_kind, alias_value),
  CONSTRAINT collection_target_aliases_value_not_blank CHECK (btrim(alias_value) <> ''),
  CONSTRAINT collection_target_aliases_target_type_kind_value_key UNIQUE (target_type, alias_kind, alias_value)
);

CREATE TABLE IF NOT EXISTS collection_target_videos (
  target_id UUID NOT NULL REFERENCES collection_targets(id) ON DELETE CASCADE,
  video_id UUID NOT NULL REFERENCES videos(id) ON DELETE CASCADE,
  first_seen_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  last_seen_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  PRIMARY KEY (target_id, video_id)
);

CREATE TABLE IF NOT EXISTS collection_requests (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  target_id UUID NOT NULL REFERENCES collection_targets(id) ON DELETE CASCADE,
  -- Exactly one backfilled request points at each legacy source.  New request
  -- records may omit source_id once the legacy source abstraction is retired.
  source_id UUID UNIQUE REFERENCES collection_sources(id) ON DELETE SET NULL,
  request_config JSONB NOT NULL DEFAULT '{}',
  idempotency_key TEXT,
  job_id UUID REFERENCES sync_jobs(id) ON DELETE SET NULL,
  status TEXT NOT NULL DEFAULT 'queued'
    CHECK (status IN (
      'queued', 'joined', 'running', 'completed', 'completed_with_warnings',
      'waiting_retry', 'waiting_quota', 'failed', 'cancelled', 'superseded'
    )),
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS collection_sources_target_idx
  ON collection_sources (target_id, created_at);
CREATE INDEX IF NOT EXISTS collection_targets_resolved_channel_idx
  ON collection_targets (resolved_channel_id)
  WHERE resolved_channel_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS collection_target_aliases_target_idx
  ON collection_target_aliases (target_id, created_at);
CREATE INDEX IF NOT EXISTS collection_target_videos_video_idx
  ON collection_target_videos (video_id, target_id);
CREATE INDEX IF NOT EXISTS collection_requests_target_created_idx
  ON collection_requests (target_id, created_at DESC);
CREATE INDEX IF NOT EXISTS collection_requests_job_idx
  ON collection_requests (job_id)
  WHERE job_id IS NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS collection_requests_target_idempotency_key_idx
  ON collection_requests (target_id, idempotency_key)
  WHERE idempotency_key IS NOT NULL;
CREATE INDEX IF NOT EXISTS sync_jobs_target_created_idx
  ON sync_jobs (target_id, created_at DESC)
  WHERE target_id IS NOT NULL;

-- Work out a conservative legacy target for every existing source.  A channel
-- source is merged only when every available signal identifies one channel:
-- linked videos, an exact channel ID input, or an exact stored handle.  Unknown
-- channel inputs remain grouped by the same normalized input rather than being
-- guessed into an unrelated channel.
CREATE TEMP TABLE monitube_legacy_source_target_map (
  source_id UUID PRIMARY KEY,
  type collection_source_type NOT NULL,
  canonical_key TEXT NOT NULL,
  config JSONB NOT NULL,
  resolved_channel_id UUID,
  source_created_at TIMESTAMPTZ NOT NULL,
  target_id UUID
) ON COMMIT PRESERVE ROWS;

WITH channel_candidates AS (
  SELECT cs.id AS source_id, c.id AS channel_id, c.youtube_channel_id
  FROM collection_sources cs
  JOIN source_videos sv ON sv.source_id = cs.id
  JOIN videos v ON v.id = sv.video_id
  JOIN channels c ON c.id = v.channel_id
  WHERE cs.type = 'channel'

  UNION

  SELECT cs.id AS source_id, c.id AS channel_id, c.youtube_channel_id
  FROM collection_sources cs
  JOIN channels c ON c.youtube_channel_id = btrim(COALESCE(cs.config ->> 'input', ''))
  WHERE cs.type = 'channel'

  UNION

  SELECT cs.id AS source_id, c.id AS channel_id, c.youtube_channel_id
  FROM collection_sources cs
  JOIN channels c
    ON btrim(COALESCE(cs.config ->> 'input', '')) LIKE '@%'
   AND lower(btrim(COALESCE(c.handle, ''))) = lower(btrim(COALESCE(cs.config ->> 'input', '')))
  WHERE cs.type = 'channel'
),
resolved_channels AS (
  SELECT
    source_id,
    CASE WHEN count(DISTINCT channel_id) = 1 THEN min(channel_id::text)::uuid END AS channel_id,
    CASE WHEN count(DISTINCT channel_id) = 1 THEN min(youtube_channel_id) END AS youtube_channel_id
  FROM channel_candidates
  GROUP BY source_id
)
INSERT INTO monitube_legacy_source_target_map (
  source_id, type, canonical_key, config, resolved_channel_id, source_created_at
)
SELECT
  cs.id,
  cs.type,
  CASE
    WHEN cs.type = 'channel' AND rc.youtube_channel_id IS NOT NULL
      THEN 'channel:' || rc.youtube_channel_id
    WHEN cs.type = 'channel'
      AND btrim(COALESCE(cs.config ->> 'input', '')) ~ '^UC[0-9A-Za-z_-]{22}$'
      THEN 'channel:' || btrim(cs.config ->> 'input')
    WHEN cs.type = 'channel' AND btrim(COALESCE(cs.config ->> 'input', '')) LIKE '@%'
      THEN 'channel:handle:' || lower(btrim(cs.config ->> 'input'))
    WHEN cs.type = 'channel' AND btrim(COALESCE(cs.config ->> 'input', '')) <> ''
      THEN 'channel:ambiguous_name:' || lower(btrim(cs.config ->> 'input'))
    WHEN cs.type = 'keyword' AND btrim(COALESCE(cs.config ->> 'query', '')) <> ''
      THEN 'keyword:' || encode(
        digest(
          array_to_string(
            ARRAY[
              lower(regexp_replace(btrim(cs.config ->> 'query'), '[[:space:]]+', ' ', 'g')),
              COALESCE(cs.config ->> 'publishedAfter', ''),
              COALESCE(cs.config ->> 'publishedBefore', ''),
              upper(btrim(COALESCE(cs.config ->> 'regionCode', ''))),
              lower(btrim(COALESCE(cs.config ->> 'relevanceLanguage', ''))),
              COALESCE(cs.config ->> 'order', 'date')
            ],
            chr(31)
          ),
          'sha256'
        ),
        'hex'
      )
    WHEN cs.type = 'video'
      AND btrim(COALESCE(cs.config ->> 'input', '')) ~ '^[0-9A-Za-z_-]{11}$'
      THEN 'video:' || btrim(cs.config ->> 'input')
    ELSE 'source:' || cs.id::text
  END,
  cs.config,
  rc.channel_id,
  cs.created_at
FROM collection_sources cs
LEFT JOIN resolved_channels rc ON rc.source_id = cs.id;

-- The earliest legacy source provides the initial target config.  Config and
-- coverage are intentionally separate: requested collection breadth is merged
-- below and never becomes a second physical target.
INSERT INTO collection_targets (
  type, canonical_key, config, resolved_channel_id, created_at, updated_at
)
SELECT DISTINCT ON (type, canonical_key)
  type, canonical_key, config, resolved_channel_id, source_created_at, source_created_at
FROM monitube_legacy_source_target_map
ORDER BY type, canonical_key, source_created_at, source_id
ON CONFLICT (type, canonical_key) DO UPDATE
SET resolved_channel_id = COALESCE(collection_targets.resolved_channel_id, EXCLUDED.resolved_channel_id);

UPDATE monitube_legacy_source_target_map map
SET target_id = target.id
FROM collection_targets target
WHERE target.type = map.type
  AND target.canonical_key = map.canonical_key;

UPDATE collection_sources source
SET target_id = map.target_id,
    updated_at = now()
FROM monitube_legacy_source_target_map map
WHERE source.id = map.source_id
  AND source.target_id IS DISTINCT FROM map.target_id;

-- Alias entries make legacy handles/IDs discoverable without treating a mutable
-- handle as the permanent identity.  Canonical channel IDs remain the durable
-- keys for resolved targets.
INSERT INTO collection_target_aliases (target_id, target_type, alias_kind, alias_value)
SELECT target_id, type, 'legacy_source', source_id::text
FROM monitube_legacy_source_target_map
ON CONFLICT (target_type, alias_kind, alias_value) DO NOTHING;

INSERT INTO collection_target_aliases (target_id, target_type, alias_kind, alias_value)
SELECT target.id, target.type, 'channel_id', channel.youtube_channel_id
FROM collection_targets target
JOIN channels channel ON channel.id = target.resolved_channel_id
WHERE target.type = 'channel'
ON CONFLICT (target_type, alias_kind, alias_value) DO NOTHING;

INSERT INTO collection_target_aliases (target_id, target_type, alias_kind, alias_value)
SELECT
  source.target_id,
  source.type,
  CASE
    WHEN source.type = 'channel' AND btrim(COALESCE(source.config ->> 'input', '')) ~ '^UC[0-9A-Za-z_-]{22}$' THEN 'channel_id'
    WHEN source.type = 'channel' AND btrim(COALESCE(source.config ->> 'input', '')) LIKE '@%' THEN 'handle'
    WHEN source.type = 'channel' THEN 'input'
    WHEN source.type = 'video' THEN 'video_id'
  END,
  CASE
    WHEN source.type = 'channel' AND btrim(COALESCE(source.config ->> 'input', '')) LIKE '@%'
      THEN lower(btrim(source.config ->> 'input'))
    ELSE btrim(source.config ->> 'input')
  END
FROM collection_sources source
WHERE source.target_id IS NOT NULL
  AND source.type IN ('channel', 'video')
  AND btrim(COALESCE(source.config ->> 'input', '')) <> ''
ON CONFLICT (target_type, alias_kind, alias_value) DO NOTHING;

-- Copy, do not move, legacy source/video membership to its canonical target.
-- This preserves source provenance and lets target-level reads deduplicate data.
INSERT INTO collection_target_videos (target_id, video_id, first_seen_at, last_seen_at)
SELECT
  source.target_id,
  source_video.video_id,
  min(source_video.first_seen_at),
  max(source_video.last_seen_at)
FROM source_videos source_video
JOIN collection_sources source ON source.id = source_video.source_id
WHERE source.target_id IS NOT NULL
GROUP BY source.target_id, source_video.video_id
ON CONFLICT (target_id, video_id) DO UPDATE
SET first_seen_at = LEAST(collection_target_videos.first_seen_at, EXCLUDED.first_seen_at),
    last_seen_at = GREATEST(collection_target_videos.last_seen_at, EXCLUDED.last_seen_at);

-- A legacy request records the original user-facing scope.  The latest job is
-- retained for history/display but does not make the request a unique target.
INSERT INTO collection_requests (
  target_id, source_id, request_config, job_id, status, created_at, updated_at
)
SELECT
  source.target_id,
  source.id,
  source.config,
  latest_job.id,
  COALESCE(latest_job.state::text, 'queued'),
  source.created_at,
  source.updated_at
FROM collection_sources source
LEFT JOIN LATERAL (
  SELECT id, state
  FROM sync_jobs
  WHERE source_id = source.id
  ORDER BY created_at DESC, id DESC
  LIMIT 1
) latest_job ON TRUE
WHERE source.target_id IS NOT NULL
ON CONFLICT (source_id) DO NOTHING;

-- Backfill job ownership.  If legacy jobs for the same target are concurrently
-- active, retain all rows but attach only one winner (a running job wins over a
-- waiting/queued job).  The other jobs still retain their source/request audit
-- link and remain nullable until the worker/API transition handles them.
WITH ranked_jobs AS (
  SELECT
    job.id,
    source.target_id,
    job.state,
    row_number() OVER (
      PARTITION BY source.target_id,
        CASE WHEN job.state IN ('queued', 'running', 'waiting_retry', 'waiting_quota') THEN TRUE ELSE FALSE END
      ORDER BY
        CASE job.state
          WHEN 'running' THEN 0
          WHEN 'waiting_retry' THEN 1
          WHEN 'waiting_quota' THEN 2
          WHEN 'queued' THEN 3
          ELSE 4
        END,
        job.created_at DESC,
        job.id DESC
    ) AS active_rank
  FROM sync_jobs job
  JOIN collection_sources source ON source.id = job.source_id
  WHERE source.target_id IS NOT NULL
)
UPDATE sync_jobs job
SET target_id = CASE
  WHEN ranked_jobs.state IN ('queued', 'running', 'waiting_retry', 'waiting_quota')
       AND ranked_jobs.active_rank > 1
    THEN NULL
  ELSE ranked_jobs.target_id
END
FROM ranked_jobs
WHERE job.id = ranked_jobs.id
  AND job.target_id IS NULL;

-- The coverage record is intentionally labelled legacy_unverified: a source
-- request limit proves requested breadth, not that every item/comment page was
-- successfully fetched.  It is nevertheless enough for the new API to decide
-- whether a refresh is likely necessary.
WITH requested_scope AS (
  SELECT
    source.target_id,
    max(
      CASE WHEN COALESCE(source.config ->> 'maxVideos', '') ~ '^[0-9]+$'
        THEN (source.config ->> 'maxVideos')::integer
      END
    ) AS source_max_videos,
    bool_or(
      CASE lower(COALESCE(source.config ->> 'includeComments', 'false'))
        WHEN 'true' THEN TRUE ELSE FALSE
      END
      OR COALESCE(job.include_comments, FALSE)
    ) AS requested_include_comments,
    max(
      CASE WHEN COALESCE(source.config ->> 'maxCommentPagesPerVideo', '') ~ '^[0-9]+$'
        THEN (source.config ->> 'maxCommentPagesPerVideo')::integer
      END
    ) AS source_max_comment_pages,
    max(
      CASE WHEN COALESCE(source.config ->> 'maxPagesPerRun', '') ~ '^[0-9]+$'
        THEN (source.config ->> 'maxPagesPerRun')::integer
      END
    ) AS source_max_pages,
    max(job.max_videos) AS job_max_videos,
    max(job.max_comments_per_video) AS job_max_comment_pages,
    max(job.updated_at) FILTER (WHERE job.state IN ('completed', 'completed_with_warnings')) AS last_completed_at
  FROM collection_sources source
  LEFT JOIN sync_jobs job ON job.source_id = source.id
  WHERE source.target_id IS NOT NULL
  GROUP BY source.target_id
),
observed_videos AS (
  SELECT target_id, count(*)::integer AS video_count
  FROM collection_target_videos
  GROUP BY target_id
)
UPDATE collection_targets target
SET config = target.config || jsonb_strip_nulls(
      jsonb_build_object(
        'maxVideos', GREATEST(requested_scope.source_max_videos, requested_scope.job_max_videos),
        'includeComments', requested_scope.requested_include_comments,
        'maxCommentPagesPerVideo', GREATEST(requested_scope.source_max_comment_pages, requested_scope.job_max_comment_pages),
        'maxPagesPerRun', requested_scope.source_max_pages
      )
    ),
    coverage = target.coverage || jsonb_strip_nulls(
      jsonb_build_object(
        'status', 'legacy_unverified',
        'requestedMaxVideos', GREATEST(requested_scope.source_max_videos, requested_scope.job_max_videos),
        'requestedIncludeComments', requested_scope.requested_include_comments,
        'requestedMaxCommentPagesPerVideo', GREATEST(requested_scope.source_max_comment_pages, requested_scope.job_max_comment_pages),
        'requestedMaxPagesPerRun', requested_scope.source_max_pages,
        'observedVideoCount', COALESCE(observed_videos.video_count, 0),
        'backfilledAt', now()
      )
    ),
    last_completed_at = CASE
      WHEN requested_scope.last_completed_at IS NULL THEN target.last_completed_at
      WHEN target.last_completed_at IS NULL THEN requested_scope.last_completed_at
      ELSE GREATEST(target.last_completed_at, requested_scope.last_completed_at)
    END,
    updated_at = now()
FROM requested_scope
LEFT JOIN observed_videos ON observed_videos.target_id = requested_scope.target_id
WHERE target.id = requested_scope.target_id;

-- Enforce the invariant for all target-aware jobs created after this migration.
-- Legacy duplicate work is preserved with a NULL target_id as documented above.
CREATE UNIQUE INDEX IF NOT EXISTS sync_jobs_one_active_target_idx
  ON sync_jobs (target_id)
  WHERE target_id IS NOT NULL
    AND state IN ('queued', 'running', 'waiting_retry', 'waiting_quota');

-- Fail atomically if the migration could not build the compatibility links.
DO $$
BEGIN
  IF EXISTS (
    SELECT 1
    FROM collection_sources source
    WHERE source.target_id IS NULL
  ) THEN
    RAISE EXCEPTION 'collection target backfill left one or more legacy sources without target_id';
  END IF;

  IF EXISTS (
    SELECT 1
    FROM collection_sources source
    LEFT JOIN collection_requests request ON request.source_id = source.id
    WHERE request.id IS NULL
  ) THEN
    RAISE EXCEPTION 'collection request backfill left one or more legacy sources without a request record';
  END IF;

  IF EXISTS (
    SELECT target_id
    FROM sync_jobs
    WHERE target_id IS NOT NULL
      AND state IN ('queued', 'running', 'waiting_retry', 'waiting_quota')
    GROUP BY target_id
    HAVING count(*) > 1
  ) THEN
    RAISE EXCEPTION 'collection target backfill left duplicate active target jobs';
  END IF;
END;
$$;
