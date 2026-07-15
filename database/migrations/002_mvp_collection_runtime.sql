-- Runtime fields required by the PostgreSQL polling collector.
-- No secret value is introduced here: youtube_runtime_configs continues to hold only
-- an opaque environment-secret reference and a non-reversible fingerprint.

ALTER TABLE sync_jobs
  ADD COLUMN IF NOT EXISTS include_comments BOOLEAN NOT NULL DEFAULT FALSE,
  ADD COLUMN IF NOT EXISTS max_videos INTEGER CHECK (max_videos IS NULL OR max_videos > 0),
  ADD COLUMN IF NOT EXISTS max_comments_per_video INTEGER CHECK (max_comments_per_video IS NULL OR max_comments_per_video > 0),
  ADD COLUMN IF NOT EXISTS progress_completed INTEGER NOT NULL DEFAULT 0 CHECK (progress_completed >= 0),
  ADD COLUMN IF NOT EXISTS progress_total INTEGER CHECK (progress_total IS NULL OR progress_total >= 0),
  ADD COLUMN IF NOT EXISTS progress_unit TEXT NOT NULL DEFAULT 'sources'
    CHECK (progress_unit IN ('sources', 'pages', 'videos', 'comments')),
  ADD COLUMN IF NOT EXISTS checkpoint JSONB NOT NULL DEFAULT '{}',
  ADD COLUMN IF NOT EXISTS partial_errors JSONB NOT NULL DEFAULT '[]',
  ADD COLUMN IF NOT EXISTS resume_is_automatic BOOLEAN NOT NULL DEFAULT FALSE;

DROP INDEX IF EXISTS sync_jobs_claim_idx;
CREATE INDEX sync_jobs_claim_idx
  ON sync_jobs (state, resume_at, lease_expires_at, created_at)
  WHERE state IN ('queued', 'waiting_retry', 'waiting_quota', 'running');

CREATE INDEX IF NOT EXISTS source_videos_source_idx
  ON source_videos (source_id, last_seen_at DESC);
