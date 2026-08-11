-- Durable MeCab/NLTK document index, transcript snippets, and incremental TF-IDF.
-- Source rows only enqueue work.  The worker applies old/new term deltas in one
-- transaction, so collection writes never wait for morphological analysis.

ALTER TABLE video_transcript_segments
  ADD COLUMN IF NOT EXISTS search_terms TEXT[] NOT NULL DEFAULT '{}',
  ADD COLUMN IF NOT EXISTS analyzer_version TEXT;

CREATE INDEX IF NOT EXISTS video_transcript_segments_search_terms_idx
  ON video_transcript_segments USING gin (search_terms);

CREATE TABLE IF NOT EXISTS nlp_documents (
  source_kind TEXT NOT NULL CHECK (source_kind IN ('transcript', 'comment')),
  source_id UUID NOT NULL,
  video_id UUID NOT NULL REFERENCES videos(id) ON DELETE CASCADE,
  source_hash TEXT NOT NULL,
  source_date DATE,
  indexed_source_date DATE,
  comment_type TEXT CHECK (comment_type IN ('top_level', 'reply')),
  indexed_comment_type TEXT CHECK (indexed_comment_type IN ('top_level', 'reply')),
  analyzer_version TEXT,
  state TEXT NOT NULL DEFAULT 'pending'
    CHECK (state IN ('pending', 'running', 'ready', 'failed', 'delete_pending')),
  token_count INTEGER NOT NULL DEFAULT 0 CHECK (token_count >= 0),
  retry_count INTEGER NOT NULL DEFAULT 0 CHECK (retry_count >= 0),
  error_code TEXT,
  lease_owner TEXT,
  lease_expires_at TIMESTAMPTZ,
  indexed_at TIMESTAMPTZ,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  PRIMARY KEY (source_kind, source_id)
);

CREATE INDEX IF NOT EXISTS nlp_documents_queue_idx
  ON nlp_documents (state, lease_expires_at, updated_at)
  WHERE state IN ('pending', 'running', 'delete_pending');
CREATE INDEX IF NOT EXISTS nlp_documents_video_idx
  ON nlp_documents (video_id, source_kind, state);

