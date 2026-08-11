"""Leased MeCab/NLTK indexing and exact incremental corpus statistics."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import date
from typing import Any


class PostgresNlpMixin:
    """Keep source collection independent from bounded NLP index work."""

    def claim_next_nlp_document(
        self,
        *,
        worker_id: str,
        lease_seconds: int,
    ) -> dict[str, Any] | None:
        with self._connection() as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                WITH candidate AS (
                  SELECT source_kind, source_id, state AS requested_state
                  FROM nlp_documents
                  WHERE state IN ('pending', 'delete_pending')
                     OR (state = 'running' AND lease_expires_at < now())
                  ORDER BY CASE state WHEN 'delete_pending' THEN 0 ELSE 1 END,
                           updated_at, source_id
                  FOR UPDATE SKIP LOCKED
                  LIMIT 1
                )
                UPDATE nlp_documents document
                   SET state = 'running', lease_owner = %s,
                       lease_expires_at = now() + (%s * interval '1 second'),
                       updated_at = now()
                  FROM candidate
                 WHERE document.source_kind = candidate.source_kind
                   AND document.source_id = candidate.source_id
                RETURNING document.*, candidate.requested_state
                """,
                (worker_id, lease_seconds),
            )
            row = cursor.fetchone()
            if not row:
                return None
            document = dict(row)
            document["source_id"] = str(document["source_id"])
            document["video_id"] = str(document["video_id"])
            if document["requested_state"] == "delete_pending":
                document["action"] = "delete"
                document["text"] = ""
                document["segments"] = []
                return document

            if document["source_kind"] == "transcript":
                cursor.execute(
                    """
                    SELECT full_text
                    FROM video_transcripts
                    WHERE id = %s AND state = 'available'
                    """,
                    (document["source_id"],),
                )
                source = cursor.fetchone()
                if not source or not source.get("full_text"):
                    document["action"] = "delete"
                    document["text"] = ""
                    document["segments"] = []
                    return document
                cursor.execute(
                    """
                    SELECT sequence, text
                    FROM video_transcript_segments
                    WHERE transcript_id = %s
                    ORDER BY sequence
                    """,
                    (document["source_id"],),
                )
                document["segments"] = [dict(item) for item in cursor.fetchall()]
                document["text"] = source["full_text"]
            else:
                cursor.execute(
                    """
                    SELECT text_display
                    FROM comments
                    WHERE id = %s AND deleted_at IS NULL
                    """,
                    (document["source_id"],),
                )
                source = cursor.fetchone()
                if not source or not source.get("text_display"):
                    document["action"] = "delete"
                    document["text"] = ""
                    document["segments"] = []
                    return document
                document["segments"] = []
                document["text"] = source["text_display"]
            document["action"] = "index"
            return document

    @staticmethod
    def _corpus_kinds(source_kind: str, comment_type: str | None) -> tuple[str, ...]:
        if source_kind == "transcript":
            return ("video",)
        detail = "comment_reply" if comment_type == "reply" else "comment_top_level"
        return ("comment", detail)

    @staticmethod
    def _current_scope_memberships(
        cursor: Any, *, video_id: str
    ) -> list[dict[str, Any]]:
        cursor.execute(
            """
            SELECT 'target'::text AS scope_kind,
                   membership.target_id AS scope_id,
                   1::integer AS membership_ref_count
            FROM collection_target_videos membership
            WHERE membership.video_id = %s
            UNION ALL
            SELECT 'owner'::text AS scope_kind,
                   subscription.user_id AS scope_id,
                   count(DISTINCT membership.target_id)::integer
                     AS membership_ref_count
            FROM collection_target_videos membership
            JOIN collection_subscriptions subscription
              ON subscription.target_id = membership.target_id
             AND subscription.enabled = TRUE
            WHERE membership.video_id = %s
            GROUP BY subscription.user_id
            ORDER BY scope_kind, scope_id
            """,
            (video_id, video_id),
        )
        return [dict(row) for row in cursor.fetchall()]

    @staticmethod
    def _apply_corpus_delta(
        cursor: Any,
        *,
        scope_kind: str,
        scope_id: str,
        corpus_kind: str,
        analyzer_version: str,
        document_date: date | None,
        terms: Mapping[str, int],
        token_count: int,
        direction: int,
    ) -> None:
        if direction > 0:
            cursor.execute(
                """
                INSERT INTO nlp_corpus_stats (
                  scope_kind, scope_id, corpus_kind, analyzer_version,
                  document_count, total_token_count, stats_version, updated_at
                ) VALUES (%s, %s, %s, %s, 1, %s, 1, now())
                ON CONFLICT (
                  scope_kind, scope_id, corpus_kind, analyzer_version
                ) DO UPDATE
                SET document_count = nlp_corpus_stats.document_count + 1,
                    total_token_count = nlp_corpus_stats.total_token_count + EXCLUDED.total_token_count,
                    stats_version = nlp_corpus_stats.stats_version + 1,
                    updated_at = now()
                """,
                (
                    scope_kind,
                    scope_id,
                    corpus_kind,
                    analyzer_version,
                    token_count,
                ),
            )
            for term, frequency in sorted(terms.items()):
                cursor.execute(
                    """
                    INSERT INTO nlp_term_stats (
                      scope_kind, scope_id, corpus_kind, analyzer_version,
                      term, document_frequency, total_term_frequency, updated_at
                    ) VALUES (%s, %s, %s, %s, %s, 1, %s, now())
                    ON CONFLICT (
                      scope_kind, scope_id, corpus_kind, analyzer_version, term
                    ) DO UPDATE
                    SET document_frequency = nlp_term_stats.document_frequency + 1,
                        total_term_frequency = nlp_term_stats.total_term_frequency + EXCLUDED.total_term_frequency,
                        updated_at = now()
                    """,
                    (
                        scope_kind,
                        scope_id,
                        corpus_kind,
                        analyzer_version,
                        term,
                        frequency,
                    ),
                )
        else:
            cursor.execute(
                """
                UPDATE nlp_corpus_stats
                   SET document_count = document_count - 1,
                       total_token_count = total_token_count - %s,
                       stats_version = stats_version + 1,
                       updated_at = now()
                 WHERE scope_kind = %s AND scope_id = %s
                   AND corpus_kind = %s AND analyzer_version = %s
                """,
                (token_count, scope_kind, scope_id, corpus_kind, analyzer_version),
            )
            for term, frequency in sorted(terms.items()):
                cursor.execute(
                    """
                    UPDATE nlp_term_stats
                       SET document_frequency = document_frequency - 1,
                           total_term_frequency = total_term_frequency - %s,
                           updated_at = now()
                     WHERE scope_kind = %s AND scope_id = %s
                       AND corpus_kind = %s AND analyzer_version = %s
                       AND term = %s
                    """,
                    (
                        frequency,
                        scope_kind,
                        scope_id,
                        corpus_kind,
                        analyzer_version,
                        term,
                    ),
                )
            cursor.execute(
                """DELETE FROM nlp_term_stats
                   WHERE scope_kind = %s AND scope_id = %s
                     AND corpus_kind = %s AND analyzer_version = %s
                     AND document_frequency = 0
                     AND total_term_frequency = 0""",
                (scope_kind, scope_id, corpus_kind, analyzer_version),
            )

        if document_date is None:
            return
        if direction > 0:
            cursor.execute(
                """
                INSERT INTO nlp_daily_corpus_stats (
                  scope_kind, scope_id, corpus_kind, analyzer_version,
                  document_date, document_count, total_token_count, updated_at
                ) VALUES (%s, %s, %s, %s, %s, 1, %s, now())
                ON CONFLICT (
                  scope_kind, scope_id, corpus_kind,
                  analyzer_version, document_date
                ) DO UPDATE
                SET document_count = nlp_daily_corpus_stats.document_count + 1,
                    total_token_count = nlp_daily_corpus_stats.total_token_count + EXCLUDED.total_token_count,
                    updated_at = now()
                """,
                (
                    scope_kind,
                    scope_id,
                    corpus_kind,
                    analyzer_version,
                    document_date,
                    token_count,
                ),
            )
            for term, frequency in sorted(terms.items()):
                cursor.execute(
                    """
                    INSERT INTO nlp_daily_term_stats (
                      scope_kind, scope_id, corpus_kind, analyzer_version,
                      document_date, term, document_frequency,
                      total_term_frequency, updated_at
                    ) VALUES (%s, %s, %s, %s, %s, %s, 1, %s, now())
                    ON CONFLICT (
                      scope_kind, scope_id, corpus_kind, analyzer_version,
                      document_date, term
                    ) DO UPDATE
                    SET document_frequency = nlp_daily_term_stats.document_frequency + 1,
                        total_term_frequency = nlp_daily_term_stats.total_term_frequency + EXCLUDED.total_term_frequency,
                        updated_at = now()
                    """,
                    (
                        scope_kind,
                        scope_id,
                        corpus_kind,
                        analyzer_version,
                        document_date,
                        term,
                        frequency,
                    ),
                )
        else:
            for term, frequency in sorted(terms.items()):
                cursor.execute(
                    """
                    UPDATE nlp_daily_term_stats
                       SET document_frequency = document_frequency - 1,
                           total_term_frequency = total_term_frequency - %s,
                           updated_at = now()
                     WHERE scope_kind = %s AND scope_id = %s
                       AND corpus_kind = %s AND analyzer_version = %s
                       AND document_date = %s AND term = %s
                    """,
                    (
                        frequency,
                        scope_kind,
                        scope_id,
                        corpus_kind,
                        analyzer_version,
                        document_date,
                        term,
                    ),
                )
            cursor.execute(
                """
                DELETE FROM nlp_daily_term_stats
                 WHERE scope_kind = %s AND scope_id = %s
                   AND corpus_kind = %s AND analyzer_version = %s
                   AND document_date = %s
                   AND document_frequency = 0 AND total_term_frequency = 0
                """,
                (
                    scope_kind,
                    scope_id,
                    corpus_kind,
                    analyzer_version,
                    document_date,
                ),
            )
            cursor.execute(
                """
                UPDATE nlp_daily_corpus_stats
                   SET document_count = document_count - 1,
                       total_token_count = total_token_count - %s,
                       updated_at = now()
                 WHERE scope_kind = %s AND scope_id = %s
                   AND corpus_kind = %s AND analyzer_version = %s
                   AND document_date = %s
                """,
                (
                    token_count,
                    scope_kind,
                    scope_id,
                    corpus_kind,
                    analyzer_version,
                    document_date,
                ),
            )
            cursor.execute(
                """
                DELETE FROM nlp_daily_corpus_stats
                 WHERE scope_kind = %s AND scope_id = %s
                   AND corpus_kind = %s AND analyzer_version = %s
                   AND document_date = %s
                   AND document_count = 0 AND total_token_count = 0
                """,
                (
                    scope_kind,
                    scope_id,
                    corpus_kind,
                    analyzer_version,
                    document_date,
                ),
            )
            cursor.execute(
                """
                DELETE FROM nlp_corpus_stats
                WHERE scope_kind = %s AND scope_id = %s
                  AND corpus_kind = %s AND analyzer_version = %s
                  AND document_count = 0 AND total_token_count = 0
                """,
                (scope_kind, scope_id, corpus_kind, analyzer_version),
            )

    def complete_nlp_document(
        self,
        *,
        source_kind: str,
        source_id: str,
        source_hash: str,
        analyzer_version: str,
        terms: Mapping[str, int],
        segment_terms: Mapping[int, list[str]] | None = None,
        delete: bool = False,
    ) -> bool:
        clean_terms = {
            str(term): int(frequency)
            for term, frequency in terms.items()
            if str(term) and int(frequency) > 0
        }
        token_count = sum(clean_terms.values())
        with self._connection() as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT * FROM nlp_documents
                WHERE source_kind = %s AND source_id = %s
                FOR UPDATE
                """,
                (source_kind, source_id),
            )
            row = cursor.fetchone()
            if not row:
                return False
            if not delete and row["source_hash"] != source_hash:
                cursor.execute(
                    """
                    UPDATE nlp_documents
                    SET state = 'pending', lease_owner = NULL,
                        lease_expires_at = NULL, updated_at = now()
                    WHERE source_kind = %s AND source_id = %s
                    """,
                    (source_kind, source_id),
                )
                return False

            cursor.execute(
                """
                SELECT term, term_frequency
                FROM nlp_document_terms
                WHERE source_kind = %s AND source_id = %s
                ORDER BY term
                """,
                (source_kind, source_id),
            )
            old_terms = {
                item["term"]: int(item["term_frequency"]) for item in cursor.fetchall()
            }
            cursor.execute(
                """
                SELECT scope_kind, scope_id, document_date,
                       membership_ref_count
                FROM nlp_scope_documents
                WHERE source_kind = %s AND source_id = %s
                ORDER BY scope_kind, scope_id
                FOR UPDATE
                """,
                (source_kind, source_id),
            )
            old_scopes = [dict(item) for item in cursor.fetchall()]
            old_token_count = int(row["token_count"] or 0)
            old_analyzer_version = row.get("analyzer_version")
            if row.get("indexed_at") is not None and old_analyzer_version:
                for scope in old_scopes:
                    for corpus_kind in self._corpus_kinds(
                        source_kind, row.get("indexed_comment_type")
                    ):
                        self._apply_corpus_delta(
                            cursor,
                            scope_kind=scope["scope_kind"],
                            scope_id=scope["scope_id"],
                            corpus_kind=corpus_kind,
                            analyzer_version=old_analyzer_version,
                            document_date=scope.get("document_date"),
                            terms=old_terms,
                            token_count=old_token_count,
                            direction=-1,
                        )

            cursor.execute(
                """DELETE FROM nlp_scope_documents
                   WHERE source_kind = %s AND source_id = %s""",
                (source_kind, source_id),
            )

            cursor.execute(
                """DELETE FROM nlp_document_terms
                   WHERE source_kind = %s AND source_id = %s""",
                (source_kind, source_id),
            )
            if delete:
                cursor.execute(
                    """DELETE FROM nlp_documents
                       WHERE source_kind = %s AND source_id = %s""",
                    (source_kind, source_id),
                )
                return True

            for term, frequency in sorted(clean_terms.items()):
                cursor.execute(
                    """
                    INSERT INTO nlp_document_terms (
                      source_kind, source_id, term, term_frequency
                    ) VALUES (%s, %s, %s, %s)
                    """,
                    (source_kind, source_id, term, frequency),
                )
            new_scopes = self._current_scope_memberships(
                cursor, video_id=str(row["video_id"])
            )
            for scope in new_scopes:
                cursor.execute(
                    """
                    INSERT INTO nlp_scope_documents (
                      scope_kind, scope_id, source_kind, source_id,
                      document_date, membership_ref_count
                    ) VALUES (%s, %s, %s, %s, %s, %s)
                    """,
                    (
                        scope["scope_kind"],
                        scope["scope_id"],
                        source_kind,
                        source_id,
                        row.get("source_date"),
                        scope["membership_ref_count"],
                    ),
                )
                for corpus_kind in self._corpus_kinds(
                    source_kind, row.get("comment_type")
                ):
                    self._apply_corpus_delta(
                        cursor,
                        scope_kind=scope["scope_kind"],
                        scope_id=scope["scope_id"],
                        corpus_kind=corpus_kind,
                        analyzer_version=analyzer_version,
                        document_date=row.get("source_date"),
                        terms=clean_terms,
                        token_count=token_count,
                        direction=1,
                    )

            if source_kind == "transcript":
                cursor.execute(
                    """
                    UPDATE video_transcript_segments
                    SET search_terms = '{}', analyzer_version = %s
                    WHERE transcript_id = %s
                    """,
                    (analyzer_version, source_id),
                )
                for sequence, values in (segment_terms or {}).items():
                    cursor.execute(
                        """
                        UPDATE video_transcript_segments
                        SET search_terms = %s, analyzer_version = %s
                        WHERE transcript_id = %s AND sequence = %s
                        """,
                        (sorted(set(values)), analyzer_version, source_id, sequence),
                    )

            cursor.execute(
                """
                UPDATE nlp_documents
                SET analyzer_version = %s, state = 'ready', token_count = %s,
                    indexed_source_date = source_date,
                    indexed_comment_type = comment_type,
                    indexed_at = now(), retry_count = 0, error_code = NULL,
                    lease_owner = NULL, lease_expires_at = NULL, updated_at = now()
                WHERE source_kind = %s AND source_id = %s
                """,
                (analyzer_version, token_count, source_kind, source_id),
            )
            return True

    def fail_nlp_document(
        self,
        *,
        source_kind: str,
        source_id: str,
        error_code: str,
    ) -> None:
        with self._connection() as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                UPDATE nlp_documents
                SET state = CASE WHEN retry_count >= 4 THEN 'failed' ELSE 'pending' END,
                    retry_count = retry_count + 1,
                    error_code = left(%s, 500), lease_owner = NULL,
                    lease_expires_at = NULL, updated_at = now()
                WHERE source_kind = %s AND source_id = %s
                """,
                (error_code, source_kind, source_id),
            )

    def enqueue_stale_nlp_documents(self, *, analyzer_version: str) -> int:
        with self._connection() as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                UPDATE nlp_documents
                SET state = 'pending', retry_count = 0, error_code = NULL,
                    lease_owner = NULL, lease_expires_at = NULL, updated_at = now()
                WHERE state IN ('ready', 'failed')
                  AND analyzer_version IS DISTINCT FROM %s
                """,
                (analyzer_version,),
            )
            return max(0, int(cursor.rowcount or 0))
