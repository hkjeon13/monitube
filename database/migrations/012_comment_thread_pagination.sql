-- Stable keyset pagination for top-level video comment threads.
CREATE INDEX IF NOT EXISTS comments_video_thread_published_idx
  ON comments (
    video_id,
    COALESCE(published_at, source_fetched_at, 'epoch'::timestamptz) DESC,
    youtube_comment_id DESC
  )
  WHERE youtube_parent_comment_id IS NULL;