CREATE TABLE IF NOT EXISTS nlp_document_terms (
  source_kind TEXT NOT NULL,
  source_id UUID NOT NULL,
  term TEXT NOT NULL,
  term_frequency INTEGER NOT NULL CHECK (term_frequency > 0),
  PRIMARY KEY (source_kind, source_id, term),
  FOREIGN KEY (source_kind, source_id)
    REFERENCES nlp_documents(source_kind, source_id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS nlp_document_terms_term_idx
  ON nlp_document_terms (source_kind, term, source_id);

CREATE TABLE IF NOT EXISTS nlp_scope_documents (
  scope_kind TEXT NOT NULL CHECK (scope_kind IN ('target', 'owner')),
  scope_id UUID NOT NULL,
  source_kind TEXT NOT NULL,
  source_id UUID NOT NULL,
  document_date DATE,
  membership_ref_count INTEGER NOT NULL DEFAULT 1
    CHECK (membership_ref_count > 0),
  PRIMARY KEY (scope_kind, scope_id, source_kind, source_id),
  FOREIGN KEY (source_kind, source_id)
    REFERENCES nlp_documents(source_kind, source_id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS nlp_scope_documents_date_idx
  ON nlp_scope_documents (
    scope_kind, scope_id, source_kind, document_date, source_id
  );

CREATE TABLE IF NOT EXISTS nlp_corpus_stats (
  scope_kind TEXT NOT NULL CHECK (scope_kind IN ('target', 'owner')),
  scope_id UUID NOT NULL,
  corpus_kind TEXT NOT NULL
    CHECK (corpus_kind IN ('video', 'comment', 'comment_top_level', 'comment_reply')),
  analyzer_version TEXT NOT NULL,
  document_count BIGINT NOT NULL DEFAULT 0 CHECK (document_count >= 0),
  total_token_count BIGINT NOT NULL DEFAULT 0 CHECK (total_token_count >= 0),
  stats_version BIGINT NOT NULL DEFAULT 0 CHECK (stats_version >= 0),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  PRIMARY KEY (scope_kind, scope_id, corpus_kind, analyzer_version)
);

CREATE TABLE IF NOT EXISTS nlp_term_stats (
  scope_kind TEXT NOT NULL,
  scope_id UUID NOT NULL,
  corpus_kind TEXT NOT NULL,
  analyzer_version TEXT NOT NULL,
  term TEXT NOT NULL,
  document_frequency BIGINT NOT NULL DEFAULT 0 CHECK (document_frequency >= 0),
  total_term_frequency BIGINT NOT NULL DEFAULT 0 CHECK (total_term_frequency >= 0),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  PRIMARY KEY (scope_kind, scope_id, corpus_kind, analyzer_version, term),
  FOREIGN KEY (scope_kind, scope_id, corpus_kind, analyzer_version)
    REFERENCES nlp_corpus_stats(
      scope_kind, scope_id, corpus_kind, analyzer_version
    ) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS nlp_term_stats_rank_idx
  ON nlp_term_stats (
    scope_kind, scope_id, corpus_kind, analyzer_version,
    document_frequency DESC, total_term_frequency DESC
  );

CREATE TABLE IF NOT EXISTS nlp_daily_corpus_stats (
  scope_kind TEXT NOT NULL,
  scope_id UUID NOT NULL,
  corpus_kind TEXT NOT NULL,
  analyzer_version TEXT NOT NULL,
  document_date DATE NOT NULL,
  document_count BIGINT NOT NULL DEFAULT 0 CHECK (document_count >= 0),
  total_token_count BIGINT NOT NULL DEFAULT 0 CHECK (total_token_count >= 0),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  PRIMARY KEY (
    scope_kind, scope_id, corpus_kind, analyzer_version, document_date
  )
);

CREATE TABLE IF NOT EXISTS nlp_daily_term_stats (
  scope_kind TEXT NOT NULL,
  scope_id UUID NOT NULL,
  corpus_kind TEXT NOT NULL,
  analyzer_version TEXT NOT NULL,
  document_date DATE NOT NULL,
  term TEXT NOT NULL,
  document_frequency BIGINT NOT NULL DEFAULT 0 CHECK (document_frequency >= 0),
  total_term_frequency BIGINT NOT NULL DEFAULT 0 CHECK (total_term_frequency >= 0),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  PRIMARY KEY (
    scope_kind, scope_id, corpus_kind, analyzer_version, document_date, term
  ),
  FOREIGN KEY (
    scope_kind, scope_id, corpus_kind, analyzer_version, document_date
  ) REFERENCES nlp_daily_corpus_stats(
    scope_kind, scope_id, corpus_kind, analyzer_version, document_date
  ) ON DELETE CASCADE
);

CREATE OR REPLACE FUNCTION enqueue_transcript_nlp_document()
RETURNS TRIGGER LANGUAGE plpgsql AS $$
DECLARE
  effective_hash TEXT;
BEGIN
  IF TG_OP = 'DELETE' THEN
    UPDATE nlp_documents
       SET state = 'delete_pending', lease_owner = NULL,
           lease_expires_at = NULL, updated_at = now()
     WHERE source_kind = 'transcript'
       AND source_id = OLD.id;
    RETURN OLD;
  END IF;
  IF NEW.state <> 'available' OR NULLIF(btrim(NEW.full_text), '') IS NULL THEN
    UPDATE nlp_documents
       SET state = 'delete_pending', lease_owner = NULL,
           lease_expires_at = NULL, updated_at = now()
     WHERE source_kind = 'transcript' AND source_id = NEW.id;
    RETURN NEW;
  END IF;

  effective_hash := COALESCE(
    NULLIF(NEW.content_hash, ''),
    encode(digest(NEW.full_text, 'sha256'), 'hex')
  );
  INSERT INTO nlp_documents (
    source_kind, source_id, video_id, source_hash, source_date, state
  ) VALUES (
    'transcript', NEW.id, NEW.video_id, effective_hash,
    COALESCE((SELECT published_at::date FROM videos WHERE id = NEW.video_id), NEW.fetched_at::date),
    'pending'
  )
  ON CONFLICT (source_kind, source_id) DO UPDATE
  SET video_id = EXCLUDED.video_id,
      source_hash = EXCLUDED.source_hash,
      source_date = EXCLUDED.source_date,
      state = CASE
        WHEN nlp_documents.source_hash IS DISTINCT FROM EXCLUDED.source_hash
          OR nlp_documents.source_date IS DISTINCT FROM EXCLUDED.source_date
          OR nlp_documents.state = 'delete_pending'
          THEN 'pending'
        ELSE nlp_documents.state
      END,
      lease_owner = CASE
        WHEN nlp_documents.source_hash IS DISTINCT FROM EXCLUDED.source_hash
          OR nlp_documents.source_date IS DISTINCT FROM EXCLUDED.source_date
          OR nlp_documents.state = 'delete_pending' THEN NULL
        ELSE nlp_documents.lease_owner
      END,
      lease_expires_at = CASE
        WHEN nlp_documents.source_hash IS DISTINCT FROM EXCLUDED.source_hash
          OR nlp_documents.source_date IS DISTINCT FROM EXCLUDED.source_date
          OR nlp_documents.state = 'delete_pending' THEN NULL
        ELSE nlp_documents.lease_expires_at
      END,
      updated_at = now();
  RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS video_transcripts_nlp_enqueue ON video_transcripts;
CREATE TRIGGER video_transcripts_nlp_enqueue
AFTER INSERT OR UPDATE OF state, full_text, content_hash OR DELETE
ON video_transcripts FOR EACH ROW EXECUTE FUNCTION enqueue_transcript_nlp_document();

CREATE OR REPLACE FUNCTION enqueue_comment_nlp_document()
RETURNS TRIGGER LANGUAGE plpgsql AS $$
BEGIN
  IF TG_OP = 'DELETE' THEN
    UPDATE nlp_documents
       SET state = 'delete_pending', lease_owner = NULL,
           lease_expires_at = NULL, updated_at = now()
     WHERE source_kind = 'comment' AND source_id = OLD.id;
    RETURN OLD;
  END IF;
  IF NEW.deleted_at IS NOT NULL OR NULLIF(btrim(NEW.text_display), '') IS NULL THEN
    UPDATE nlp_documents
       SET state = 'delete_pending', lease_owner = NULL,
           lease_expires_at = NULL, updated_at = now()
     WHERE source_kind = 'comment' AND source_id = NEW.id;
    RETURN NEW;
  END IF;

  INSERT INTO nlp_documents (
    source_kind, source_id, video_id, source_hash, source_date,
    comment_type, state
  ) VALUES (
    'comment', NEW.id, NEW.video_id,
    encode(digest(NEW.text_display, 'sha256'), 'hex'),
    COALESCE(NEW.published_at::date, NEW.source_fetched_at::date),
    CASE WHEN NEW.youtube_parent_comment_id IS NULL THEN 'top_level' ELSE 'reply' END,
    'pending'
  )
  ON CONFLICT (source_kind, source_id) DO UPDATE
  SET video_id = EXCLUDED.video_id,
      source_hash = EXCLUDED.source_hash,
      source_date = EXCLUDED.source_date,
      comment_type = EXCLUDED.comment_type,
      state = CASE
        WHEN nlp_documents.source_hash IS DISTINCT FROM EXCLUDED.source_hash
          OR nlp_documents.source_date IS DISTINCT FROM EXCLUDED.source_date
          OR nlp_documents.comment_type IS DISTINCT FROM EXCLUDED.comment_type
          OR nlp_documents.state = 'delete_pending'
          THEN 'pending'
        ELSE nlp_documents.state
      END,
      lease_owner = CASE
        WHEN nlp_documents.source_hash IS DISTINCT FROM EXCLUDED.source_hash
          OR nlp_documents.source_date IS DISTINCT FROM EXCLUDED.source_date
          OR nlp_documents.comment_type IS DISTINCT FROM EXCLUDED.comment_type
          OR nlp_documents.state = 'delete_pending' THEN NULL
        ELSE nlp_documents.lease_owner
      END,
      lease_expires_at = CASE
        WHEN nlp_documents.source_hash IS DISTINCT FROM EXCLUDED.source_hash
          OR nlp_documents.source_date IS DISTINCT FROM EXCLUDED.source_date
          OR nlp_documents.comment_type IS DISTINCT FROM EXCLUDED.comment_type
          OR nlp_documents.state = 'delete_pending' THEN NULL
        ELSE nlp_documents.lease_expires_at
      END,
      updated_at = now();
  RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS comments_nlp_enqueue ON comments;
CREATE TRIGGER comments_nlp_enqueue
AFTER INSERT OR UPDATE OF text_display, youtube_parent_comment_id, published_at,
  source_fetched_at, deleted_at OR DELETE
ON comments FOR EACH ROW EXECUTE FUNCTION enqueue_comment_nlp_document();

-- Initial backfill is queue-only.  The worker processes it in small leased batches.
INSERT INTO nlp_documents (
  source_kind, source_id, video_id, source_hash, source_date, state
)
SELECT 'transcript', transcript.id, transcript.video_id,
       COALESCE(NULLIF(transcript.content_hash, ''), encode(digest(transcript.full_text, 'sha256'), 'hex')),
       COALESCE(video.published_at::date, transcript.fetched_at::date), 'pending'
FROM video_transcripts transcript
JOIN videos video ON video.id = transcript.video_id
WHERE transcript.state = 'available'
  AND NULLIF(btrim(transcript.full_text), '') IS NOT NULL
ON CONFLICT (source_kind, source_id) DO NOTHING;

-- Membership changes do not require a corpus scan. They only requeue documents
-- for the affected video, and the worker reconciles old/new scope memberships.
CREATE OR REPLACE FUNCTION enqueue_video_nlp_membership_change()
RETURNS TRIGGER LANGUAGE plpgsql AS $$
BEGIN
  IF TG_OP = 'DELETE' THEN
    UPDATE nlp_documents
       SET state = 'pending', lease_owner = NULL, lease_expires_at = NULL,
           updated_at = now()
     WHERE video_id = OLD.video_id AND state <> 'delete_pending';
    RETURN OLD;
  END IF;
  UPDATE nlp_documents
     SET state = 'pending', lease_owner = NULL, lease_expires_at = NULL,
         updated_at = now()
   WHERE video_id = NEW.video_id
     AND state <> 'delete_pending';
  RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS collection_target_videos_nlp_membership
  ON collection_target_videos;
CREATE TRIGGER collection_target_videos_nlp_membership
AFTER INSERT OR DELETE ON collection_target_videos
FOR EACH ROW EXECUTE FUNCTION enqueue_video_nlp_membership_change();

CREATE OR REPLACE FUNCTION enqueue_subscription_nlp_membership_change()
RETURNS TRIGGER LANGUAGE plpgsql AS $$
BEGIN
  IF TG_OP <> 'DELETE' THEN
    UPDATE nlp_documents document
       SET state = 'pending', lease_owner = NULL, lease_expires_at = NULL,
           updated_at = now()
     WHERE document.video_id IN (
       SELECT membership.video_id
       FROM collection_target_videos membership
       WHERE membership.target_id = NEW.target_id
     )
       AND document.state <> 'delete_pending';
  END IF;
  IF TG_OP = 'DELETE'
     OR (TG_OP = 'UPDATE' AND OLD.target_id IS DISTINCT FROM NEW.target_id)
     OR (TG_OP = 'UPDATE' AND OLD.user_id IS DISTINCT FROM NEW.user_id) THEN
    UPDATE nlp_documents document
       SET state = 'pending', lease_owner = NULL, lease_expires_at = NULL,
           updated_at = now()
     WHERE document.video_id IN (
       SELECT membership.video_id
       FROM collection_target_videos membership
       WHERE membership.target_id = OLD.target_id
     )
       AND document.state <> 'delete_pending';
  END IF;
  IF TG_OP = 'DELETE' THEN
    RETURN OLD;
  END IF;
  RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS collection_subscriptions_nlp_membership
  ON collection_subscriptions;
CREATE TRIGGER collection_subscriptions_nlp_membership
AFTER INSERT OR UPDATE OF target_id, user_id, enabled OR DELETE
ON collection_subscriptions
FOR EACH ROW EXECUTE FUNCTION enqueue_subscription_nlp_membership_change();

INSERT INTO nlp_documents (
  source_kind, source_id, video_id, source_hash, source_date, comment_type, state
)
SELECT 'comment', comment.id, comment.video_id,
       encode(digest(comment.text_display, 'sha256'), 'hex'),
       COALESCE(comment.published_at::date, comment.source_fetched_at::date),
       CASE WHEN comment.youtube_parent_comment_id IS NULL THEN 'top_level' ELSE 'reply' END,
       'pending'
FROM comments comment
WHERE comment.deleted_at IS NULL
  AND NULLIF(btrim(comment.text_display), '') IS NOT NULL
ON CONFLICT (source_kind, source_id) DO NOTHING;
