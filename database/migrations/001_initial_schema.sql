-- Monitube initial schema.
-- The YouTube API key is a server-managed secret. This database deliberately stores
-- only an opaque Secret Manager reference/fingerprint, never a raw key or auth token.

CREATE EXTENSION IF NOT EXISTS pgcrypto;

CREATE TYPE collection_source_type AS ENUM ('channel', 'keyword', 'video');
CREATE TYPE job_state AS ENUM (
  'queued',
  'running',
  'waiting_retry',
  'waiting_quota',
  'completed',
  'completed_with_warnings',
  'failed',
  'cancelled'
);
CREATE TYPE quota_reservation_state AS ENUM ('reserved', 'consumed', 'unknown', 'released');
CREATE TYPE coverage_status AS ENUM ('complete', 'limited', 'unknown');

-- Internal-only runtime configuration. It is never exposed through the browser API.
CREATE TABLE youtube_runtime_configs (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  environment TEXT NOT NULL,
  google_project_number TEXT NOT NULL,
  secret_ref TEXT NOT NULL,
  key_fingerprint TEXT,
  approved_use_case TEXT,
  status TEXT NOT NULL DEFAULT 'active'
    CHECK (status IN ('active', 'draining', 'disabled', 'retired')),
  activated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  retired_at TIMESTAMPTZ,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE (environment, google_project_number)
);

