-- Global pins turn a canonical collection target into a periodically refreshed
-- subscription.  A pin is intentionally target-scoped (not browser/session
-- scoped), so equivalent channel inputs share one scheduled collection job.

CREATE TABLE IF NOT EXISTS collection_target_pins (
  target_id UUID PRIMARY KEY REFERENCES collection_targets(id) ON DELETE CASCADE,
  interval_minutes INTEGER NOT NULL DEFAULT 360 CHECK (interval_minutes BETWEEN 15 AND 10080),
  enabled BOOLEAN NOT NULL DEFAULT TRUE,
  next_run_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  last_dispatched_at TIMESTAMPTZ,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS collection_target_pins_due_idx
  ON collection_target_pins (next_run_at)
  WHERE enabled = TRUE;
