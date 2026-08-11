-- Keep leased NLP queue claims index-ordered even with a multi-million-row
-- initial backfill.  Separate partial indexes mirror the worker's explicit
-- delete, expired-lease, and pending priority order.

CREATE INDEX IF NOT EXISTS nlp_documents_delete_claim_idx
  ON nlp_documents (updated_at, source_id)
  WHERE state = 'delete_pending';

CREATE INDEX IF NOT EXISTS nlp_documents_expired_claim_idx
  ON nlp_documents (lease_expires_at, updated_at, source_id)
  WHERE state = 'running';

CREATE INDEX IF NOT EXISTS nlp_documents_pending_claim_idx
  ON nlp_documents (updated_at, source_id)
  WHERE state = 'pending';
