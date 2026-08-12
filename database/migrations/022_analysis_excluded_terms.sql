-- Per-user term exclusions applied before analysis frequency ranking.

CREATE TABLE IF NOT EXISTS analysis_excluded_terms (
  user_id UUID NOT NULL REFERENCES app_users(id) ON DELETE CASCADE,
  corpus_kind TEXT NOT NULL CHECK (corpus_kind IN ('video', 'comment')),
  term TEXT NOT NULL CHECK (char_length(term) BETWEEN 1 AND 64),
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  PRIMARY KEY (user_id, corpus_kind, term)
);
