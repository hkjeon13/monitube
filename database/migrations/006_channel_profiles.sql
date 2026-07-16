-- Channel profile data powers the Explore channel overview without another
-- client-side YouTube call.  Public statistics remain historical snapshots.

ALTER TABLE channels
  ADD COLUMN IF NOT EXISTS thumbnail_url TEXT;
