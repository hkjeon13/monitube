-- Performance foundation for bounded source reads, asynchronous summaries,
-- comment rollups, and indexed search.  This migration is intentionally
-- expand-only: every new read/write path can be disabled without removing data.

CREATE EXTENSION IF NOT EXISTS pg_stat_statements;
CREATE EXTENSION IF NOT EXISTS pg_trgm;

ALTER TABLE collection_targets
  ADD COLUMN IF NOT EXISTS data_version BIGINT NOT NULL DEFAULT 0;

ALTER TABLE collection_sources
  ADD COLUMN IF NOT EXISTS data_version BIGINT NOT NULL DEFAULT 0;

ALTER TABLE analysis_runs
  ADD COLUMN IF NOT EXISTS target_id UUID REFERENCES collection_targets(id) ON DELETE CASCADE,
  ADD COLUMN IF NOT EXISTS job_id UUID REFERENCES sync_jobs(id) ON DELETE SET NULL,
  ADD COLUMN IF NOT EXISTS data_version BIGINT NOT NULL DEFAULT 0,
  ADD COLUMN IF NOT EXISTS lease_owner TEXT,
  ADD COLUMN IF NOT EXISTS lease_expires_at TIMESTAMPTZ,
  ADD COLUMN IF NOT EXISTS retry_count INTEGER NOT NULL DEFAULT 0,
  ADD COLUMN IF NOT EXISTS resume_at TIMESTAMPTZ,
  ADD COLUMN IF NOT EXISTS last_error TEXT;

-- Preserve prior deterministic summaries as stale version-zero seeds.  They
-- remain useful while the first target-aware analysis run is being built.
UPDATE analysis_runs analysis
SET target_id = source.target_id
FROM collection_sources source
WHERE analysis.source_id = source.id
  AND analysis.target_id IS NULL
  AND source.target_id IS NOT NULL;

CREATE INDEX IF NOT EXISTS analysis_runs_claim_idx
  ON analysis_runs (state, resume_at, lease_expires_at, created_at)
  WHERE state IN ('queued', 'running');

CREATE INDEX IF NOT EXISTS analysis_runs_target_completed_idx
  ON analysis_runs (target_id, data_version DESC, completed_at DESC)
  WHERE state = 'completed' AND target_id IS NOT NULL;

CREATE INDEX IF NOT EXISTS analysis_runs_legacy_completed_idx
  ON analysis_runs (source_id, data_version DESC, completed_at DESC)
  WHERE state = 'completed' AND target_id IS NULL AND source_id IS NOT NULL;

CREATE UNIQUE INDEX IF NOT EXISTS analysis_runs_target_v2_version_idx
  ON analysis_runs (target_id, data_version, pipeline_version)
  WHERE target_id IS NOT NULL AND pipeline_version = 'deterministic-v2';

CREATE UNIQUE INDEX IF NOT EXISTS analysis_runs_legacy_v2_version_idx
  ON analysis_runs (source_id, data_version, pipeline_version)
  WHERE target_id IS NULL AND source_id IS NOT NULL
    AND pipeline_version = 'deterministic-v2';

CREATE UNIQUE INDEX IF NOT EXISTS analysis_results_run_kind_idx
  ON analysis_results (analysis_run_id, result_kind)
  WHERE deleted_at IS NULL;

CREATE TABLE IF NOT EXISTS video_comment_rollups (
  video_id UUID PRIMARY KEY REFERENCES videos(id) ON DELETE CASCADE,
  stored_count BIGINT NOT NULL DEFAULT 0 CHECK (stored_count >= 0),
  top_level_count BIGINT NOT NULL DEFAULT 0 CHECK (top_level_count >= 0),
  reply_count BIGINT NOT NULL DEFAULT 0 CHECK (reply_count >= 0),
  latest_published_at TIMESTAMPTZ,
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  last_reconciled_at TIMESTAMPTZ,
  CHECK (stored_count = top_level_count + reply_count)
);

CREATE TABLE IF NOT EXISTS maintenance_backfills (
  name TEXT PRIMARY KEY,
  state TEXT NOT NULL CHECK (state IN ('schema_ready', 'dual_write_enabled', 'backfill_running', 'reconciling', 'ready', 'failed')),
  cursor TEXT,
  processed BIGINT NOT NULL DEFAULT 0,
  total BIGINT,
  last_error TEXT,
  started_at TIMESTAMPTZ,
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  completed_at TIMESTAMPTZ
);

INSERT INTO maintenance_backfills (name, state, total)
VALUES ('video_comment_rollups', 'schema_ready', (SELECT count(*) FROM videos))
ON CONFLICT (name) DO NOTHING;

