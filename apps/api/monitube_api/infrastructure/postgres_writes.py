"""PostgreSQL collected-content writes and request accounting."""

from dataclasses import replace
import hashlib
from typing import Any, Iterable

from psycopg.types.json import Json

from ..domain import CommentRecord, JobRecord, QuotaBucket, VideoRecord
from ..repositories import NotFoundError, RepositoryError
from .postgres_support import _optional_nonnegative_int, _strip_nul


class PostgresCollectionWriteMixin:
    def upsert_channel(self, channel: dict[str, Any]) -> dict[str, Any]:
        channel = _strip_nul(channel)
        statistics = dict(channel.get("statistics") or {})
        payload = {**channel, "thumbnail_url": channel.get("thumbnail_url")}
        with self._connection() as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO channels (youtube_channel_id, handle, title, description, thumbnail_url, uploads_playlist_id, source_fetched_at)
                VALUES (%(youtube_channel_id)s, %(handle)s, %(title)s, %(description)s, %(thumbnail_url)s, %(uploads_playlist_id)s, %(source_fetched_at)s)
                ON CONFLICT (youtube_channel_id) DO UPDATE SET
                  handle = COALESCE(EXCLUDED.handle, channels.handle),
                  title = COALESCE(EXCLUDED.title, channels.title),
                  description = COALESCE(EXCLUDED.description, channels.description),
                  thumbnail_url = COALESCE(EXCLUDED.thumbnail_url, channels.thumbnail_url),
                  uploads_playlist_id = COALESCE(EXCLUDED.uploads_playlist_id, channels.uploads_playlist_id),
                  source_fetched_at = EXCLUDED.source_fetched_at
                RETURNING id::text, youtube_channel_id
                """,
                payload,
            )
            stored = dict(cursor.fetchone())
            if statistics:
                cursor.execute(
                    """
                    INSERT INTO channel_snapshots (channel_id, fetched_at, subscriber_count, view_count, video_count, hidden_subscriber_count)
                    VALUES (%s, %s, %s, %s, %s, %s)
                    ON CONFLICT (channel_id, fetched_at) DO UPDATE SET
                      subscriber_count = EXCLUDED.subscriber_count, view_count = EXCLUDED.view_count,
                      video_count = EXCLUDED.video_count, hidden_subscriber_count = EXCLUDED.hidden_subscriber_count
                    """,
                    (
                        stored["id"], channel["source_fetched_at"],
                        _optional_nonnegative_int(statistics.get("subscriberCount")),
                        _optional_nonnegative_int(statistics.get("viewCount")),
                        _optional_nonnegative_int(statistics.get("videoCount")),
                        bool(statistics.get("hiddenSubscriberCount")) if statistics.get("hiddenSubscriberCount") is not None else None,
                    ),
                )
            return stored

    def upsert_video(self, video: VideoRecord) -> VideoRecord:
        video = replace(
            video,
            youtube_video_id=_strip_nul(video.youtube_video_id),
            youtube_channel_id=_strip_nul(video.youtube_channel_id),
            title=_strip_nul(video.title),
            description=_strip_nul(video.description),
            privacy_status=_strip_nul(video.privacy_status),
        )
        with self._connection() as connection, connection.cursor() as cursor:
            cursor.execute(
                "SELECT id FROM channels WHERE youtube_channel_id = %s",
                (video.youtube_channel_id,),
            )
            channel_row = cursor.fetchone() if video.youtube_channel_id else None
            channel_id = channel_row["id"] if channel_row else None
            cursor.execute(
                """
                INSERT INTO videos (
                  youtube_video_id, channel_id, title, description, published_at, duration_seconds,
                  privacy_status, made_for_kids, source_fetched_at
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (youtube_video_id) DO UPDATE SET
                  channel_id = EXCLUDED.channel_id, title = EXCLUDED.title, description = EXCLUDED.description,
                  published_at = EXCLUDED.published_at, duration_seconds = EXCLUDED.duration_seconds,
                  privacy_status = EXCLUDED.privacy_status, made_for_kids = EXCLUDED.made_for_kids,
                  source_fetched_at = EXCLUDED.source_fetched_at
                RETURNING id::text
                """,
                (
                    video.youtube_video_id,
                    channel_id,
                    video.title,
                    video.description,
                    video.published_at,
                    video.duration_seconds,
                    video.privacy_status,
                    video.made_for_kids,
                    video.source_fetched_at,
                ),
            )
            internal_id = str(cursor.fetchone()["id"])
            cursor.execute(
                """
                INSERT INTO video_stat_snapshots (video_id, fetched_at, view_count, like_count, comment_count)
                VALUES (%s, %s, %s, %s, %s)
                ON CONFLICT (video_id, fetched_at) DO UPDATE SET
                  view_count = EXCLUDED.view_count, like_count = EXCLUDED.like_count, comment_count = EXCLUDED.comment_count
                """,
                (
                    internal_id,
                    video.source_fetched_at,
                    video.statistics.get("viewCount", 0),
                    video.statistics.get("likeCount", 0),
                    video.statistics.get("commentCount", 0),
                ),
            )
            return replace(video, id=internal_id)

    def get_videos_by_youtube_ids(self, youtube_video_ids: Iterable[str]) -> dict[str, VideoRecord]:
        video_ids = list(dict.fromkeys(youtube_video_ids))
        if not video_ids:
            return {}
        with self._connection() as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT v.id::text, v.youtube_video_id, c.youtube_channel_id, v.title, v.description,
                       v.published_at, v.duration_seconds, v.privacy_status, v.made_for_kids,
                       COALESCE(
                         (SELECT jsonb_build_object(
                           'viewCount', vs.view_count,
                           'likeCount', vs.like_count,
                           'commentCount', vs.comment_count
                         )
                         FROM video_stat_snapshots vs
                         WHERE vs.video_id = v.id
                         ORDER BY vs.fetched_at DESC
                         LIMIT 1),
                         '{}'::jsonb
                       ) AS statistics,
                       v.source_fetched_at
                FROM videos v
                LEFT JOIN channels c ON c.id = v.channel_id
                WHERE v.youtube_video_id = ANY(%s)
                """,
                (video_ids,),
            )
            return {row["youtube_video_id"]: self._video(row) for row in cursor.fetchall()}

    def count_videos_by_channel(self, youtube_channel_id: str) -> int:
        with self._connection() as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT count(*)::integer AS video_count
                FROM videos v
                JOIN channels c ON c.id = v.channel_id
                WHERE c.youtube_channel_id = %s
                """,
                (youtube_channel_id,),
            )
            return int(cursor.fetchone()["video_count"])

    def link_source_video(self, source_id: str, youtube_video_id: str) -> None:
        with self._connection() as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO source_videos (source_id, video_id)
                SELECT %s, id FROM videos WHERE youtube_video_id = %s
                ON CONFLICT (source_id, video_id) DO UPDATE SET last_seen_at = now()
                """,
                (source_id, youtube_video_id),
            )
            if cursor.rowcount != 1:
                raise NotFoundError(f"Video '{youtube_video_id}' was not found")
            cursor.execute(
                """
                INSERT INTO collection_target_videos (target_id, video_id)
                SELECT collection_sources.target_id, video.id FROM collection_sources
                CROSS JOIN (SELECT id FROM videos WHERE youtube_video_id = %s) video
                WHERE collection_sources.id = %s AND collection_sources.target_id IS NOT NULL
                ON CONFLICT (target_id, video_id) DO UPDATE SET last_seen_at = now()
                """,
                (youtube_video_id, source_id),
            )

    def source_video_ids(self, source_id: str, youtube_video_ids: Iterable[str]) -> set[str]:
        ids = list(dict.fromkeys(youtube_video_ids))
        if not ids:
            return set()
        with self._connection() as connection, connection.cursor() as cursor:
            cursor.execute(
                """SELECT video.youtube_video_id FROM source_videos source_video
                   JOIN videos video ON video.id = source_video.video_id
                   WHERE source_video.source_id = %s AND video.youtube_video_id = ANY(%s)""",
                (source_id, ids),
            )
            return {str(row["youtube_video_id"]) for row in cursor.fetchall()}

    def count_source_videos(self, source_id: str) -> int:
        with self._connection() as connection, connection.cursor() as cursor:
            cursor.execute("SELECT count(*)::integer AS video_count FROM source_videos WHERE source_id = %s", (source_id,))
            return int(cursor.fetchone()["video_count"])

    def upsert_comment(self, comment: CommentRecord) -> CommentRecord:
        return self.persist_comment_page([comment])[0]

    def persist_comment_page(
        self,
        comments: Iterable[CommentRecord],
        *,
        job_id: str | None = None,
        checkpoint: dict[str, Any] | None = None,
    ) -> list[CommentRecord]:
        """Persist one upstream page using one connection and one transaction.

        Top-level comments are written before replies so parent foreign keys are
        resolved deterministically.  The rollup is recomputed while holding the
        video row lock: replaying a page after a crash therefore cannot inflate
        counts.  A supplied checkpoint commits in the same transaction.
        """

        sanitized_page = [
            replace(
                comment,
                youtube_comment_id=_strip_nul(comment.youtube_comment_id),
                youtube_video_id=_strip_nul(comment.youtube_video_id),
                youtube_parent_comment_id=_strip_nul(comment.youtube_parent_comment_id),
                youtube_thread_id=_strip_nul(comment.youtube_thread_id),
                text_display=_strip_nul(comment.text_display),
            )
            for comment in comments
        ]
        # Upstream pages should already be unique, but de-duplicating here keeps
        # both the SQL upsert and rollup delta idempotent under malformed/replayed
        # responses.  Dict insertion order preserves the first-seen page order.
        page_by_id: dict[str, CommentRecord] = {}
        for comment in sanitized_page:
            page_by_id[comment.youtube_comment_id] = comment
        page = list(page_by_id.values())
        video_ids = {comment.youtube_video_id for comment in page}
        if len(video_ids) > 1:
            raise RepositoryError("A comment page must belong to exactly one video")
        if checkpoint is not None and not job_id:
            raise RepositoryError("job_id is required when persisting a checkpoint")

        with self._connection() as connection, connection.cursor() as cursor:
            video: dict[str, Any] | None = None
            existing_comments: dict[str, dict[str, Any]] = {}
            absolute_rollup_required = False
            if video_ids:
                youtube_video_id = next(iter(video_ids))
                cursor.execute("SELECT id FROM videos WHERE youtube_video_id = %s FOR UPDATE", (youtube_video_id,))
                video = cursor.fetchone()
                if not video:
                    raise NotFoundError(f"Video '{youtube_video_id}' was not found")
                cursor.execute(
                    """
                    SELECT youtube_comment_id, video_id, youtube_parent_comment_id,
                           COALESCE(published_at, source_fetched_at) AS effective_published_at
                    FROM comments
                    WHERE youtube_comment_id = ANY(%s)
                    FOR UPDATE
                    """,
                    ([item.youtube_comment_id for item in page],),
                )
                existing_comments = {
                    str(item["youtube_comment_id"]): dict(item)
                    for item in cursor.fetchall()
                }
                for item in page:
                    previous = existing_comments.get(item.youtube_comment_id)
                    if previous is None:
                        continue
                    if str(previous["video_id"]) != str(video["id"]):
                        raise RepositoryError(
                            f"Comment '{item.youtube_comment_id}' cannot move between videos"
                        )
                    effective_published_at = item.published_at or item.source_fetched_at
                    if (
                        previous.get("youtube_parent_comment_id")
                        != item.youtube_parent_comment_id
                        or previous.get("effective_published_at")
                        != effective_published_at
                    ):
                        # Category changes and a correction to the current maximum
                        # cannot be represented safely as a simple positive delta.
                        absolute_rollup_required = True

            upsert_sql = """
                INSERT INTO comments (
                  youtube_comment_id, video_id, parent_id, youtube_parent_comment_id, youtube_thread_id,
                  author_channel_id, author_display_name, text_display, text_original, like_count,
                  published_at, updated_at, source_fetched_at
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (youtube_comment_id) DO UPDATE SET
                  video_id = EXCLUDED.video_id, parent_id = EXCLUDED.parent_id,
                  youtube_parent_comment_id = EXCLUDED.youtube_parent_comment_id,
                  youtube_thread_id = EXCLUDED.youtube_thread_id,
                  text_display = EXCLUDED.text_display, text_original = EXCLUDED.text_original,
                  author_channel_id = EXCLUDED.author_channel_id,
                  author_display_name = EXCLUDED.author_display_name,
                  like_count = EXCLUDED.like_count, published_at = EXCLUDED.published_at,
                  updated_at = EXCLUDED.updated_at, source_fetched_at = EXCLUDED.source_fetched_at
            """

            def parameters(comment: CommentRecord, parent_id: Any | None) -> tuple[Any, ...]:
                return (
                    comment.youtube_comment_id,
                    video["id"] if video else None,
                    parent_id,
                    comment.youtube_parent_comment_id,
                    comment.youtube_thread_id,
                    comment.author_channel_id,
                    comment.author_display_name,
                    comment.text_display,
                    comment.text_display,
                    comment.like_count,
                    comment.published_at,
                    comment.updated_at,
                    comment.source_fetched_at,
                )

            top_level = [item for item in page if not item.youtube_parent_comment_id]
            replies = [item for item in page if item.youtube_parent_comment_id]
            if top_level:
                cursor.executemany(upsert_sql, [parameters(item, None) for item in top_level])

            parent_ids = list(
                dict.fromkeys(
                    item.youtube_parent_comment_id
                    for item in replies
                    if item.youtube_parent_comment_id
                )
            )
            parent_map: dict[str, Any] = {}
            if parent_ids:
                cursor.execute(
                    """
                    SELECT youtube_comment_id, id
                    FROM comments
                    WHERE youtube_comment_id = ANY(%s)
                    """,
                    (parent_ids,),
                )
                parent_map = {
                    str(item["youtube_comment_id"]): item["id"]
                    for item in cursor.fetchall()
                }
            if replies:
                cursor.executemany(
                    upsert_sql,
                    [
                        parameters(item, parent_map.get(str(item.youtube_parent_comment_id)))
                        for item in replies
                    ],
                )

            stored_by_youtube_id: dict[str, CommentRecord] = {}
            if page:
                cursor.execute(
                    """
                    SELECT youtube_comment_id, id::text
                    FROM comments
                    WHERE youtube_comment_id = ANY(%s)
                    """,
                    ([item.youtube_comment_id for item in page],),
                )
                database_ids = {
                    str(item["youtube_comment_id"]): str(item["id"])
                    for item in cursor.fetchall()
                }
                stored_by_youtube_id = {
                    item.youtube_comment_id: replace(
                        item, id=database_ids[item.youtube_comment_id]
                    )
                    for item in page
                }

            if video is not None and self.enable_comment_rollup_dual_write:
                cursor.execute(
                    "SELECT video_id FROM video_comment_rollups WHERE video_id = %s FOR UPDATE",
                    (video["id"],),
                )
                rollup_exists = cursor.fetchone() is not None
                new_comments = [
                    item for item in page if item.youtube_comment_id not in existing_comments
                ]
                if absolute_rollup_required or not rollup_exists:
                    cursor.execute(
                        """
                        INSERT INTO video_comment_rollups (
                          video_id, stored_count, top_level_count, reply_count,
                          latest_published_at, updated_at, last_reconciled_at
                        )
                        SELECT
                          %s,
                          count(*)::bigint,
                          count(*) FILTER (WHERE youtube_parent_comment_id IS NULL)::bigint,
                          count(*) FILTER (WHERE youtube_parent_comment_id IS NOT NULL)::bigint,
                          max(COALESCE(published_at, source_fetched_at)),
                          now(), now()
                        FROM comments
                        WHERE video_id = %s
                        ON CONFLICT (video_id) DO UPDATE SET
                          stored_count = EXCLUDED.stored_count,
                          top_level_count = EXCLUDED.top_level_count,
                          reply_count = EXCLUDED.reply_count,
                          latest_published_at = EXCLUDED.latest_published_at,
                          updated_at = EXCLUDED.updated_at,
                          last_reconciled_at = EXCLUDED.last_reconciled_at
                        """,
                        (video["id"], video["id"]),
                    )
                elif new_comments:
                    top_level_delta = sum(
                        1 for item in new_comments if not item.youtube_parent_comment_id
                    )
                    reply_delta = len(new_comments) - top_level_delta
                    effective_times = [
                        item.published_at or item.source_fetched_at
                        for item in new_comments
                        if item.published_at or item.source_fetched_at
                    ]
                    latest_delta = max(effective_times) if effective_times else None
                    cursor.execute(
                        """
                        UPDATE video_comment_rollups
                        SET stored_count = stored_count + %s,
                            top_level_count = top_level_count + %s,
                            reply_count = reply_count + %s,
                            latest_published_at = CASE
                              WHEN %s::timestamptz IS NULL THEN latest_published_at
                              WHEN latest_published_at IS NULL THEN %s::timestamptz
                              ELSE GREATEST(latest_published_at, %s::timestamptz)
                            END,
                            updated_at = now()
                        WHERE video_id = %s
                        """,
                        (
                            len(new_comments),
                            top_level_delta,
                            reply_delta,
                            latest_delta,
                            latest_delta,
                            latest_delta,
                            video["id"],
                        ),
                    )

            if checkpoint is not None and job_id is not None:
                cursor.execute(
                    """
                    UPDATE sync_jobs
                    SET checkpoint = %s, updated_at = now()
                    WHERE id = %s AND state = 'running'
                    """,
                    (Json(checkpoint), job_id),
                )
                if cursor.rowcount != 1:
                    raise RepositoryError(f"Running job '{job_id}' was not found while checkpointing")
                cursor.execute(
                    """
                    INSERT INTO sync_checkpoints (
                      job_id, stage, scope_key, request_hash, page_token, batch_cursor, checkpoint_seq
                    ) VALUES (%s, %s, %s, %s, %s, %s, 1)
                    ON CONFLICT (job_id, stage, scope_key) DO UPDATE SET
                      request_hash = EXCLUDED.request_hash,
                      page_token = EXCLUDED.page_token,
                      batch_cursor = EXCLUDED.batch_cursor,
                      checkpoint_seq = sync_checkpoints.checkpoint_seq + 1,
                      updated_at = now()
                    """,
                    (
                        job_id,
                        str(checkpoint.get("stage", "comments")),
                        str(checkpoint.get("scopeKey", "job")),
                        hashlib.sha256(str(sorted(checkpoint.items())).encode("utf-8")).hexdigest(),
                        checkpoint.get("pageToken"),
                        int(checkpoint.get("batchCursor", 0)),
                    ),
                )

            return [stored_by_youtube_id[item.youtube_comment_id] for item in page]

    def existing_comment_ids(self, youtube_comment_ids: Iterable[str]) -> set[str]:
        comment_ids = list(dict.fromkeys(youtube_comment_ids))
        if not comment_ids:
            return set()
        with self._connection() as connection, connection.cursor() as cursor:
            cursor.execute("SELECT youtube_comment_id FROM comments WHERE youtube_comment_id = ANY(%s)", (comment_ids,))
            return {str(row["youtube_comment_id"]) for row in cursor.fetchall()}

    def comment_counts_by_video(self, youtube_video_ids: Iterable[str]) -> dict[str, int]:
        video_ids = list(dict.fromkeys(youtube_video_ids))
        if not video_ids:
            return {}
        with self._connection() as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT v.youtube_video_id, count(c.id)::integer AS comment_count
                FROM videos v
                LEFT JOIN comments c ON c.video_id = v.id
                WHERE v.youtube_video_id = ANY(%s)
                GROUP BY v.youtube_video_id
                """,
                (video_ids,),
            )
            return {str(row["youtube_video_id"]): int(row["comment_count"]) for row in cursor.fetchall()}

    def record_api_request(
        self, *, job_id: str, bucket: QuotaBucket, endpoint: str, status_code: int, error_reason: str | None = None
    ) -> None:
        with self._connection() as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO api_request_logs (job_id, runtime_config_id, bucket, endpoint, parameter_hash, expected_cost, actual_cost, http_status, error_reason)
                SELECT id, runtime_config_id, %s, %s, %s, 1, 1, %s, %s FROM sync_jobs WHERE id = %s
                """,
                (bucket.value, endpoint, "server-managed", status_code, error_reason, job_id),
            )
