"""PostgreSQL result, analysis, explore, and search read models."""

from typing import Any, Iterable

from psycopg.types.json import Json

from ..analysis import top_words_from_texts
from ..domain import (
    CommentRecord,
    JobState,
    SourceRecord,
    SourceType,
    VideoRecord,
    utcnow,
)
from ..fuzzy_search import normalize_search_text, rank_text_fields
from ..repositories import (
    CommentThreadSort,
    NotFoundError,
    RepositoryError,
    decode_comment_cursor,
    decode_comment_thread_cursor,
    decode_explore_video_cursor,
    decode_source_video_cursor,
    encode_comment_cursor,
    encode_comment_thread_cursor,
    encode_explore_video_cursor,
    encode_source_video_cursor,
    explore_video_filter_hash,
    source_video_filter_hash,
)


class PostgresResultMixin:
    def _source_videos(self, cursor: Any, source_id: str) -> list[VideoRecord]:
        cursor.execute(
            """
            SELECT v.id::text, v.youtube_video_id, c.youtube_channel_id, v.title, v.description, v.published_at,
                   v.duration_seconds, v.privacy_status, v.made_for_kids, v.source_fetched_at,
                   jsonb_build_object(
                     'viewCount', COALESCE(stats.view_count, 0), 'likeCount', COALESCE(stats.like_count, 0),
                     'commentCount', COALESCE(stats.comment_count, 0)
                   ) AS statistics
            FROM source_videos sv
            JOIN videos v ON v.id = sv.video_id
            LEFT JOIN channels c ON c.id = v.channel_id
            LEFT JOIN LATERAL (
              SELECT view_count, like_count, comment_count FROM video_stat_snapshots
              WHERE video_id = v.id ORDER BY fetched_at DESC LIMIT 1
            ) stats ON TRUE
            WHERE sv.source_id = %s
            ORDER BY v.published_at DESC NULLS LAST, v.youtube_video_id
            """,
            (source_id,),
        )
        return [self._video(row) for row in cursor.fetchall()]

    def _target_videos(self, cursor: Any, target_id: str) -> list[VideoRecord]:
        cursor.execute(
            """
            SELECT v.id::text, v.youtube_video_id, c.youtube_channel_id, v.title, v.description, v.published_at,
                   v.duration_seconds, v.privacy_status, v.made_for_kids, v.source_fetched_at,
                   jsonb_build_object(
                     'viewCount', COALESCE(stats.view_count, 0), 'likeCount', COALESCE(stats.like_count, 0),
                     'commentCount', COALESCE(stats.comment_count, 0)
                   ) AS statistics
            FROM collection_target_videos tv
            JOIN videos v ON v.id = tv.video_id
            LEFT JOIN channels c ON c.id = v.channel_id
            LEFT JOIN LATERAL (
              SELECT view_count, like_count, comment_count FROM video_stat_snapshots
              WHERE video_id = v.id ORDER BY fetched_at DESC LIMIT 1
            ) stats ON TRUE
            WHERE tv.target_id = %s
            ORDER BY v.published_at DESC NULLS LAST, v.youtube_video_id
            """,
            (target_id,),
        )
        return [self._video(row) for row in cursor.fetchall()]

    def _comments(self, cursor: Any, video_ids: list[str]) -> list[CommentRecord]:
        if not video_ids:
            return []
        cursor.execute(
            """
            SELECT cm.id::text, cm.youtube_comment_id, v.youtube_video_id, cm.youtube_parent_comment_id,
                   cm.youtube_thread_id, cm.author_channel_id, cm.author_display_name, cm.text_display, cm.like_count, cm.published_at, cm.updated_at, cm.source_fetched_at
            FROM comments cm JOIN videos v ON v.id = cm.video_id
            WHERE v.youtube_video_id = ANY(%s)
            ORDER BY cm.published_at DESC NULLS LAST, cm.youtube_comment_id
            """,
            (video_ids,),
        )
        return [self._comment(row) for row in cursor.fetchall()]

    def _resolve_source_scope(
        self, cursor: Any, source_id: str, *, owner_id: str | None
    ) -> tuple[SourceRecord, str | None, str]:
        """Resolve a public subscription/source ID to its bounded read scope."""

        source_row = self._select_subscription_source(cursor, source_id, owner_id=owner_id)
        if source_row:
            source = self._source(source_row)
            return source, source.target_id, source_id
        if self._select_subscription(cursor, source_id) is not None:
            raise NotFoundError(f"Source '{source_id}' was not found")
        source_row = self._select_source(cursor, source_id)
        if not source_row:
            raise NotFoundError(f"Source '{source_id}' was not found")
        if owner_id is not None:
            cursor.execute(
                "SELECT 1 FROM collection_sources WHERE id = %s AND owner_id = %s",
                (source_id, owner_id),
            )
            if not cursor.fetchone():
                raise NotFoundError(f"Source '{source_id}' was not found")
        source = self._source(source_row)
        return source, source.target_id, source_id

    def get_scope_data_version(
        self, *, target_id: str | None, source_id: str
    ) -> int:
        """Return the monotonic identity used only for derived cache keys."""

        with self._connection() as connection, connection.cursor() as cursor:
            if target_id:
                cursor.execute(
                    "SELECT data_version FROM collection_targets WHERE id = %s",
                    (target_id,),
                )
            else:
                cursor.execute(
                    "SELECT data_version FROM collection_sources WHERE id = %s AND target_id IS NULL",
                    (source_id,),
                )
            row = cursor.fetchone()
            if not row:
                raise NotFoundError("Collection data scope was not found")
            return int(row["data_version"] or 0)

    def get_owner_explore_generation(self, *, owner_id: str) -> str:
        """Hash subscription ACL/update state and every visible target version."""

        with self._connection() as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT md5(COALESCE(string_agg(
                  concat_ws(':', subscription.target_id::text, target.data_version::text,
                            extract(epoch FROM subscription.updated_at)::text),
                  ',' ORDER BY subscription.target_id
                ), '')) AS generation
                FROM collection_subscriptions subscription
                JOIN collection_targets target ON target.id = subscription.target_id
                WHERE subscription.user_id = %s
                """,
                (owner_id,),
            )
            return str(cursor.fetchone()["generation"])

    def list_active_parent_jobs(self, *, owner_id: str) -> list[dict[str, Any]]:
        """Return only user-visible, active coordinator jobs in one query."""

        with self._connection() as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT subscription.id::text AS public_source_id, job.*
                FROM collection_subscriptions subscription
                JOIN sync_jobs job ON job.target_id = subscription.target_id
                WHERE subscription.user_id = %s
                  AND job.parent_job_id IS NULL
                  AND job.state NOT IN ('completed', 'completed_with_warnings', 'failed', 'cancelled')
                UNION ALL
                SELECT source.id::text AS public_source_id, job.*
                FROM collection_sources source
                JOIN sync_jobs job ON job.source_id = source.id
                WHERE source.owner_id = %s
                  AND source.target_id IS NULL
                  AND job.target_id IS NULL
                  AND job.parent_job_id IS NULL
                  AND job.state NOT IN ('completed', 'completed_with_warnings', 'failed', 'cancelled')
                ORDER BY created_at DESC
                """,
                (owner_id, owner_id),
            )
            return [
                {
                    "source_id": str(row["public_source_id"]),
                    "target_id": str(row["target_id"]) if row.get("target_id") else None,
                    "job": self._job(row),
                }
                for row in cursor.fetchall()
            ]

    def list_recent_failed_parent_jobs(
        self, *, owner_id: str, limit: int = 10
    ) -> list[dict[str, Any]]:
        """Return owner-scoped failed coordinator jobs in reverse failure order."""

        with self._connection() as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                WITH visible_parent_failures AS MATERIALIZED (
                  SELECT subscription.id::text AS public_source_id,
                         target.id::text AS public_target_id,
                         target.type AS public_source_type,
                         COALESCE(NULLIF(subscription.display_config, '{}'::jsonb), target.config)
                           AS public_source_config,
                         target.canonical_key AS public_canonical_key,
                         job.updated_at AS failed_at,
                         job.*
                  FROM collection_subscriptions subscription
                  JOIN collection_targets target ON target.id = subscription.target_id
                  JOIN sync_jobs job ON job.target_id = target.id
                  WHERE subscription.user_id = %s
                    AND job.updated_at >= subscription.created_at
                    AND job.parent_job_id IS NULL
                    AND job.state = 'failed'

                  UNION ALL

                  SELECT source.id::text AS public_source_id,
                         NULL::text AS public_target_id,
                         source.type AS public_source_type,
                         source.config AS public_source_config,
                         NULL::text AS public_canonical_key,
                         job.updated_at AS failed_at,
                         job.*
                  FROM collection_sources source
                  JOIN sync_jobs job ON job.source_id = source.id
                  WHERE source.owner_id = %s
                    AND source.target_id IS NULL
                    AND job.target_id IS NULL
                    AND job.parent_job_id IS NULL
                    AND job.state = 'failed'
                  ORDER BY failed_at DESC, id DESC
                  LIMIT %s
                ),
                ranked_failed_children AS (
                  SELECT child.parent_job_id,
                         child.pause_reason AS representative_child_pause_reason,
                         child.partial_errors AS representative_child_partial_errors,
                         count(*) OVER (PARTITION BY child.parent_job_id)::integer
                           AS failed_child_count,
                         row_number() OVER (
                           PARTITION BY child.parent_job_id
                           ORDER BY
                             CASE
                               WHEN NULLIF(btrim(child.pause_reason), '') IS NOT NULL
                                 OR jsonb_array_length(COALESCE(child.partial_errors, '[]'::jsonb)) > 0
                               THEN 0 ELSE 1
                             END,
                             child.updated_at DESC,
                             child.id DESC
                         ) AS failure_rank
                  FROM sync_jobs child
                  JOIN visible_parent_failures parent ON parent.id = child.parent_job_id
                  WHERE child.parent_job_id IS NOT NULL
                    AND child.state = 'failed'
                )
                SELECT parent.*,
                       COALESCE(child.failed_child_count, 0)::integer AS failed_child_count,
                       child.representative_child_pause_reason,
                       COALESCE(child.representative_child_partial_errors, '[]'::jsonb)
                         AS representative_child_partial_errors
                FROM visible_parent_failures parent
                LEFT JOIN ranked_failed_children child
                  ON child.parent_job_id = parent.id AND child.failure_rank = 1
                ORDER BY parent.failed_at DESC, parent.id DESC
                """,
                (owner_id, owner_id, limit),
            )
            return [
                {
                    "source_id": str(row["public_source_id"]),
                    "target_id": str(row["public_target_id"])
                    if row.get("public_target_id")
                    else None,
                    "source_type": SourceType(row["public_source_type"]),
                    "source_config": dict(row.get("public_source_config") or {}),
                    "canonical_key": row.get("public_canonical_key"),
                    "failed_at": row["failed_at"],
                    "job": self._job(row),
                    "failed_child_count": int(row.get("failed_child_count") or 0),
                    "representative_child_pause_reason": row.get(
                        "representative_child_pause_reason"
                    ),
                    "representative_child_partial_errors": list(
                        row.get("representative_child_partial_errors") or []
                    ),
                }
                for row in cursor.fetchall()
            ]

    @staticmethod
    def _visible_video_cte(target_id: str | None) -> str:
        if target_id:
            return """
                SELECT membership.video_id, membership.first_seen_at
                FROM collection_target_videos membership
                WHERE membership.target_id = %s
            """
        return """
            SELECT membership.video_id, membership.first_seen_at
            FROM source_videos membership
            WHERE membership.source_id = %s
        """

    def get_source_overview(self, source_id: str, *, owner_id: str | None = None) -> dict[str, Any]:
        """Return exact aggregates and rankings without hydrating comment text."""

        with self._connection() as connection, connection.cursor() as cursor:
            source, target_id, membership_id = self._resolve_source_scope(
                cursor, source_id, owner_id=owner_id
            )
            scope_value = target_id or membership_id
            if target_id:
                cursor.execute(
                    "SELECT data_version, coverage FROM collection_targets WHERE id = %s",
                    (target_id,),
                )
            else:
                cursor.execute(
                    "SELECT data_version, '{}'::jsonb AS coverage FROM collection_sources WHERE id = %s",
                    (membership_id,),
                )
            version_row = cursor.fetchone() or {"data_version": 0, "coverage": {}}
            data_version = int(version_row.get("data_version") or 0)

            if target_id:
                cursor.execute(
                    """
                    SELECT * FROM sync_jobs
                    WHERE target_id = %s AND parent_job_id IS NULL
                    ORDER BY created_at DESC LIMIT 1
                    """,
                    (target_id,),
                )
            else:
                cursor.execute(
                    """
                    SELECT * FROM sync_jobs
                    WHERE source_id = %s AND target_id IS NULL AND parent_job_id IS NULL
                    ORDER BY created_at DESC LIMIT 1
                    """,
                    (membership_id,),
                )
            latest_job_row = cursor.fetchone()
            latest_job = self._job(latest_job_row) if latest_job_row else None
            if target_id:
                cursor.execute(
                    """
                    SELECT * FROM sync_jobs
                    WHERE target_id = %s AND parent_job_id IS NULL
                      AND state IN ('completed', 'completed_with_warnings', 'failed', 'cancelled')
                    ORDER BY created_at DESC LIMIT 1
                    """,
                    (target_id,),
                )
            else:
                cursor.execute(
                    """
                    SELECT * FROM sync_jobs
                    WHERE source_id = %s AND target_id IS NULL AND parent_job_id IS NULL
                      AND state IN ('completed', 'completed_with_warnings', 'failed', 'cancelled')
                    ORDER BY created_at DESC LIMIT 1
                    """,
                    (membership_id,),
                )
            latest_terminal_row = cursor.fetchone()
            latest_terminal = self._job(latest_terminal_row) if latest_terminal_row else None

            visible_cte = self._visible_video_cte(target_id)
            if self.enable_comment_rollup_read:
                aggregate_sql = f"""
                    WITH visible_video AS ({visible_cte})
                    SELECT count(*)::bigint AS video_count,
                           COALESCE(sum(rollup.stored_count), 0)::bigint AS comment_count,
                           max(video.published_at) AS latest_video_published_at,
                           max(rollup.latest_published_at) AS latest_comment_published_at
                    FROM visible_video membership
                    JOIN videos video ON video.id = membership.video_id
                    LEFT JOIN video_comment_rollups rollup ON rollup.video_id = video.id
                """
            else:
                aggregate_sql = f"""
                    WITH visible_video AS ({visible_cte}), comment_aggregate AS (
                      SELECT comment.video_id, count(*)::bigint AS stored_count,
                             max(COALESCE(comment.published_at, comment.source_fetched_at)) AS latest_published_at
                      FROM comments comment
                      JOIN visible_video membership ON membership.video_id = comment.video_id
                      GROUP BY comment.video_id
                    )
                    SELECT count(*)::bigint AS video_count,
                           COALESCE(sum(comment_aggregate.stored_count), 0)::bigint AS comment_count,
                           max(video.published_at) AS latest_video_published_at,
                           max(comment_aggregate.latest_published_at) AS latest_comment_published_at
                    FROM visible_video membership
                    JOIN videos video ON video.id = membership.video_id
                    LEFT JOIN comment_aggregate ON comment_aggregate.video_id = video.id
                """
            cursor.execute(aggregate_sql, (scope_value,))
            exact = cursor.fetchone() or {}

            summary_row: dict[str, Any] | None = None
            if self.enable_target_summary_read and target_id:
                cursor.execute(
                    """
                    SELECT run.data_version, run.job_id::text, run.completed_at, run.coverage,
                           result.payload
                    FROM analysis_runs run
                    JOIN analysis_results result ON result.analysis_run_id = run.id
                    WHERE run.target_id = %s AND run.state = 'completed'
                      AND run.pipeline_version = 'deterministic-v2'
                      AND result.result_kind = 'basic_summary'
                      AND result.deleted_at IS NULL
                      AND (result.expires_at IS NULL OR result.expires_at > now())
                    ORDER BY run.data_version DESC, run.completed_at DESC
                    LIMIT 1
                    """,
                    (target_id,),
                )
                summary_row = cursor.fetchone()
            elif self.enable_target_summary_read:
                cursor.execute(
                    """
                    SELECT run.data_version, run.job_id::text, run.completed_at, run.coverage,
                           result.payload
                    FROM analysis_runs run
                    JOIN analysis_results result ON result.analysis_run_id = run.id
                    WHERE run.target_id IS NULL AND run.source_id = %s
                      AND run.state = 'completed'
                      AND run.pipeline_version = 'deterministic-v2'
                      AND result.result_kind = 'basic_summary'
                      AND result.deleted_at IS NULL
                      AND (result.expires_at IS NULL OR result.expires_at > now())
                    ORDER BY run.data_version DESC, run.completed_at DESC
                    LIMIT 1
                    """,
                    (membership_id,),
                )
                summary_row = cursor.fetchone()
            summary_payload = dict(summary_row.get("payload") or {}) if summary_row else {}
            summary_version = int(summary_row.get("data_version") or 0) if summary_row else -1

            if target_id:
                cursor.execute(
                    """
                    SELECT run.state, run.coverage FROM analysis_runs run
                    WHERE run.target_id = %s AND run.data_version = %s
                      AND run.pipeline_version = 'deterministic-v2'
                    ORDER BY run.created_at DESC LIMIT 1
                    """,
                    (target_id, data_version),
                )
            else:
                cursor.execute(
                    """
                    SELECT run.state, run.coverage FROM analysis_runs run
                    WHERE run.target_id IS NULL AND run.source_id = %s
                      AND run.data_version = %s
                      AND run.pipeline_version = 'deterministic-v2'
                    ORDER BY run.created_at DESC LIMIT 1
                    """,
                    (membership_id, data_version),
                )
            current_run = cursor.fetchone()
            run_state = str(current_run["state"]) if current_run else None
            analysis_coverage = dict(
                (current_run or {}).get("coverage")
                or (summary_row or {}).get("coverage")
                or {}
            )
            status = "fresh"
            if summary_version == data_version:
                top_words_status = "fresh"
            elif run_state in {"queued", "running"}:
                top_words_status = "building"
            elif run_state == "failed":
                top_words_status = "failed"
            else:
                top_words_status = "stale" if summary_row else "building"

            partial_data = bool(
                latest_terminal
                and latest_terminal.state
                in {JobState.COMPLETED_WITH_WARNINGS, JobState.FAILED, JobState.CANCELLED}
            )
            if latest_terminal and latest_terminal.state in {JobState.FAILED, JobState.CANCELLED}:
                status = "stale" if summary_row else "failed"
                top_words_status = "stale" if summary_row else "failed"

            generated_at = (
                summary_payload.get("generatedAt")
                or (summary_row.get("completed_at") if summary_row else None)
                or utcnow()
            )
            summary = {
                "videoCount": int(exact.get("video_count") or 0),
                "commentCount": int(exact.get("comment_count") or 0),
                "latestVideoPublishedAt": exact.get("latest_video_published_at"),
                "latestCommentPublishedAt": exact.get("latest_comment_published_at"),
                "topWords": list(summary_payload.get("topWords") or []),
                "generatedAt": generated_at,
                "asOfJobId": (
                    (str(summary_row["job_id"]) if summary_row.get("job_id") else None)
                    if summary_row
                    else (latest_terminal.id if latest_terminal else None)
                ),
                "dataVersion": data_version,
                "status": status,
                "topWordsStatus": top_words_status,
                "partialData": partial_data,
                "coverage": analysis_coverage,
            }

            top_videos: dict[str, list[VideoRecord]] = {}
            metric_columns = {
                "views": "view_count",
                "likes": "like_count",
                "comments": "comment_count",
            }
            for label, column in metric_columns.items():
                cursor.execute(
                    f"""
                    WITH visible_video AS ({visible_cte})
                    SELECT video.id::text, video.youtube_video_id, channel.youtube_channel_id,
                           video.title, video.description, video.published_at, video.duration_seconds,
                           video.privacy_status, video.made_for_kids, video.source_fetched_at,
                           jsonb_build_object(
                             'viewCount', COALESCE(stats.view_count, 0),
                             'likeCount', COALESCE(stats.like_count, 0),
                             'commentCount', COALESCE(stats.comment_count, 0)
                           ) AS statistics
                    FROM visible_video membership
                    JOIN videos video ON video.id = membership.video_id
                    LEFT JOIN channels channel ON channel.id = video.channel_id
                    LEFT JOIN LATERAL (
                      SELECT view_count, like_count, comment_count
                      FROM video_stat_snapshots snapshot
                      WHERE snapshot.video_id = video.id
                      ORDER BY snapshot.fetched_at DESC LIMIT 1
                    ) stats ON TRUE
                    ORDER BY COALESCE(stats.{column}, 0) DESC,
                             COALESCE(video.published_at, video.source_fetched_at, 'epoch'::timestamptz) DESC,
                             video.youtube_video_id DESC
                    LIMIT 6
                    """,
                    (scope_value,),
                )
                top_videos[label] = [self._video(row) for row in cursor.fetchall()]

            return {
                "source": source,
                "latest_job": latest_job,
                "summary": summary,
                "top_videos": top_videos,
            }

    def get_source_videos_page(
        self,
        source_id: str,
        *,
        owner_id: str | None = None,
        cursor: str | None = None,
        limit: int = 60,
    ) -> dict[str, Any]:
        with self._connection() as connection, connection.cursor() as db_cursor:
            _, target_id, membership_id = self._resolve_source_scope(
                db_cursor, source_id, owner_id=owner_id
            )
            filter_hash = source_video_filter_hash()
            decoded = decode_source_video_cursor(
                cursor, scope=source_id, filter_hash=filter_hash
            )
            snapshot_at = decoded.snapshot_at if decoded else utcnow()
            scope_value = target_id or membership_id
            visible_cte = self._visible_video_cte(target_id)
            db_cursor.execute(
                f"""
                WITH visible_video AS ({visible_cte})
                SELECT count(*)::bigint AS total
                FROM visible_video
                WHERE first_seen_at <= %s
                """,
                (scope_value, snapshot_at),
            )
            total = int(db_cursor.fetchone()["total"])

            after_sql = ""
            params: list[Any] = [scope_value, snapshot_at]
            if decoded:
                after_sql = """
                  AND (
                    COALESCE(video.published_at, video.source_fetched_at, 'epoch'::timestamptz),
                    video.youtube_video_id
                  ) < (%s, %s)
                """
                params.extend([decoded.effective_at, decoded.youtube_video_id])
            params.append(limit + 1)
            db_cursor.execute(
                f"""
                WITH visible_video AS ({visible_cte})
                SELECT video.id::text, video.youtube_video_id, channel.youtube_channel_id,
                       video.title, video.description, video.published_at, video.duration_seconds,
                       video.privacy_status, video.made_for_kids,
                       COALESCE(video.source_fetched_at, 'epoch'::timestamptz) AS source_fetched_at,
                       jsonb_build_object(
                         'viewCount', COALESCE(stats.view_count, 0),
                         'likeCount', COALESCE(stats.like_count, 0),
                         'commentCount', COALESCE(stats.comment_count, 0)
                       ) AS statistics
                FROM visible_video membership
                JOIN videos video ON video.id = membership.video_id
                LEFT JOIN channels channel ON channel.id = video.channel_id
                LEFT JOIN LATERAL (
                  SELECT view_count, like_count, comment_count
                  FROM video_stat_snapshots snapshot
                  WHERE snapshot.video_id = video.id
                  ORDER BY snapshot.fetched_at DESC LIMIT 1
                ) stats ON TRUE
                WHERE membership.first_seen_at <= %s
                {after_sql}
                ORDER BY COALESCE(video.published_at, video.source_fetched_at, 'epoch'::timestamptz) DESC,
                         video.youtube_video_id DESC
                LIMIT %s
                """,
                params,
            )
            candidates = [self._video(row) for row in db_cursor.fetchall()]
            has_more = len(candidates) > limit
            videos = candidates[:limit]
            next_cursor = (
                encode_source_video_cursor(
                    videos[-1],
                    snapshot_at=snapshot_at,
                    scope=source_id,
                    filter_hash=filter_hash,
                )
                if has_more and videos
                else None
            )
            return {
                "videos": videos,
                "next_cursor": next_cursor,
                "snapshot_at": snapshot_at,
                "total": total,
            }

    def enqueue_missing_analysis_runs(self, *, limit: int = 100) -> int:
        """Seed current target/source versions without duplicating active work."""

        with self._connection() as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                WITH candidate AS (
                  SELECT target.id AS target_id, target.data_version,
                         source.id AS source_id, latest_job.id AS job_id
                  FROM collection_targets target
                  LEFT JOIN LATERAL (
                    SELECT id FROM collection_sources
                    WHERE target_id = target.id ORDER BY created_at LIMIT 1
                  ) source ON TRUE
                  LEFT JOIN LATERAL (
                    SELECT id FROM sync_jobs
                    WHERE target_id = target.id AND parent_job_id IS NULL
                      AND state IN ('completed', 'completed_with_warnings')
                    ORDER BY created_at DESC LIMIT 1
                  ) latest_job ON TRUE
                  WHERE NOT EXISTS (
                    SELECT 1 FROM analysis_runs run
                    WHERE run.target_id = target.id
                      AND run.data_version = target.data_version
                      AND run.pipeline_version = 'deterministic-v2'
                  )
                  ORDER BY target.updated_at DESC
                  LIMIT %s
                )
                INSERT INTO analysis_runs (
                  source_id, target_id, job_id, data_version, state,
                  pipeline_version, policy_gate_version, sample_plan
                )
                SELECT source_id, target_id, job_id, data_version, 'queued',
                       'deterministic-v2', 'server-managed', %s
                FROM candidate
                ON CONFLICT DO NOTHING
                """,
                (
                    limit,
                    Json({"strategy": "per-video-recent", "maxComments": 50_000, "maxPerVideo": 1_000}),
                ),
            )
            inserted = max(0, cursor.rowcount)
            cursor.execute(
                """
                WITH candidate AS (
                  SELECT source.id AS source_id, source.data_version,
                         latest_job.id AS job_id
                  FROM collection_sources source
                  LEFT JOIN LATERAL (
                    SELECT id FROM sync_jobs
                    WHERE source_id = source.id AND target_id IS NULL
                      AND parent_job_id IS NULL
                      AND state IN ('completed', 'completed_with_warnings')
                    ORDER BY created_at DESC LIMIT 1
                  ) latest_job ON TRUE
                  WHERE source.target_id IS NULL
                    AND NOT EXISTS (
                      SELECT 1 FROM analysis_runs run
                      WHERE run.target_id IS NULL AND run.source_id = source.id
                        AND run.data_version = source.data_version
                        AND run.pipeline_version = 'deterministic-v2'
                    )
                  ORDER BY source.updated_at DESC
                  LIMIT %s
                )
                INSERT INTO analysis_runs (
                  source_id, job_id, data_version, state,
                  pipeline_version, policy_gate_version, sample_plan
                )
                SELECT source_id, job_id, data_version, 'queued',
                       'deterministic-v2', 'server-managed', %s
                FROM candidate
                ON CONFLICT DO NOTHING
                """,
                (
                    max(0, limit - inserted),
                    Json({"strategy": "per-video-recent", "maxComments": 50_000, "maxPerVideo": 1_000}),
                ),
            )
            return inserted + max(0, cursor.rowcount)

    def claim_next_analysis_run(
        self, *, worker_id: str, lease_seconds: int = 900
    ) -> dict[str, Any] | None:
        with self._connection() as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                WITH candidate AS (
                  SELECT id
                  FROM analysis_runs
                  WHERE (
                    (state = 'queued' AND (resume_at IS NULL OR resume_at <= now()))
                    OR (state = 'running' AND lease_expires_at <= now())
                  )
                  ORDER BY created_at
                  FOR UPDATE SKIP LOCKED
                  LIMIT 1
                )
                UPDATE analysis_runs run
                SET state = 'running', lease_owner = %s,
                    lease_expires_at = now() + (%s * interval '1 second'),
                    started_at = COALESCE(started_at, now()), resume_at = NULL,
                    last_error = NULL
                FROM candidate
                WHERE run.id = candidate.id
                RETURNING run.id::text, run.source_id::text, run.target_id::text,
                          run.job_id::text, run.data_version, run.retry_count,
                          run.sample_plan, run.coverage
                """,
                (worker_id, lease_seconds),
            )
            row = cursor.fetchone()
            return dict(row) if row else None

    def complete_analysis_run(
        self,
        run_id: str,
        *,
        worker_id: str,
        max_comments: int = 50_000,
        max_per_video: int = 1_000,
    ) -> dict[str, Any]:
        """Build one bounded deterministic summary and atomically publish it."""

        with self._connection() as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT * FROM analysis_runs
                WHERE id = %s AND state = 'running' AND lease_owner = %s
                FOR UPDATE
                """,
                (run_id, worker_id),
            )
            run = cursor.fetchone()
            if not run:
                raise RepositoryError(f"Analysis run '{run_id}' is no longer leased by this worker")
            target_id = str(run["target_id"]) if run.get("target_id") else None
            source_id = str(run["source_id"]) if run.get("source_id") else None
            if not target_id and not source_id:
                raise RepositoryError(f"Analysis run '{run_id}' has no target or source scope")
            visible_cte = self._visible_video_cte(target_id)
            scope_value = target_id or source_id
            cursor.execute(
                f"""
                WITH visible_video AS ({visible_cte}), comment_aggregate AS (
                  SELECT comment.video_id, count(*)::bigint AS stored_count,
                         max(COALESCE(comment.published_at, comment.source_fetched_at)) AS latest_published_at
                  FROM comments comment
                  JOIN visible_video membership ON membership.video_id = comment.video_id
                  GROUP BY comment.video_id
                )
                SELECT count(*)::bigint AS video_count,
                       COALESCE(sum(comment_aggregate.stored_count), 0)::bigint AS comment_count,
                       max(video.published_at) AS latest_video_published_at,
                       max(comment_aggregate.latest_published_at) AS latest_comment_published_at
                FROM visible_video membership
                JOIN videos video ON video.id = membership.video_id
                LEFT JOIN comment_aggregate ON comment_aggregate.video_id = video.id
                """,
                (scope_value,),
            )
            aggregate = cursor.fetchone() or {}
            video_count = int(aggregate.get("video_count") or 0)
            per_video_limit = min(
                max_per_video,
                max(1, (max_comments + max(1, video_count) - 1) // max(1, video_count)),
            )
            cursor.execute(
                f"""
                WITH visible_video AS ({visible_cte})
                SELECT sample.text_display
                FROM visible_video membership
                JOIN LATERAL (
                  SELECT comment.text_display
                  FROM comments comment
                  WHERE comment.video_id = membership.video_id
                    AND comment.text_display IS NOT NULL
                    AND btrim(comment.text_display) <> ''
                  ORDER BY COALESCE(comment.published_at, comment.source_fetched_at, 'epoch'::timestamptz) DESC,
                           comment.youtube_comment_id DESC
                  LIMIT %s
                ) sample ON TRUE
                ORDER BY membership.video_id
                LIMIT %s
                """,
                (scope_value, per_video_limit, max_comments),
            )
            sampled_texts = [str(row["text_display"]) for row in cursor.fetchall()]
            generated_at = utcnow()
            summary = {
                "videoCount": video_count,
                "commentCount": int(aggregate.get("comment_count") or 0),
                "latestVideoPublishedAt": aggregate.get("latest_video_published_at"),
                "latestCommentPublishedAt": aggregate.get("latest_comment_published_at"),
                "topWords": top_words_from_texts(sampled_texts),
                "generatedAt": generated_at,
            }
            coverage = dict(run.get("coverage") or {})
            coverage.update(
                {
                    "sampledComments": len(sampled_texts),
                    "totalComments": summary["commentCount"],
                    "sampleRatio": (
                        len(sampled_texts) / summary["commentCount"]
                        if summary["commentCount"]
                        else 1.0
                    ),
                }
            )
            cursor.execute(
                """
                INSERT INTO analysis_results (analysis_run_id, result_kind, payload)
                VALUES (%s, 'basic_summary', %s)
                ON CONFLICT (analysis_run_id, result_kind) WHERE deleted_at IS NULL
                DO UPDATE SET payload = EXCLUDED.payload, created_at = now()
                """,
                (run_id, Json(self._json_safe(summary))),
            )
            cursor.execute(
                """
                UPDATE analysis_runs
                SET state = 'completed', completed_at = now(), coverage = %s,
                    lease_owner = NULL, lease_expires_at = NULL, last_error = NULL
                WHERE id = %s AND lease_owner = %s
                """,
                (Json(coverage), run_id, worker_id),
            )
            if cursor.rowcount != 1:
                raise RepositoryError(f"Analysis run '{run_id}' lease was lost")
            return summary

    def fail_analysis_run(
        self,
        run_id: str,
        *,
        worker_id: str,
        error: str,
        max_retries: int = 3,
    ) -> str:
        with self._connection() as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                UPDATE analysis_runs
                SET retry_count = retry_count + 1,
                    state = CASE WHEN retry_count + 1 >= %s THEN 'failed' ELSE 'queued' END,
                    resume_at = CASE
                      WHEN retry_count + 1 >= %s THEN NULL
                      ELSE now() + (power(2, retry_count) * interval '30 seconds')
                    END,
                    last_error = %s, lease_owner = NULL, lease_expires_at = NULL,
                    completed_at = CASE WHEN retry_count + 1 >= %s THEN now() ELSE NULL END
                WHERE id = %s AND state = 'running' AND lease_owner = %s
                RETURNING state
                """,
                (max_retries, max_retries, error[:1_000], max_retries, run_id, worker_id),
            )
            row = cursor.fetchone()
            return str(row["state"]) if row else "lost"

    def save_analysis_summary(self, source_id: str) -> dict[str, Any]:
        """Compatibility producer: enqueue bounded analysis instead of reading all comments."""

        with self._connection() as connection, connection.cursor() as cursor:
            cursor.execute(
                "SELECT id::text, target_id::text, data_version FROM collection_sources WHERE id = %s",
                (source_id,),
            )
            source = cursor.fetchone()
            if not source:
                raise NotFoundError(f"Source '{source_id}' was not found")
            target_id = str(source["target_id"]) if source.get("target_id") else None
            if target_id:
                cursor.execute("SELECT data_version FROM collection_targets WHERE id = %s", (target_id,))
                version = int(cursor.fetchone()["data_version"])
            else:
                version = int(source.get("data_version") or 0)
            cursor.execute(
                """
                INSERT INTO analysis_runs (
                  source_id, target_id, data_version, state,
                  pipeline_version, policy_gate_version, sample_plan
                )
                SELECT %s, %s, %s, 'queued', 'deterministic-v2',
                       'server-managed', %s
                WHERE NOT EXISTS (
                  SELECT 1 FROM analysis_runs
                  WHERE data_version = %s AND pipeline_version = 'deterministic-v2'
                    AND (
                      (%s::uuid IS NOT NULL AND target_id = %s::uuid)
                      OR (%s::uuid IS NULL AND target_id IS NULL AND source_id = %s::uuid)
                    )
                )
                ON CONFLICT DO NOTHING
                RETURNING id::text
                """,
                (
                    source_id,
                    target_id,
                    version,
                    Json({"strategy": "per-video-recent", "maxComments": 50_000, "maxPerVideo": 1_000}),
                    version,
                    target_id,
                    target_id,
                    target_id,
                    source_id,
                ),
            )
            row = cursor.fetchone()
            return {"queued": bool(row), "dataVersion": version, "analysisRunId": str(row["id"]) if row else None}

    def get_source_results(self, source_id: str, *, owner_id: str | None = None) -> dict[str, Any]:
        """Legacy compatibility response without hydrating comment rows.

        The additive overview/video APIs are preferred, but older clients still
        receive the complete video list and exact summary.  No comment text ever
        crosses the database boundary for this endpoint.
        """

        overview = self.get_source_overview(source_id, owner_id=owner_id)
        with self._connection() as connection, connection.cursor() as cursor:
            source = overview["source"]
            videos = (
                self._target_videos(cursor, source.target_id)
                if source.target_id
                else self._source_videos(cursor, source_id)
            )
            return {
                "source": source,
                "latest_job": overview.get("latest_job"),
                "videos": videos,
                "comments": [],
                "analysis": overview["summary"],
            }
