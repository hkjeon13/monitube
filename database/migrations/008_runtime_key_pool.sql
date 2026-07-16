-- Server-managed same-project key pool. Raw values are encrypted with pgcrypto
-- and are never selected by browser-facing API routes.
CREATE TABLE IF NOT EXISTS youtube_runtime_keys (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  runtime_config_id UUID NOT NULL REFERENCES youtube_runtime_configs(id) ON DELETE CASCADE,
  key_fingerprint TEXT NOT NULL,
  encrypted_key BYTEA NOT NULL,
  status TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'cooling_down', 'disabled')),
  failure_count INTEGER NOT NULL DEFAULT 0,
  last_error_reason TEXT,
  last_used_at TIMESTAMPTZ,
  unavailable_until TIMESTAMPTZ,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE (runtime_config_id, key_fingerprint)
);
CREATE INDEX IF NOT EXISTS youtube_runtime_keys_available_idx
  ON youtube_runtime_keys (runtime_config_id, status, unavailable_until);
