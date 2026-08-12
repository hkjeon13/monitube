-- The analysis product ranks persisted bag-of-words aggregates by raw term
-- frequency. No IDF or TF-IDF score is calculated, stored, or queried.

-- Migration 020 originally created a document-frequency-first planning index
-- for an abandoned ranking design. It is not part of pure-frequency reads.
DROP INDEX IF EXISTS nlp_term_stats_rank_idx;

CREATE INDEX IF NOT EXISTS nlp_term_stats_frequency_rank_idx
  ON nlp_term_stats (
    scope_kind,
    scope_id,
    corpus_kind,
    analyzer_version,
    total_term_frequency DESC,
    term
  );

CREATE INDEX IF NOT EXISTS nlp_daily_term_stats_frequency_scope_idx
  ON nlp_daily_term_stats (
    scope_kind,
    scope_id,
    corpus_kind,
    analyzer_version,
    document_date,
    term
  ) INCLUDE (total_term_frequency, document_frequency);

COMMENT ON TABLE nlp_document_terms IS
  'Sparse per-document bag-of-words containing raw term frequency only.';
COMMENT ON TABLE nlp_term_stats IS
  'Corpus aggregate containing raw document and total term frequency only.';
COMMENT ON TABLE nlp_daily_term_stats IS
  'Daily corpus aggregate containing raw document and total term frequency only.';
