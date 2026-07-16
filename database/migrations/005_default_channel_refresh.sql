-- Channel collection is a subscription by default.  Preserve an explicit prior
-- stop action: rows that already have a pin (including enabled = false) are not
-- changed by this backfill.

INSERT INTO collection_target_pins (target_id, enabled, interval_minutes, next_run_at)
SELECT id, TRUE, 360, now()
FROM collection_targets
WHERE type = 'channel'
ON CONFLICT (target_id) DO NOTHING;