CREATE TABLE collection_sources (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  type collection_source_type NOT NULL,
  config JSONB NOT NULL,
  config_version INTEGER NOT NULL DEFAULT 1,
  enabled BOOLEAN NOT NULL DEFAULT TRUE,
  schedule_cron TEXT,
  next_run_at TIMESTAMPTZ,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE channels (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  youtube_channel_id TEXT NOT NULL UNIQUE,
  handle TEXT,
  title TEXT,
  description TEXT,
  uploads_playlist_id TEXT,
  source_fetched_at TIMESTAMPTZ,
  expires_at TIMESTAMPTZ,
  deleted_at TIMESTAMPTZ
);

CREATE TABLE videos (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  youtube_video_id TEXT NOT NULL UNIQUE,
  channel_id UUID REFERENCES channels(id) ON DELETE SET NULL,
  title TEXT,
  description TEXT,
  published_at TIMESTAMPTZ,
  duration_seconds INTEGER CHECK (duration_seconds >= 0),
  privacy_status TEXT,
  made_for_kids BOOLEAN,
  etag TEXT,
  source_fetched_at TIMESTAMPTZ,
  expires_at TIMESTAMPTZ,
  deleted_at TIMESTAMPTZ
);

-- Preserves how a video was found, even when it appears in several sources.
CREATE TABLE source_videos (
  source_id UUID NOT NULL REFERENCES collection_sources(id) ON DELETE CASCADE,
  video_id UUID NOT NULL REFERENCES videos(id) ON DELETE CASCADE,
  first_seen_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  last_seen_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  PRIMARY KEY (source_id, video_id)
);

CREATE TABLE keyword_queries (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  source_id UUID NOT NULL UNIQUE REFERENCES collection_sources(id) ON DELETE CASCADE,
  canonical_query TEXT NOT NULL,
  query_hash TEXT NOT NULL,
  query_version INTEGER NOT NULL DEFAULT 1,
  filters JSONB NOT NULL DEFAULT '{}',
  overlap_seconds INTEGER NOT NULL DEFAULT 21600 CHECK (overlap_seconds >= 0),
  max_pages_per_run INTEGER NOT NULL DEFAULT 5 CHECK (max_pages_per_run > 0),
  last_completed_watermark TIMESTAMPTZ,
  UNIQUE (source_id, query_version)
);

CREATE TABLE keyword_search_runs (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  keyword_query_id UUID NOT NULL REFERENCES keyword_queries(id) ON DELETE CASCADE,
  job_id UUID,
  query_version INTEGER NOT NULL,
  window_start TIMESTAMPTZ NOT NULL,
  window_end TIMESTAMPTZ NOT NULL,
  next_page_token TEXT,
  page_count INTEGER NOT NULL DEFAULT 0,
  raw_result_count INTEGER NOT NULL DEFAULT 0,
  unique_result_count INTEGER NOT NULL DEFAULT 0,
  coverage coverage_status NOT NULL DEFAULT 'unknown',
  completed_at TIMESTAMPTZ,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  CHECK (window_start <= window_end)
);

CREATE TABLE keyword_search_results (
  run_id UUID NOT NULL REFERENCES keyword_search_runs(id) ON DELETE CASCADE,
  video_id UUID NOT NULL REFERENCES videos(id) ON DELETE CASCADE,
  page_number INTEGER NOT NULL CHECK (page_number >= 1),
  rank_in_page INTEGER NOT NULL CHECK (rank_in_page >= 1),
  first_seen_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  PRIMARY KEY (run_id, video_id)
);

CREATE TABLE channel_snapshots (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  channel_id UUID NOT NULL REFERENCES channels(id) ON DELETE CASCADE,
  fetched_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  subscriber_count BIGINT CHECK (subscriber_count >= 0),
  view_count BIGINT CHECK (view_count >= 0),
  video_count BIGINT CHECK (video_count >= 0),
  hidden_subscriber_count BOOLEAN,
  source_attribution TEXT NOT NULL DEFAULT 'youtube_data_api',
  expires_at TIMESTAMPTZ,
  deleted_at TIMESTAMPTZ,
  UNIQUE (channel_id, fetched_at)
);

CREATE TABLE video_stat_snapshots (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  video_id UUID NOT NULL REFERENCES videos(id) ON DELETE CASCADE,
  fetched_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  view_count BIGINT CHECK (view_count >= 0),
  like_count BIGINT CHECK (like_count >= 0),
  comment_count BIGINT CHECK (comment_count >= 0),
  favorite_count BIGINT CHECK (favorite_count >= 0),
  source_attribution TEXT NOT NULL DEFAULT 'youtube_data_api',
  expires_at TIMESTAMPTZ,
  deleted_at TIMESTAMPTZ,
  UNIQUE (video_id, fetched_at)
);

CREATE TABLE comments (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  youtube_comment_id TEXT NOT NULL UNIQUE,
  video_id UUID NOT NULL REFERENCES videos(id) ON DELETE CASCADE,
  parent_id UUID REFERENCES comments(id) ON DELETE SET NULL,
  youtube_parent_comment_id TEXT,
  youtube_thread_id TEXT,
  author_channel_id TEXT,
  author_display_name TEXT,
  text_display TEXT,
  text_original TEXT,
  like_count INTEGER NOT NULL DEFAULT 0 CHECK (like_count >= 0),
  moderation_status TEXT,
  published_at TIMESTAMPTZ,
  updated_at TIMESTAMPTZ,
  source_fetched_at TIMESTAMPTZ,
  expires_at TIMESTAMPTZ,
  deleted_at TIMESTAMPTZ
);

CREATE TABLE sync_jobs (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  source_id UUID NOT NULL REFERENCES collection_sources(id) ON DELETE CASCADE,
  runtime_config_id UUID NOT NULL REFERENCES youtube_runtime_configs(id) ON DELETE RESTRICT,
  state job_state NOT NULL DEFAULT 'queued',
  current_stage TEXT NOT NULL DEFAULT 'resolving_source',
  idempotency_key TEXT NOT NULL,
  pause_reason TEXT,
  quota_bucket TEXT CHECK (quota_bucket IN ('search_queries', 'core')),
  retry_at TIMESTAMPTZ,
  resume_at TIMESTAMPTZ,
  lease_owner TEXT,
  lease_expires_at TIMESTAMPTZ,
  cancel_requested_at TIMESTAMPTZ,
  estimated_quota JSONB NOT NULL DEFAULT '{}',
  actual_quota JSONB NOT NULL DEFAULT '{}',
  include_comments BOOLEAN NOT NULL DEFAULT FALSE,
  max_videos INTEGER CHECK (max_videos IS NULL OR max_videos > 0),
  max_comments_per_video INTEGER CHECK (max_comments_per_video IS NULL OR max_comments_per_video > 0),
  progress_completed INTEGER NOT NULL DEFAULT 0 CHECK (progress_completed >= 0),
  progress_total INTEGER CHECK (progress_total IS NULL OR progress_total >= 0),
  progress_unit TEXT NOT NULL DEFAULT 'sources'
    CHECK (progress_unit IN ('sources', 'pages', 'videos', 'comments')),
  checkpoint JSONB NOT NULL DEFAULT '{}',
  partial_errors JSONB NOT NULL DEFAULT '[]',
  resume_is_automatic BOOLEAN NOT NULL DEFAULT FALSE,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE (source_id, idempotency_key)
);

ALTER TABLE keyword_search_runs
  ADD CONSTRAINT keyword_search_runs_job_id_fkey
  FOREIGN KEY (job_id) REFERENCES sync_jobs(id) ON DELETE SET NULL;

CREATE TABLE sync_checkpoints (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  job_id UUID NOT NULL REFERENCES sync_jobs(id) ON DELETE CASCADE,
  stage TEXT NOT NULL,
  scope_key TEXT NOT NULL,
  request_hash TEXT NOT NULL,
  page_token TEXT,
  batch_cursor INTEGER NOT NULL DEFAULT 0,
  frozen_window_start TIMESTAMPTZ,
  frozen_window_end TIMESTAMPTZ,
  high_watermark TIMESTAMPTZ,
  checkpoint_seq INTEGER NOT NULL DEFAULT 0,
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE (job_id, stage, scope_key)
);

CREATE TABLE quota_windows (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  runtime_config_id UUID NOT NULL REFERENCES youtube_runtime_configs(id) ON DELETE RESTRICT,
  bucket TEXT NOT NULL CHECK (bucket IN ('search_queries', 'core')),
  window_start TIMESTAMPTZ NOT NULL,
  reset_at TIMESTAMPTZ NOT NULL,
  quota_limit INTEGER NOT NULL CHECK (quota_limit >= 0),
  reserved INTEGER NOT NULL DEFAULT 0 CHECK (reserved >= 0),
  consumed INTEGER NOT NULL DEFAULT 0 CHECK (consumed >= 0),
  unknown_consumed INTEGER NOT NULL DEFAULT 0 CHECK (unknown_consumed >= 0),
  exhausted_at TIMESTAMPTZ,
  status TEXT NOT NULL DEFAULT 'open' CHECK (status IN ('open', 'closed', 'unknown')),
  UNIQUE (runtime_config_id, bucket, window_start),
  CHECK (window_start < reset_at)
);

CREATE TABLE quota_reservations (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  quota_window_id UUID NOT NULL REFERENCES quota_windows(id) ON DELETE CASCADE,
  job_id UUID NOT NULL REFERENCES sync_jobs(id) ON DELETE CASCADE,
  request_fingerprint TEXT NOT NULL,
  endpoint TEXT NOT NULL,
  cost INTEGER NOT NULL CHECK (cost >= 0),
  state quota_reservation_state NOT NULL DEFAULT 'reserved',
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE (job_id, request_fingerprint)
);

CREATE TABLE api_request_logs (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  job_id UUID REFERENCES sync_jobs(id) ON DELETE SET NULL,
  runtime_config_id UUID REFERENCES youtube_runtime_configs(id) ON DELETE SET NULL,
  bucket TEXT NOT NULL CHECK (bucket IN ('search_queries', 'core')),
  endpoint TEXT NOT NULL,
  parameter_hash TEXT NOT NULL,
  expected_cost INTEGER NOT NULL DEFAULT 0,
  actual_cost INTEGER,
  http_status INTEGER,
  error_reason TEXT,
  latency_ms INTEGER,
  attempt INTEGER NOT NULL DEFAULT 1,
  occurred_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE quota_ledger (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  runtime_config_id UUID NOT NULL REFERENCES youtube_runtime_configs(id) ON DELETE RESTRICT,
  job_id UUID REFERENCES sync_jobs(id) ON DELETE SET NULL,
  quota_reservation_id UUID REFERENCES quota_reservations(id) ON DELETE SET NULL,
  bucket TEXT NOT NULL CHECK (bucket IN ('search_queries', 'core')),
  endpoint TEXT NOT NULL,
  entry_type TEXT NOT NULL CHECK (entry_type IN ('reserved', 'consumed', 'unknown', 'released', 'reconciled')),
  estimated_cost INTEGER NOT NULL DEFAULT 0 CHECK (estimated_cost >= 0),
  actual_cost INTEGER CHECK (actual_cost >= 0),
  occurred_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE job_events (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  job_id UUID NOT NULL REFERENCES sync_jobs(id) ON DELETE CASCADE,
  event_type TEXT NOT NULL,
  payload JSONB NOT NULL DEFAULT '{}',
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE outbox_events (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  aggregate_type TEXT NOT NULL,
  aggregate_id UUID NOT NULL,
  event_type TEXT NOT NULL,
  payload JSONB NOT NULL DEFAULT '{}',
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  published_at TIMESTAMPTZ
);

CREATE TABLE analysis_runs (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  source_id UUID REFERENCES collection_sources(id) ON DELETE SET NULL,
  state TEXT NOT NULL CHECK (state IN ('queued', 'running', 'completed', 'failed', 'cancelled')),
  pipeline_version TEXT NOT NULL,
  prompt_version TEXT,
  policy_gate_version TEXT NOT NULL,
  sample_plan JSONB NOT NULL DEFAULT '{}',
  coverage JSONB NOT NULL DEFAULT '{}',
  started_at TIMESTAMPTZ,
  completed_at TIMESTAMPTZ,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE analysis_results (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  analysis_run_id UUID NOT NULL REFERENCES analysis_runs(id) ON DELETE CASCADE,
  result_kind TEXT NOT NULL,
  payload JSONB NOT NULL,
  retention_class TEXT NOT NULL DEFAULT 'derived',
  expires_at TIMESTAMPTZ,
  deleted_at TIMESTAMPTZ,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE analysis_evidence (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  analysis_result_id UUID NOT NULL REFERENCES analysis_results(id) ON DELETE CASCADE,
  video_id UUID REFERENCES videos(id) ON DELETE SET NULL,
  comment_id UUID REFERENCES comments(id) ON DELETE SET NULL,
  excerpt TEXT,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  CHECK (video_id IS NOT NULL OR comment_id IS NOT NULL)
);

CREATE TABLE deletion_jobs (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  target_type TEXT NOT NULL CHECK (target_type IN ('source', 'channel', 'video', 'comment', 'analysis')),
  target_id UUID,
  reason TEXT NOT NULL,
  deadline_at TIMESTAMPTZ NOT NULL,
  completed_at TIMESTAMPTZ,
  evidence JSONB NOT NULL DEFAULT '{}',
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX collection_sources_due_idx ON collection_sources (next_run_at)
  WHERE enabled = TRUE;
CREATE INDEX videos_channel_published_idx ON videos (channel_id, published_at DESC);
CREATE INDEX source_videos_video_idx ON source_videos (video_id, source_id);
CREATE INDEX channel_snapshots_channel_fetched_idx ON channel_snapshots (channel_id, fetched_at DESC);
CREATE INDEX video_stat_snapshots_video_fetched_idx ON video_stat_snapshots (video_id, fetched_at DESC);
CREATE INDEX comments_video_published_idx ON comments (video_id, published_at DESC);
CREATE INDEX comments_parent_idx ON comments (parent_id) WHERE parent_id IS NOT NULL;
CREATE INDEX keyword_search_runs_query_window_idx ON keyword_search_runs (keyword_query_id, window_end DESC);
CREATE INDEX sync_jobs_resume_idx ON sync_jobs (resume_at)
  WHERE state IN ('waiting_quota', 'waiting_retry');
CREATE INDEX sync_jobs_claim_idx ON sync_jobs (state, resume_at, lease_expires_at, created_at)
  WHERE state IN ('queued', 'waiting_quota', 'waiting_retry', 'running');
CREATE INDEX quota_windows_reset_idx ON quota_windows (reset_at)
  WHERE status <> 'open';
CREATE INDEX api_request_logs_config_bucket_idx ON api_request_logs (runtime_config_id, bucket, occurred_at DESC);
CREATE INDEX quota_ledger_config_bucket_idx ON quota_ledger (runtime_config_id, bucket, occurred_at DESC);
CREATE INDEX analysis_runs_source_created_idx ON analysis_runs (source_id, created_at DESC);
CREATE INDEX analysis_results_run_idx ON analysis_results (analysis_run_id, created_at);
CREATE INDEX deletion_jobs_due_idx ON deletion_jobs (deadline_at)
  WHERE completed_at IS NULL;
CREATE INDEX outbox_unpublished_idx ON outbox_events (created_at)
  WHERE published_at IS NULL;
