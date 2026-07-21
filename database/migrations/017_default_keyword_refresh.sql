-- Keyword collection follows the same default subscription policy as channels.
-- Preserve an explicit prior stop action by creating only missing pin rows.

INSERT INTO collection_target_pins (target_id, enabled, interval_minutes, next_run_at)
SELECT target.id, TRUE, 360, now()
FROM collection_targets target
WHERE target.type = 'keyword'
  AND EXISTS (
    SELECT 1
    FROM collection_subscriptions subscription
    WHERE subscription.target_id = target.id
      AND subscription.enabled = TRUE
  )
ON CONFLICT (target_id) DO NOTHING;
