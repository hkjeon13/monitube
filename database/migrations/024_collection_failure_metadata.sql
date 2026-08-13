-- Durable, user-safe failure classification for Rust collection jobs.
-- Additive and backwards compatible: previous API/worker images ignore these
-- columns while new images can expose the existing optional error contract.

ALTER TABLE sync_jobs
  ADD COLUMN IF NOT EXISTS retry_count INTEGER NOT NULL DEFAULT 0
    CHECK (retry_count >= 0),
  ADD COLUMN IF NOT EXISTS last_error_code TEXT,
  ADD COLUMN IF NOT EXISTS last_error_provider TEXT,
  ADD COLUMN IF NOT EXISTS last_error_operation TEXT,
  ADD COLUMN IF NOT EXISTS last_error_retryable BOOLEAN,
  ADD COLUMN IF NOT EXISTS last_error_http_status INTEGER
    CHECK (last_error_http_status IS NULL OR
           last_error_http_status BETWEEN 100 AND 599),
  ADD COLUMN IF NOT EXISTS last_error_at TIMESTAMPTZ;

CREATE INDEX IF NOT EXISTS sync_jobs_last_error_code_idx
  ON sync_jobs (last_error_code, updated_at DESC)
  WHERE last_error_code IS NOT NULL;
