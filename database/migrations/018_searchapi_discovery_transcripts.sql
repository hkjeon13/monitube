-- SearchAPI.io discovery provenance and selected video transcripts.
-- Existing comments remain on the official YouTube Data API path.

CREATE TABLE IF NOT EXISTS provider_request_logs (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  job_id UUID REFERENCES sync_jobs(id) ON DELETE SET NULL,
  provider TEXT NOT NULL,
  operation TEXT NOT NULL,
  status_code INTEGER NOT NULL,
  error_code TEXT,
  item_count INTEGER CHECK (item_count IS NULL OR item_count >= 0),
  requested_language TEXT,
  resolved_language TEXT,
  occurred_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS provider_request_logs_job_idx
  ON provider_request_logs (job_id, occurred_at DESC);

CREATE TABLE IF NOT EXISTS channel_provider_profiles (
  channel_id UUID NOT NULL REFERENCES channels(id) ON DELETE CASCADE,
  provider TEXT NOT NULL,
  keywords TEXT,
  tags JSONB NOT NULL DEFAULT '[]',
  available_countries JSONB NOT NULL DEFAULT '[]',
  badges JSONB NOT NULL DEFAULT '[]',
  is_verified BOOLEAN,
  is_family_safe BOOLEAN,
  banner_url TEXT,
  avatar_url TEXT,
  external_links JSONB NOT NULL DEFAULT '[]',
  joined_date DATE,
  source_fetched_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  PRIMARY KEY (channel_id, provider)
);

ALTER TABLE keyword_search_results
  ADD COLUMN IF NOT EXISTS provider TEXT NOT NULL DEFAULT 'youtube_data_api',
  ADD COLUMN IF NOT EXISTS provider_result_kind TEXT,
  ADD COLUMN IF NOT EXISTS provider_position INTEGER,
  ADD COLUMN IF NOT EXISTS provider_section TEXT,
  ADD COLUMN IF NOT EXISTS preview_title TEXT,
  ADD COLUMN IF NOT EXISTS preview_thumbnail_url TEXT,
  ADD COLUMN IF NOT EXISTS provider_published_text TEXT;

CREATE TABLE IF NOT EXISTS video_transcripts (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  video_id UUID NOT NULL REFERENCES videos(id) ON DELETE CASCADE,
  provider TEXT NOT NULL,
  requested_language TEXT NOT NULL,
  resolved_language TEXT,
  language_name TEXT,
  selection_reason TEXT,
  transcript_type TEXT,
  is_auto_generated BOOLEAN,
  is_translated BOOLEAN,
  state TEXT NOT NULL CHECK (state IN ('available', 'unavailable', 'retryable_error', 'failed')),
  full_text TEXT,
  content_hash TEXT,
  fetched_at TIMESTAMPTZ,
  last_attempted_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  error_code TEXT,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE (video_id, provider)
);

CREATE TABLE IF NOT EXISTS video_transcript_segments (
  transcript_id UUID NOT NULL REFERENCES video_transcripts(id) ON DELETE CASCADE,
  sequence INTEGER NOT NULL CHECK (sequence >= 0),
  start_ms INTEGER NOT NULL CHECK (start_ms >= 0),
  duration_ms INTEGER NOT NULL CHECK (duration_ms >= 0),
  text TEXT NOT NULL,
  PRIMARY KEY (transcript_id, sequence)
);
