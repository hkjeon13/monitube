-- Browser-console accounts: username and PBKDF2 password hash only.
CREATE TABLE IF NOT EXISTS app_users (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  username TEXT NOT NULL UNIQUE CHECK (username ~ '^[A-Za-z0-9_-]{3,32}$'),
  password_hash TEXT NOT NULL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS app_sessions (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id UUID NOT NULL REFERENCES app_users(id) ON DELETE CASCADE,
  token_hash TEXT NOT NULL UNIQUE,
  expires_at TIMESTAMPTZ NOT NULL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS app_sessions_user_expires_idx ON app_sessions (user_id, expires_at);

-- Existing collection history is claimed once by the first account registered
-- through the web console. No account or password is seeded by this migration.
ALTER TABLE collection_sources ADD COLUMN IF NOT EXISTS owner_id UUID REFERENCES app_users(id) ON DELETE RESTRICT;
ALTER TABLE collection_targets ADD COLUMN IF NOT EXISTS owner_id UUID REFERENCES app_users(id) ON DELETE RESTRICT;
CREATE INDEX IF NOT EXISTS collection_sources_owner_idx ON collection_sources (owner_id, created_at);
CREATE INDEX IF NOT EXISTS collection_targets_owner_idx ON collection_targets (owner_id, created_at);
