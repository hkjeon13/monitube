-- Existing channel sources predate the all-content defaults.  Promote them to
-- the same unlimited public-video/comment collection policy as new channels.
-- Numeric fields are intentionally removed; all-content flags take precedence
-- in the worker and avoid displaying a misleading legacy cap in the UI.

UPDATE collection_sources
SET config = (config - 'maxVideos' - 'maxCommentPagesPerVideo') || jsonb_build_object(
      'includeComments', TRUE,
      'collectAllVideos', TRUE,
      'collectAllComments', TRUE
    ),
    updated_at = now()
WHERE type = 'channel';

-- Target-level coverage is reset so legacy partial runs are not treated as
-- complete. The next pinned run will continue from the full playlist and, when
-- a video-count deficit exists, process historical uploads oldest-first.
UPDATE collection_targets
SET config = (config - 'maxVideos' - 'maxCommentPagesPerVideo') || jsonb_build_object(
      'includeComments', TRUE,
      'collectAllVideos', TRUE,
      'collectAllComments', TRUE
    ),
    coverage = (coverage - 'maxVideos' - 'maxCommentPagesPerVideo') || jsonb_build_object(
      'complete', FALSE,
      'includeComments', TRUE,
      'collectAllVideos', TRUE,
      'collectAllComments', TRUE
    ),
    updated_at = now()
WHERE type = 'channel';

-- Do not override an explicit stop action. Enabled channel pins are made due
-- now, so the worker begins the widened collection without waiting six hours.
UPDATE collection_target_pins pin
SET next_run_at = now(), updated_at = now()
FROM collection_targets target
WHERE pin.target_id = target.id
  AND target.type = 'channel'
  AND pin.enabled = TRUE;
