-- Keyset pagination for top-level comment threads ordered by stored likes.
CREATE INDEX IF NOT EXISTS comments_video_thread_recommended_idx
  ON comments (
    video_id,
    COALESCE(like_count, 0) DESC,
    COALESCE(published_at, source_fetched_at, 'epoch'::timestamptz) DESC,
    youtube_comment_id DESC
  )
  WHERE youtube_parent_comment_id IS NULL;