CREATE TABLE IF NOT EXISTS video_search_documents (
  video_id UUID PRIMARY KEY REFERENCES videos(id) ON DELETE CASCADE,
  document TEXT NOT NULL,
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- A trigger keeps the normalized video document current without coupling
-- every repository caller to the search implementation.  Channel metadata
-- changes refresh all affected documents in a bounded SQL statement.
CREATE OR REPLACE FUNCTION monitube_refresh_video_search_document()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
  IF TG_OP = 'UPDATE'
     AND OLD.youtube_video_id IS NOT DISTINCT FROM NEW.youtube_video_id
     AND OLD.channel_id IS NOT DISTINCT FROM NEW.channel_id
     AND OLD.title IS NOT DISTINCT FROM NEW.title
     AND OLD.description IS NOT DISTINCT FROM NEW.description THEN
    RETURN NEW;
  END IF;
  INSERT INTO video_search_documents (video_id, document, updated_at)
  SELECT v.id,
         lower(concat_ws(' ', v.youtube_video_id, v.title, v.description, c.youtube_channel_id, c.title, c.handle)),
         now()
  FROM videos v
  LEFT JOIN channels c ON c.id = v.channel_id
  WHERE v.id = NEW.id
  ON CONFLICT (video_id) DO UPDATE
  SET document = EXCLUDED.document, updated_at = EXCLUDED.updated_at;
  RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS videos_refresh_search_document ON videos;
CREATE TRIGGER videos_refresh_search_document
AFTER INSERT OR UPDATE OF youtube_video_id, channel_id, title, description ON videos
FOR EACH ROW EXECUTE FUNCTION monitube_refresh_video_search_document();

CREATE OR REPLACE FUNCTION monitube_refresh_channel_search_documents()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
  -- Video fanout upserts the same minimal channel metadata for every child.
  -- PostgreSQL fires an UPDATE OF trigger when a column appears in SET even if
  -- its value is unchanged, so this guard prevents channel-size-squared search
  -- document/Gin maintenance during a full refresh.
  IF OLD.youtube_channel_id IS NOT DISTINCT FROM NEW.youtube_channel_id
     AND OLD.title IS NOT DISTINCT FROM NEW.title
     AND OLD.handle IS NOT DISTINCT FROM NEW.handle THEN
    RETURN NEW;
  END IF;
  INSERT INTO video_search_documents (video_id, document, updated_at)
  SELECT v.id,
         lower(concat_ws(' ', v.youtube_video_id, v.title, v.description, NEW.youtube_channel_id, NEW.title, NEW.handle)),
         now()
  FROM videos v
  WHERE v.channel_id = NEW.id
  ON CONFLICT (video_id) DO UPDATE
  SET document = EXCLUDED.document, updated_at = EXCLUDED.updated_at;
  RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS channels_refresh_video_search_documents ON channels;
CREATE TRIGGER channels_refresh_video_search_documents
AFTER UPDATE OF youtube_channel_id, title, handle ON channels
FOR EACH ROW EXECUTE FUNCTION monitube_refresh_channel_search_documents();

INSERT INTO video_search_documents (video_id, document, updated_at)
SELECT v.id,
       lower(concat_ws(' ', v.youtube_video_id, v.title, v.description, c.youtube_channel_id, c.title, c.handle)),
       now()
FROM videos v
LEFT JOIN channels c ON c.id = v.channel_id
ON CONFLICT (video_id) DO UPDATE
SET document = EXCLUDED.document, updated_at = EXCLUDED.updated_at;

CREATE INDEX IF NOT EXISTS video_search_documents_trgm_idx
  ON video_search_documents USING gin (document gin_trgm_ops);

CREATE INDEX IF NOT EXISTS comments_text_display_trgm_idx
  ON comments USING gin (lower(COALESCE(text_display, '')) gin_trgm_ops)
  WHERE text_display IS NOT NULL;

CREATE INDEX IF NOT EXISTS comments_author_recent_idx
  ON comments (author_channel_id, published_at DESC NULLS LAST, source_fetched_at DESC)
  WHERE author_channel_id IS NOT NULL;

CREATE INDEX IF NOT EXISTS comments_parent_effective_published_idx
  ON comments (
    youtube_parent_comment_id,
    COALESCE(published_at, source_fetched_at, 'epoch'::timestamptz),
    youtube_comment_id
  )
  WHERE youtube_parent_comment_id IS NOT NULL;

CREATE INDEX IF NOT EXISTS videos_effective_published_idx
  ON videos (
    COALESCE(published_at, source_fetched_at, 'epoch'::timestamptz) DESC,
    youtube_video_id DESC
  );

CREATE INDEX IF NOT EXISTS videos_channel_fetched_idx
  ON videos (channel_id, source_fetched_at DESC);

CREATE INDEX IF NOT EXISTS sync_jobs_source_created_idx
  ON sync_jobs (source_id, created_at DESC);

CREATE INDEX IF NOT EXISTS sync_jobs_target_parent_created_idx
  ON sync_jobs (target_id, parent_job_id, created_at DESC)
  WHERE target_id IS NOT NULL;

CREATE INDEX IF NOT EXISTS collection_target_videos_target_seen_idx
  ON collection_target_videos (target_id, first_seen_at, video_id);

CREATE INDEX IF NOT EXISTS source_videos_source_seen_idx
  ON source_videos (source_id, first_seen_at, video_id);
