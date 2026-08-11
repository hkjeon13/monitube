"""PostgreSQL read model for workspace-wide video and comment analysis."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from ..analysis import question_signals_from_texts
from ..analysis_insights import build_video_insights
from ..domain import utcnow
from ..nlp import ANALYZER_VERSION
from ..nlp.tfidf import keyword_scores


class PostgresAnalysisMixin:
    @staticmethod
    def _analysis_tfidf_keywords(
        *,
        cursor: Any,
        params: dict[str, Any],
        corpus_kind: str,
        limit: int = 15,
    ) -> tuple[list[dict[str, int | float | str]], int]:
        scope_kind = "owner"
        scope_id = params["owner_id"]
        target_ids = params["target_ids"]
        if len(target_ids) == 1:
            scope_kind = "target"
            scope_id = target_ids[0]
        elif params["scope"] == "channel" and len(params["channel_ids"]) == 1:
            cursor.execute(
                """
                SELECT target.id
                FROM collection_targets target
                JOIN channels channel ON channel.id = target.resolved_channel_id
                JOIN collection_subscriptions subscription
                  ON subscription.target_id = target.id
                 AND subscription.user_id = %s::uuid
                 AND subscription.enabled = TRUE
                WHERE channel.youtube_channel_id = %s
                ORDER BY target.updated_at DESC, target.id
                LIMIT 1
                """,
                (params["owner_id"], params["channel_ids"][0]),
            )
            target = cursor.fetchone()
            if target:
                scope_kind = "target"
                scope_id = target["id"]

        stats_params = {
            "nlp_scope_kind": scope_kind,
            "nlp_scope_id": scope_id,
            "nlp_corpus_kind": corpus_kind,
            "nlp_analyzer_version": ANALYZER_VERSION,
            "from_at": params["from_at"],
            "to_at": params["to_at"],
            "nlp_keyword_candidate_limit": max(50, limit * 4),
        }
        bounded_period = params["from_at"] is not None or params["to_at"] is not None
        if bounded_period:
            cursor.execute(
                """
                SELECT COALESCE(sum(document_count), 0)::bigint AS document_count
                FROM nlp_daily_corpus_stats
                WHERE scope_kind = %(nlp_scope_kind)s
                  AND scope_id = %(nlp_scope_id)s::uuid
                  AND corpus_kind = %(nlp_corpus_kind)s
                  AND analyzer_version = %(nlp_analyzer_version)s
                  AND (%(from_at)s::timestamptz IS NULL
                       OR document_date >= %(from_at)s::date)
                  AND (%(to_at)s::timestamptz IS NULL
                       OR document_date < %(to_at)s::date)
                """,
                stats_params,
            )
        else:
            cursor.execute(
                """
                SELECT document_count
                FROM nlp_corpus_stats
                WHERE scope_kind = %(nlp_scope_kind)s
                  AND scope_id = %(nlp_scope_id)s::uuid
                  AND corpus_kind = %(nlp_corpus_kind)s
                  AND analyzer_version = %(nlp_analyzer_version)s
                """,
                stats_params,
            )
        corpus = cursor.fetchone()
        document_count = int(corpus["document_count"] if corpus else 0)
        score_params = {**stats_params, "nlp_document_count": document_count}
        if bounded_period:
            cursor.execute(
                """
                WITH term_period AS (
                  SELECT term,
                         sum(document_frequency)::bigint AS document_frequency,
                         sum(total_term_frequency)::bigint AS total_term_frequency
                  FROM nlp_daily_term_stats
                  WHERE scope_kind = %(nlp_scope_kind)s
                    AND scope_id = %(nlp_scope_id)s::uuid
                    AND corpus_kind = %(nlp_corpus_kind)s
                    AND analyzer_version = %(nlp_analyzer_version)s
                    AND (%(from_at)s::timestamptz IS NULL
                         OR document_date >= %(from_at)s::date)
                    AND (%(to_at)s::timestamptz IS NULL
                         OR document_date < %(to_at)s::date)
                  GROUP BY term
                )
                SELECT term, document_frequency, total_term_frequency
                FROM term_period
                ORDER BY (
                  (1 + ln(total_term_frequency::double precision))
                  * (ln(
                      (%(nlp_document_count)s::double precision + 1)
                      / (document_frequency::double precision + 1)
                    ) + 1)
                ) DESC, term
                LIMIT %(nlp_keyword_candidate_limit)s
                """,
                score_params,
            )
        else:
            cursor.execute(
                """
                SELECT term, document_frequency, total_term_frequency
                FROM nlp_term_stats
                WHERE scope_kind = %(nlp_scope_kind)s
                  AND scope_id = %(nlp_scope_id)s::uuid
                  AND corpus_kind = %(nlp_corpus_kind)s
                  AND analyzer_version = %(nlp_analyzer_version)s
                ORDER BY (
                  (1 + ln(total_term_frequency::double precision))
                  * (ln(
                      (%(nlp_document_count)s::double precision + 1)
                      / (document_frequency::double precision + 1)
                    ) + 1)
                ) DESC, term
                LIMIT %(nlp_keyword_candidate_limit)s
                """,
                score_params,
            )
        rows = [dict(row) for row in cursor.fetchall()]
        return (
            keyword_scores(rows, document_count=document_count, limit=limit),
            document_count,
        )

    @staticmethod
    def _analysis_scope_cte(
        bucket: str,
        *,
        include_comment_content: bool = False,
    ) -> str:
        content_columns = (
            """,
                     comment.author_display_name,
                     comment.text_display"""
            if include_comment_content
            else ""
        )
        return f"""
            WITH visible_membership AS MATERIALIZED (
              SELECT DISTINCT membership.video_id,
                     target.id AS target_id,
                     target.type AS target_type,
                     target.config AS target_config,
                     target.canonical_key
              FROM collection_subscriptions subscription
              JOIN collection_targets target
                ON target.id = subscription.target_id
              JOIN collection_target_videos membership
                ON membership.target_id = target.id
              WHERE subscription.user_id = %(owner_id)s::uuid
                AND (
                  %(scope)s::text <> 'keyword'
                  OR target.type = 'keyword'
                )
                AND (
                  cardinality(%(target_ids)s::uuid[]) = 0
                  OR target.id = ANY(%(target_ids)s::uuid[])
                )
            ),
            visible_video AS MATERIALIZED (
              SELECT DISTINCT video.id
              FROM visible_membership membership
              JOIN videos video ON video.id = membership.video_id
              LEFT JOIN channels channel ON channel.id = video.channel_id
              WHERE video.deleted_at IS NULL
                AND (
                  %(scope)s::text <> 'channel'
                  OR cardinality(%(channel_ids)s::text[]) = 0
                  OR channel.youtube_channel_id = ANY(%(channel_ids)s::text[])
                )
            ),
            latest_stats AS MATERIALIZED (
              SELECT visible.id AS video_id,
                     snapshot.view_count,
                     snapshot.like_count,
                     snapshot.comment_count,
                     snapshot.fetched_at
              FROM visible_video visible
              LEFT JOIN LATERAL (
                SELECT current.view_count,
                       current.like_count,
                       current.comment_count,
                       current.fetched_at
                FROM video_stat_snapshots current
                WHERE current.video_id = visible.id
                  AND current.deleted_at IS NULL
                  AND (
                    current.expires_at IS NULL
                    OR current.expires_at > now()
                  )
                ORDER BY current.fetched_at DESC
                LIMIT 1
              ) snapshot ON TRUE
            ),
            video_period AS MATERIALIZED (
              SELECT video.*,
                     channel.youtube_channel_id,
                     channel.title AS channel_title,
                     stats.view_count,
                     stats.like_count,
                     stats.comment_count AS youtube_comment_count,
                     stats.fetched_at AS statistics_fetched_at
              FROM visible_video visible
              JOIN videos video ON video.id = visible.id
              LEFT JOIN channels channel ON channel.id = video.channel_id
              LEFT JOIN latest_stats stats ON stats.video_id = video.id
              WHERE (
                  %(from_at)s::timestamptz IS NULL
                  OR video.published_at >= %(from_at)s::timestamptz
                )
                AND (
                  %(to_at)s::timestamptz IS NULL
                  OR video.published_at < %(to_at)s::timestamptz
                )
            ),
            comment_period AS MATERIALIZED (
              SELECT comment.id,
                     comment.youtube_comment_id,
                     comment.video_id,
                     comment.youtube_parent_comment_id,
                     comment.author_channel_id,
                     comment.like_count,
                     comment.published_at,
                     comment.source_fetched_at
                     {content_columns}
              FROM comments comment
              JOIN visible_video visible ON visible.id = comment.video_id
              WHERE comment.deleted_at IS NULL
                AND (
                  comment.expires_at IS NULL
                  OR comment.expires_at > now()
                )
                AND (
                  %(from_at)s::timestamptz IS NULL
                  OR comment.published_at >= %(from_at)s::timestamptz
                )
                AND (
                  %(to_at)s::timestamptz IS NULL
                  OR comment.published_at < %(to_at)s::timestamptz
                )
                AND (
                  %(comment_type)s::text = 'all'
                  OR (
                    %(comment_type)s::text = 'top_level'
                    AND comment.youtube_parent_comment_id IS NULL
                  )
                  OR (
                    %(comment_type)s::text = 'reply'
                    AND comment.youtube_parent_comment_id IS NOT NULL
                  )
                )
            ),
            video_trend AS (
              SELECT date_trunc('{bucket}', published_at) AS period,
                     count(*)::bigint AS count
              FROM video_period
              WHERE published_at IS NOT NULL
              GROUP BY period
            ),
            comment_trend AS (
              SELECT date_trunc('{bucket}', published_at) AS period,
                     count(*)::bigint AS count,
                     count(*) FILTER (
                       WHERE youtube_parent_comment_id IS NULL
                     )::bigint AS top_level_count,
                     count(*) FILTER (
                       WHERE youtube_parent_comment_id IS NOT NULL
                     )::bigint AS reply_count
              FROM comment_period
              WHERE published_at IS NOT NULL
              GROUP BY period
            )
        """

    @staticmethod
    def _analysis_params(
        *,
        owner_id: str,
        scope: str,
        target_ids: list[str],
        channel_ids: list[str],
        from_at: datetime | None,
        to_at: datetime | None,
        comment_type: str,
        limit: int,
    ) -> dict[str, Any]:
        return {
            "owner_id": owner_id,
            "scope": scope,
            "target_ids": target_ids,
            "channel_ids": channel_ids,
            "from_at": from_at,
            "to_at": to_at,
            "comment_type": comment_type,
            "limit": limit,
        }

    def get_analysis_insights(
        self,
        *,
        owner_id: str,
        scope: str = "all",
        target_ids: list[str] | None = None,
        channel_ids: list[str] | None = None,
        from_at: datetime | None = None,
        to_at: datetime | None = None,
        limit: int = 20,
    ) -> dict[str, Any]:
        params = self._analysis_params(
            owner_id=owner_id,
            scope=scope,
            target_ids=target_ids or [],
            channel_ids=channel_ids or [],
            from_at=from_at,
            to_at=to_at,
            comment_type="all",
            limit=limit,
        )
        with self._connection() as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                WITH visible_membership AS MATERIALIZED (
                  SELECT DISTINCT membership.video_id
                  FROM collection_subscriptions subscription
                  JOIN collection_targets target
                    ON target.id = subscription.target_id
                  JOIN collection_target_videos membership
                    ON membership.target_id = target.id
                  WHERE subscription.user_id = %(owner_id)s::uuid
                    AND (
                      %(scope)s::text <> 'keyword'
                      OR target.type = 'keyword'
                    )
                    AND (
                      cardinality(%(target_ids)s::uuid[]) = 0
                      OR target.id = ANY(%(target_ids)s::uuid[])
                    )
                ),
                visible_video AS MATERIALIZED (
                  SELECT DISTINCT video.id
                  FROM visible_membership membership
                  JOIN videos video ON video.id = membership.video_id
                  LEFT JOIN channels channel ON channel.id = video.channel_id
                  WHERE video.deleted_at IS NULL
                    AND (
                      %(scope)s::text <> 'channel'
                      OR cardinality(%(channel_ids)s::text[]) = 0
                      OR channel.youtube_channel_id
                        = ANY(%(channel_ids)s::text[])
                    )
                    AND (
                      %(from_at)s::timestamptz IS NULL
                      OR video.published_at >= %(from_at)s::timestamptz
                    )
                    AND (
                      %(to_at)s::timestamptz IS NULL
                      OR video.published_at < %(to_at)s::timestamptz
                    )
                )
                SELECT video.youtube_video_id AS id,
                       channel.youtube_channel_id AS channel_id,
                       channel.title AS channel_title,
                       video.title,
                       video.published_at,
                       latest.fetched_at AS statistics_fetched_at,
                       COALESCE(latest.view_count, 0)::bigint AS view_count,
                       COALESCE(latest.like_count, 0)::bigint AS like_count,
                       COALESCE(latest.comment_count, 0)::bigint
                         AS youtube_comment_count,
                       COALESCE(rollup.stored_count, 0)::bigint
                         AS collected_comment_count,
                       baseline.fetched_at AS baseline_fetched_at,
                       baseline.view_count AS baseline_view_count,
                       baseline.like_count AS baseline_like_count,
                       baseline.comment_count AS baseline_comment_count
                FROM visible_video visible
                JOIN videos video ON video.id = visible.id
                LEFT JOIN channels channel ON channel.id = video.channel_id
                LEFT JOIN video_comment_rollups rollup
                  ON rollup.video_id = video.id
                LEFT JOIN LATERAL (
                  SELECT snapshot.fetched_at,
                         snapshot.view_count,
                         snapshot.like_count,
                         snapshot.comment_count
                  FROM video_stat_snapshots snapshot
                  WHERE snapshot.video_id = video.id
                    AND snapshot.deleted_at IS NULL
                    AND (
                      snapshot.expires_at IS NULL
                      OR snapshot.expires_at > now()
                    )
                  ORDER BY snapshot.fetched_at DESC
                  LIMIT 1
                ) latest ON TRUE
                LEFT JOIN LATERAL (
                  SELECT snapshot.fetched_at,
                         snapshot.view_count,
                         snapshot.like_count,
                         snapshot.comment_count
                  FROM video_stat_snapshots snapshot
                  WHERE snapshot.video_id = video.id
                    AND snapshot.deleted_at IS NULL
                    AND (
                      snapshot.expires_at IS NULL
                      OR snapshot.expires_at > now()
                    )
                    AND latest.fetched_at IS NOT NULL
                    AND snapshot.fetched_at
                      <= latest.fetched_at - interval '7 days'
                  ORDER BY snapshot.fetched_at DESC
                  LIMIT 1
                ) baseline ON TRUE
                """,
                params,
            )
            rows = [dict(row) for row in cursor.fetchall()]
        return build_video_insights(rows, limit=limit)

    def get_analysis_overview(
        self,
        *,
        owner_id: str,
        scope: str = "all",
        target_ids: list[str] | None = None,
        channel_ids: list[str] | None = None,
        from_at: datetime | None = None,
        to_at: datetime | None = None,
        comment_type: str = "all",
        section: str = "all",
        limit: int = 10,
    ) -> dict[str, Any]:
        target_ids = target_ids or []
        channel_ids = channel_ids or []
        if from_at and to_at:
            days = max(1, (to_at - from_at).days)
            bucket = "day" if days <= 45 else "week" if days <= 365 else "month"
        else:
            bucket = "month"
        cte = self._analysis_scope_cte(bucket)
        content_cte = self._analysis_scope_cte(
            bucket,
            include_comment_content=True,
        )
        params = self._analysis_params(
            owner_id=owner_id,
            scope=scope,
            target_ids=target_ids,
            channel_ids=channel_ids,
            from_at=from_at,
            to_at=to_at,
            comment_type=comment_type,
            limit=limit,
        )
        if section == "content":
            return self._analysis_content_overview(
                cte=cte,
                content_cte=content_cte,
                params=params,
            )

        with self._connection() as connection, connection.cursor() as cursor:
            cursor.execute(
                cte
                + """
                , video_summary AS (
                  SELECT count(*)::bigint AS video_count,
                         COALESCE(
                           sum(COALESCE(view_count, 0)),
                           0
                         )::bigint AS total_view_count,
                         COALESCE(
                      percentile_disc(0.5) WITHIN GROUP (
                        ORDER BY COALESCE(view_count, 0)
                      ),
                      0
                         )::bigint AS median_view_count,
                         COALESCE(
                           sum(COALESCE(like_count, 0)),
                           0
                         )::bigint AS total_like_count,
                         COALESCE(
                           sum(COALESCE(youtube_comment_count, 0)),
                           0
                         )::bigint AS youtube_comment_count,
                         max(published_at) AS latest_video_published_at,
                         max(statistics_fetched_at) AS statistics_fetched_at,
                         count(*) FILTER (
                           WHERE statistics_fetched_at IS NOT NULL
                         )::bigint AS videos_with_statistics
                  FROM video_period
                ),
                comment_summary AS (
                  SELECT count(*)::bigint AS collected_comment_count,
                         count(DISTINCT video_id)::bigint
                           AS commented_video_count,
                         count(DISTINCT author_channel_id) FILTER (
                           WHERE author_channel_id IS NOT NULL
                         )::bigint AS identified_author_count,
                         count(*) FILTER (
                           WHERE youtube_parent_comment_id IS NULL
                         )::bigint AS top_level_count,
                         count(*) FILTER (
                           WHERE youtube_parent_comment_id IS NOT NULL
                         )::bigint AS reply_count,
                         COALESCE(avg(like_count), 0)::double precision
                           AS average_comment_like_count,
                         max(published_at) AS latest_comment_published_at
                  FROM comment_period
                )
                SELECT video.*, comment.*
                FROM video_summary video
                CROSS JOIN comment_summary comment
                """,
                params,
            )
            summary_row = dict(cursor.fetchone() or {})

            cursor.execute(
                cte
                + """
                SELECT period,
                       count,
                       0::bigint AS top_level_count,
                       0::bigint AS reply_count
                FROM video_trend
                ORDER BY period
                """,
                params,
            )
            video_trend = [
                {
                    "period": row["period"],
                    "count": int(row["count"]),
                    "topLevelCount": 0,
                    "replyCount": 0,
                }
                for row in cursor.fetchall()
            ]

            cursor.execute(
                cte
                + """
                SELECT period, count, top_level_count, reply_count
                FROM comment_trend
                ORDER BY period
                """,
                params,
            )
            comment_trend = [
                {
                    "period": row["period"],
                    "count": int(row["count"]),
                    "topLevelCount": int(row["top_level_count"]),
                    "replyCount": int(row["reply_count"]),
                }
                for row in cursor.fetchall()
            ]

            if scope == "keyword":
                channel_breakdown = []
            else:
                cursor.execute(
                    cte
                    + """
                , video_by_channel AS (
                  SELECT youtube_channel_id AS id,
                         max(COALESCE(channel_title, youtube_channel_id))
                           AS label,
                         count(*)::bigint AS video_count,
                         COALESCE(sum(COALESCE(view_count, 0)), 0)::bigint
                           AS view_count,
                         COALESCE(sum(COALESCE(like_count, 0)), 0)::bigint
                           AS like_count,
                         COALESCE(
                           sum(COALESCE(youtube_comment_count, 0)),
                           0
                         )::bigint AS youtube_comment_count,
                         max(published_at) AS latest_published_at
                  FROM video_period
                  GROUP BY youtube_channel_id
                ),
                comment_by_channel AS (
                  SELECT channel.youtube_channel_id AS id,
                         count(*)::bigint AS collected_comment_count,
                         count(*) FILTER (
                           WHERE comment.youtube_parent_comment_id IS NULL
                         )::bigint AS top_level_count,
                         count(*) FILTER (
                           WHERE comment.youtube_parent_comment_id IS NOT NULL
                         )::bigint AS reply_count
                  FROM comment_period comment
                  JOIN videos video ON video.id = comment.video_id
                  LEFT JOIN channels channel ON channel.id = video.channel_id
                  GROUP BY channel.youtube_channel_id
                )
                SELECT COALESCE(video.id, comment.id, 'unknown') AS id,
                       COALESCE(video.label, comment.id, '알 수 없는 채널')
                         AS label,
                       COALESCE(video.video_count, 0)::bigint AS video_count,
                       COALESCE(video.view_count, 0)::bigint AS view_count,
                       COALESCE(video.like_count, 0)::bigint AS like_count,
                       COALESCE(video.youtube_comment_count, 0)::bigint
                         AS youtube_comment_count,
                       COALESCE(comment.collected_comment_count, 0)::bigint
                         AS collected_comment_count,
                       COALESCE(comment.top_level_count, 0)::bigint
                         AS top_level_count,
                       COALESCE(comment.reply_count, 0)::bigint
                         AS reply_count,
                       video.latest_published_at
                FROM video_by_channel video
                FULL JOIN comment_by_channel comment USING (id)
                ORDER BY COALESCE(video.view_count, 0) DESC,
                         COALESCE(comment.collected_comment_count, 0) DESC
                LIMIT %(limit)s
                """,
                    params,
                )
                channel_breakdown = [
                    self._analysis_breakdown_row(row, kind="channel")
                    for row in cursor.fetchall()
                ]

            if scope == "keyword":
                cursor.execute(
                    cte
                    + """
                , keyword_video AS (
                  SELECT membership.target_id AS id,
                         max(COALESCE(
                           membership.target_config ->> 'query',
                           membership.canonical_key
                         )) AS label,
                         count(DISTINCT video.id)::bigint AS video_count,
                         COALESCE(sum(COALESCE(video.view_count, 0)), 0)::bigint
                           AS view_count,
                         COALESCE(sum(COALESCE(video.like_count, 0)), 0)::bigint
                           AS like_count,
                         COALESCE(
                           sum(COALESCE(video.youtube_comment_count, 0)),
                           0
                         )::bigint AS youtube_comment_count,
                         max(video.published_at) AS latest_published_at
                  FROM visible_membership membership
                  JOIN video_period video ON video.id = membership.video_id
                  WHERE membership.target_type = 'keyword'
                  GROUP BY membership.target_id
                ),
                keyword_comment AS (
                  SELECT membership.target_id AS id,
                         count(DISTINCT comment.id)::bigint
                           AS collected_comment_count,
                         count(DISTINCT comment.id) FILTER (
                           WHERE comment.youtube_parent_comment_id IS NULL
                         )::bigint AS top_level_count,
                         count(DISTINCT comment.id) FILTER (
                           WHERE comment.youtube_parent_comment_id IS NOT NULL
                         )::bigint AS reply_count
                  FROM visible_membership membership
                  JOIN comment_period comment
                    ON comment.video_id = membership.video_id
                  WHERE membership.target_type = 'keyword'
                  GROUP BY membership.target_id
                )
                SELECT COALESCE(video.id, comment.id)::text AS id,
                       COALESCE(video.label, '키워드') AS label,
                       COALESCE(video.video_count, 0)::bigint AS video_count,
                       COALESCE(video.view_count, 0)::bigint AS view_count,
                       COALESCE(video.like_count, 0)::bigint AS like_count,
                       COALESCE(video.youtube_comment_count, 0)::bigint
                         AS youtube_comment_count,
                       COALESCE(comment.collected_comment_count, 0)::bigint
                         AS collected_comment_count,
                       COALESCE(comment.top_level_count, 0)::bigint
                         AS top_level_count,
                       COALESCE(comment.reply_count, 0)::bigint
                         AS reply_count,
                       video.latest_published_at
                FROM keyword_video video
                FULL JOIN keyword_comment comment USING (id)
                ORDER BY COALESCE(video.view_count, 0) DESC,
                         COALESCE(comment.collected_comment_count, 0) DESC
                LIMIT %(limit)s
                """,
                    params,
                )
                keyword_breakdown = [
                    self._analysis_breakdown_row(row, kind="keyword")
                    for row in cursor.fetchall()
                ]
            else:
                keyword_breakdown = []

            cursor.execute(
                cte
                + """
                SELECT video.youtube_video_id AS id,
                       video.youtube_channel_id AS channel_id,
                       video.channel_title,
                       video.title,
                       video.published_at,
                       video.duration_seconds,
                       COALESCE(video.view_count, 0)::bigint AS view_count,
                       COALESCE(video.like_count, 0)::bigint AS like_count,
                       COALESCE(video.youtube_comment_count, 0)::bigint
                         AS youtube_comment_count,
                       COALESCE(rollup.stored_count, 0)::bigint
                         AS collected_comment_count,
                       COALESCE(rollup.top_level_count, 0)::bigint
                         AS top_level_count,
                       COALESCE(rollup.reply_count, 0)::bigint AS reply_count,
                       video.statistics_fetched_at
                FROM video_period video
                LEFT JOIN video_comment_rollups rollup
                  ON rollup.video_id = video.id
                ORDER BY COALESCE(video.view_count, 0) DESC,
                         COALESCE(video.published_at, 'epoch'::timestamptz) DESC,
                         video.youtube_video_id DESC
                LIMIT %(limit)s
                """,
                params,
            )
            top_videos = [
                {
                    "id": str(row["id"]),
                    "channelId": row.get("channel_id"),
                    "channelTitle": row.get("channel_title"),
                    "title": row.get("title"),
                    "publishedAt": row.get("published_at"),
                    "durationSeconds": row.get("duration_seconds"),
                    "viewCount": int(row.get("view_count") or 0),
                    "likeCount": int(row.get("like_count") or 0),
                    "youtubeCommentCount": int(row.get("youtube_comment_count") or 0),
                    "collectedCommentCount": int(
                        row.get("collected_comment_count") or 0
                    ),
                    "topLevelCount": int(row.get("top_level_count") or 0),
                    "replyCount": int(row.get("reply_count") or 0),
                    "statisticsFetchedAt": row.get("statistics_fetched_at"),
                }
                for row in cursor.fetchall()
            ]

            if section == "core":
                top_comments = []
                sampled_texts = []
            else:
                top_comments, sampled_texts = self._analysis_content_rows(
                    cursor=cursor,
                    cte=cte,
                    content_cte=content_cte,
                    params=params,
                )

            coverage_row = self._analysis_coverage_row(
                cursor=cursor,
                params=params,
            )
            video_keywords, indexed_video_documents = self._analysis_tfidf_keywords(
                cursor=cursor,
                params=params,
                corpus_kind="video",
            )
            comment_corpus_kind = (
                "comment_top_level"
                if comment_type == "top_level"
                else "comment_reply"
                if comment_type == "reply"
                else "comment"
            )
            comment_keywords, indexed_comment_documents = self._analysis_tfidf_keywords(
                cursor=cursor,
                params=params,
                corpus_kind=comment_corpus_kind,
            )

        generated_at = utcnow()
        return {
            "summary": {
                "videoCount": int(summary_row.get("video_count") or 0),
                "totalViewCount": int(summary_row.get("total_view_count") or 0),
                "medianViewCount": int(summary_row.get("median_view_count") or 0),
                "totalLikeCount": int(summary_row.get("total_like_count") or 0),
                "youtubeCommentCount": int(
                    summary_row.get("youtube_comment_count") or 0
                ),
                "collectedCommentCount": int(
                    summary_row.get("collected_comment_count") or 0
                ),
                "commentedVideoCount": int(
                    summary_row.get("commented_video_count") or 0
                ),
                "identifiedAuthorCount": int(
                    summary_row.get("identified_author_count") or 0
                ),
                "topLevelCount": int(summary_row.get("top_level_count") or 0),
                "replyCount": int(summary_row.get("reply_count") or 0),
                "averageCommentLikeCount": float(
                    summary_row.get("average_comment_like_count") or 0
                ),
                "latestVideoPublishedAt": summary_row.get("latest_video_published_at"),
                "latestCommentPublishedAt": summary_row.get(
                    "latest_comment_published_at"
                ),
                "statisticsFetchedAt": summary_row.get("statistics_fetched_at"),
            },
            "videoTrend": video_trend,
            "commentTrend": comment_trend,
            "channelBreakdown": channel_breakdown,
            "keywordBreakdown": keyword_breakdown,
            "topVideos": top_videos,
            "topComments": top_comments,
            "topWords": [
                {"word": item["term"], "count": item["termCount"]}
                for item in comment_keywords
            ],
            "videoKeywords": video_keywords,
            "commentKeywords": comment_keywords,
            "keywordCoverage": {
                "indexedVideoDocuments": indexed_video_documents,
                "indexedCommentDocuments": indexed_comment_documents,
                "analyzerVersion": ANALYZER_VERSION,
            },
            "commentSignals": {
                "replyRate": (
                    round(
                        int(summary_row.get("reply_count") or 0)
                        / int(summary_row.get("collected_comment_count") or 1)
                        * 100,
                        2,
                    )
                    if summary_row.get("collected_comment_count")
                    else 0
                ),
                "authorDiversityRate": (
                    round(
                        int(summary_row.get("identified_author_count") or 0)
                        / int(summary_row.get("collected_comment_count") or 1)
                        * 100,
                        2,
                    )
                    if summary_row.get("collected_comment_count")
                    else 0
                ),
                **question_signals_from_texts(sampled_texts),
            },
            "coverage": {
                "visibleTargetCount": int(coverage_row.get("target_count") or 0),
                "includedVideoCount": int(summary_row.get("video_count") or 0),
                "videosWithStatistics": int(
                    summary_row.get("videos_with_statistics") or 0
                ),
                "sampledComments": len(sampled_texts),
                "totalComments": int(summary_row.get("collected_comment_count") or 0),
                "partialData": bool(coverage_row.get("partial_data", False)),
                "generatedAt": generated_at,
            },
        }

    def _analysis_content_overview(
        self,
        *,
        cte: str,
        content_cte: str,
        params: dict[str, Any],
    ) -> dict[str, Any]:
        with self._connection() as connection, connection.cursor() as cursor:
            top_comments, sampled_texts = self._analysis_content_rows(
                cursor=cursor,
                cte=cte,
                content_cte=content_cte,
                params=params,
            )
            coverage_row = self._analysis_coverage_row(
                cursor=cursor,
                params=params,
            )
            video_keywords, indexed_video_documents = self._analysis_tfidf_keywords(
                cursor=cursor,
                params=params,
                corpus_kind="video",
            )
            comment_keywords, indexed_comment_documents = self._analysis_tfidf_keywords(
                cursor=cursor,
                params=params,
                corpus_kind=(
                    "comment_top_level"
                    if params["comment_type"] == "top_level"
                    else "comment_reply"
                    if params["comment_type"] == "reply"
                    else "comment"
                ),
            )
        return {
            "summary": {
                "videoCount": 0,
                "totalViewCount": 0,
                "medianViewCount": 0,
                "totalLikeCount": 0,
                "youtubeCommentCount": 0,
                "collectedCommentCount": 0,
                "commentedVideoCount": 0,
                "identifiedAuthorCount": 0,
                "topLevelCount": 0,
                "replyCount": 0,
                "averageCommentLikeCount": 0,
                "latestVideoPublishedAt": None,
                "latestCommentPublishedAt": None,
                "statisticsFetchedAt": None,
            },
            "videoTrend": [],
            "commentTrend": [],
            "channelBreakdown": [],
            "keywordBreakdown": [],
            "topVideos": [],
            "topComments": top_comments,
            "topWords": [
                {"word": item["term"], "count": item["termCount"]}
                for item in comment_keywords
            ],
            "videoKeywords": video_keywords,
            "commentKeywords": comment_keywords,
            "keywordCoverage": {
                "indexedVideoDocuments": indexed_video_documents,
                "indexedCommentDocuments": indexed_comment_documents,
                "analyzerVersion": ANALYZER_VERSION,
            },
            "commentSignals": {
                "replyRate": 0,
                "authorDiversityRate": 0,
                **question_signals_from_texts(sampled_texts),
            },
            "coverage": {
                "visibleTargetCount": int(coverage_row.get("target_count") or 0),
                "includedVideoCount": 0,
                "videosWithStatistics": 0,
                "sampledComments": len(sampled_texts),
                "totalComments": 0,
                "partialData": bool(coverage_row.get("partial_data", False)),
                "generatedAt": utcnow(),
            },
        }

    @staticmethod
    def _analysis_content_rows(
        *,
        cursor: Any,
        cte: str,
        content_cte: str,
        params: dict[str, Any],
    ) -> tuple[list[dict[str, Any]], list[str]]:
        cursor.execute(
            content_cte
            + """
            SELECT comment.youtube_comment_id AS id,
                   video.youtube_video_id AS video_id,
                   video.title AS video_title,
                   channel.title AS channel_title,
                   comment.text_display AS text,
                   comment.author_display_name AS author_name,
                   comment.published_at,
                   comment.like_count,
                   comment.youtube_parent_comment_id IS NOT NULL AS is_reply
            FROM comment_period comment
            JOIN videos video ON video.id = comment.video_id
            LEFT JOIN channels channel ON channel.id = video.channel_id
            ORDER BY comment.like_count DESC,
                     COALESCE(
                       comment.published_at,
                       comment.source_fetched_at
                     ) DESC,
                     comment.youtube_comment_id DESC
            LIMIT %(limit)s
            """,
            params,
        )
        top_comments = [
            {
                "id": str(row["id"]),
                "videoId": str(row["video_id"]),
                "videoTitle": row.get("video_title"),
                "channelTitle": row.get("channel_title"),
                "text": row.get("text"),
                "authorName": row.get("author_name"),
                "publishedAt": row.get("published_at"),
                "likeCount": int(row.get("like_count") or 0),
                "isReply": bool(row.get("is_reply")),
            }
            for row in cursor.fetchall()
        ]

        sample_limit = 5000
        sample_params = {
            **params,
            "sample_limit": sample_limit,
            "sample_candidate_limit": sample_limit * 4,
        }
        cursor.execute(
            cte
            + """
            , sample_candidates AS (
              SELECT id
              FROM comment_period
              ORDER BY COALESCE(
                         published_at,
                         source_fetched_at
                       ) DESC,
                       youtube_comment_id DESC
              LIMIT %(sample_candidate_limit)s
            )
            SELECT stored.text_display
            FROM sample_candidates candidate
            JOIN comments stored ON stored.id = candidate.id
            WHERE stored.text_display IS NOT NULL
              AND btrim(stored.text_display) <> ''
            LIMIT %(sample_limit)s
            """,
            sample_params,
        )
        sampled_texts = [str(row["text_display"]) for row in cursor.fetchall()]
        return top_comments, sampled_texts

    @staticmethod
    def _analysis_coverage_row(
        *,
        cursor: Any,
        params: dict[str, Any],
    ) -> dict[str, Any]:
        cursor.execute(
            """
            SELECT count(DISTINCT target.id)::bigint AS target_count,
                   COALESCE(bool_or(
                     COALESCE(target.coverage ->> 'status', 'complete')
                       IN ('limited', 'unknown')
                   ), false) AS partial_data
            FROM collection_subscriptions subscription
            JOIN collection_targets target
              ON target.id = subscription.target_id
            WHERE subscription.user_id = %(owner_id)s::uuid
              AND (
                %(scope)s::text <> 'keyword'
                OR target.type = 'keyword'
              )
              AND (
                cardinality(%(target_ids)s::uuid[]) = 0
                OR target.id = ANY(%(target_ids)s::uuid[])
              )
            """,
            params,
        )
        return dict(cursor.fetchone() or {})

    @staticmethod
    def _analysis_breakdown_row(
        row: dict[str, Any],
        *,
        kind: str,
    ) -> dict[str, Any]:
        return {
            "id": str(row["id"]),
            "label": str(row.get("label") or row["id"]),
            "kind": kind,
            "videoCount": int(row.get("video_count") or 0),
            "viewCount": int(row.get("view_count") or 0),
            "likeCount": int(row.get("like_count") or 0),
            "youtubeCommentCount": int(row.get("youtube_comment_count") or 0),
            "collectedCommentCount": int(row.get("collected_comment_count") or 0),
            "topLevelCount": int(row.get("top_level_count") or 0),
            "replyCount": int(row.get("reply_count") or 0),
            "latestPublishedAt": row.get("latest_published_at"),
        }
