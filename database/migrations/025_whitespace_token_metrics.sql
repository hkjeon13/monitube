-- Persist lightweight, tokenizer-independent corpus size metrics.
-- Existing rows intentionally remain NULL so the bounded maintenance command
-- can backfill them without holding a long migration transaction or table lock.

ALTER TABLE comments
  ADD COLUMN IF NOT EXISTS whitespace_token_count INTEGER;

ALTER TABLE video_transcripts
  ADD COLUMN IF NOT EXISTS whitespace_token_count INTEGER;

ALTER TABLE comments
  DROP CONSTRAINT IF EXISTS comments_whitespace_token_count_nonnegative;
ALTER TABLE comments
  ADD CONSTRAINT comments_whitespace_token_count_nonnegative
  CHECK (whitespace_token_count IS NULL OR whitespace_token_count >= 0)
  NOT VALID;

ALTER TABLE video_transcripts
  DROP CONSTRAINT IF EXISTS video_transcripts_whitespace_token_count_nonnegative;
ALTER TABLE video_transcripts
  ADD CONSTRAINT video_transcripts_whitespace_token_count_nonnegative
  CHECK (whitespace_token_count IS NULL OR whitespace_token_count >= 0)
  NOT VALID;

CREATE OR REPLACE FUNCTION monitube_whitespace_token_count(input_text TEXT)
RETURNS INTEGER
LANGUAGE SQL
IMMUTABLE
PARALLEL SAFE
AS $$
  SELECT CASE
    WHEN input_text IS NULL OR btrim(input_text) = '' THEN 0
    ELSE cardinality(regexp_split_to_array(btrim(input_text), '[[:space:]]+'))
  END
$$;

CREATE OR REPLACE FUNCTION set_comment_whitespace_token_count()
RETURNS TRIGGER
LANGUAGE plpgsql
AS $$
BEGIN
  NEW.whitespace_token_count := monitube_whitespace_token_count(NEW.text_display);
  RETURN NEW;
END
$$;

CREATE OR REPLACE FUNCTION set_transcript_whitespace_token_count()
RETURNS TRIGGER
LANGUAGE plpgsql
AS $$
BEGIN
  NEW.whitespace_token_count := monitube_whitespace_token_count(NEW.full_text);
  RETURN NEW;
END
$$;

DROP TRIGGER IF EXISTS comments_whitespace_token_count_write ON comments;
CREATE TRIGGER comments_whitespace_token_count_write
BEFORE INSERT OR UPDATE OF text_display
ON comments
FOR EACH ROW
EXECUTE FUNCTION set_comment_whitespace_token_count();

DROP TRIGGER IF EXISTS video_transcripts_whitespace_token_count_write
ON video_transcripts;
CREATE TRIGGER video_transcripts_whitespace_token_count_write
BEFORE INSERT OR UPDATE OF full_text
ON video_transcripts
FOR EACH ROW
EXECUTE FUNCTION set_transcript_whitespace_token_count();
