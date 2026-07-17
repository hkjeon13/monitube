-- Direct reply detail lookups use the stable YouTube parent-comment ID rather
-- than the optional internal parent_id.  This preserves replies that arrived
-- before their parent while keeping the detail drawer fast on the shared
-- comments corpus.
CREATE INDEX IF NOT EXISTS comments_youtube_parent_published_idx
  ON comments (youtube_parent_comment_id, published_at ASC, source_fetched_at ASC, youtube_comment_id ASC)
  WHERE youtube_parent_comment_id IS NOT NULL;
