-- Keep planner estimates current for ACL-first and trigram-backed search.
-- These tables have very different sizes: subscriptions stay tiny, while
-- memberships, search documents, and comments grow continuously as workers
-- collect new videos. The defaults can leave a new production table without
-- statistics or wait too long between analyzes.

ALTER TABLE collection_subscriptions SET (
  autovacuum_analyze_scale_factor = 0.0,
  autovacuum_analyze_threshold = 2
);

ALTER TABLE collection_target_videos SET (
  autovacuum_analyze_scale_factor = 0.02,
  autovacuum_analyze_threshold = 100
);

ALTER TABLE videos SET (
  autovacuum_analyze_scale_factor = 0.02,
  autovacuum_analyze_threshold = 100
);

ALTER TABLE video_search_documents SET (
  autovacuum_analyze_scale_factor = 0.02,
  autovacuum_analyze_threshold = 100
);

ALTER TABLE comments SET (
  autovacuum_analyze_scale_factor = 0.02,
  autovacuum_analyze_threshold = 5000
);

ANALYZE collection_subscriptions;
ANALYZE collection_target_videos;
ANALYZE videos;
ANALYZE video_search_documents;
ANALYZE comments;
