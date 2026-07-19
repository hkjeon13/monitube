"""PostgreSQL video comment, thread, reply, and detail read models."""

from typing import Any

from ..domain import CommentRecord, utcnow
from ..repositories import (
    CommentThreadSort,
    NotFoundError,
    decode_comment_cursor,
    decode_comment_thread_cursor,
    encode_comment_cursor,
    encode_comment_thread_cursor,
)


class PostgresCommentReadMixin:
    def get_video_comments(self, video_id: str, *, owner_id: str | None = None) -> dict[str, Any]:
        with self._connection() as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT v.id::text, v.youtube_video_id, c.youtube_channel_id, v.title, v.description, v.published_at,
                       v.duration_seconds, v.privacy_status, v.made_for_kids, v.source_fetched_at,
                       jsonb_build_object('viewCount', COALESCE(stats.view_count, 0), 'likeCount', COALESCE(stats.like_count, 0), 'commentCount', COALESCE(stats.comment_count, 0)) AS statistics
                FROM videos v
                LEFT JOIN channels c ON c.id = v.channel_id
                LEFT JOIN LATERAL (SELECT view_count, like_count, comment_count FROM video_stat_snapshots WHERE video_id = v.id ORDER BY fetched_at DESC LIMIT 1) stats ON TRUE
                WHERE (v.youtube_video_id = %s OR v.id::text = %s)
                  AND (
                    %s::uuid IS NULL
                    OR EXISTS (
                      SELECT 1
                      FROM collection_target_videos membership
                      JOIN collection_subscriptions subscription ON subscription.target_id = membership.target_id
                      WHERE membership.video_id = v.id AND subscription.user_id = %s::uuid
                    )
                  )
                """,
                (video_id, video_id, owner_id, owner_id),
            )
            row = cursor.fetchone()
            if not row:
                raise NotFoundError(f"Video '{video_id}' was not found")
            video = self._video(row)
            cursor.execute(
                """
                SELECT comment.id::text, comment.youtube_comment_id,
                       video.youtube_video_id, comment.youtube_parent_comment_id,
                       comment.youtube_thread_id, comment.author_channel_id,
                       comment.author_display_name, comment.text_display,
                       comment.like_count, comment.published_at, comment.updated_at,
                       comment.source_fetched_at
                FROM comments comment
                JOIN videos video ON video.id = comment.video_id
                WHERE comment.video_id = %s
                ORDER BY COALESCE(comment.published_at, comment.source_fetched_at, 'epoch'::timestamptz) DESC,
                         comment.youtube_comment_id DESC
                LIMIT 100
                """,
                (row["id"],),
            )
            comments = [self._comment(item) for item in cursor.fetchall()]
            cursor.execute(
                """
                SELECT count(*)::bigint AS comment_count,
                       max(COALESCE(published_at, source_fetched_at)) AS latest_comment_published_at
                FROM comments WHERE video_id = %s
                """,
                (row["id"],),
            )
            aggregate = cursor.fetchone()
            summary = {
                "videoCount": 1,
                "commentCount": int(aggregate["comment_count"]),
                "latestVideoPublishedAt": video.published_at,
                "latestCommentPublishedAt": aggregate.get("latest_comment_published_at"),
                "topWords": top_words_from_texts(comment.text_display for comment in comments),
                "generatedAt": utcnow(),
            }
            return {"video": video, "comments": comments, "summary": summary}

    def get_video_comment_threads(
        self, video_id: str, *, owner_id: str | None = None, cursor: str | None = None,
        limit: int = 20, sort: CommentThreadSort = "newest"
    ) -> dict[str, Any]:
        cursor_key = decode_comment_thread_cursor(cursor, sort)
        with self._connection() as connection, connection.cursor() as db_cursor:
            db_cursor.execute(
                """
                SELECT v.id::text, v.youtube_video_id, c.youtube_channel_id, v.title, v.description, v.published_at,
                       v.duration_seconds, v.privacy_status, v.made_for_kids, v.source_fetched_at,
                       jsonb_build_object('viewCount', COALESCE(stats.view_count, 0), 'likeCount', COALESCE(stats.like_count, 0), 'commentCount', COALESCE(stats.comment_count, 0)) AS statistics
                FROM videos v
                LEFT JOIN channels c ON c.id = v.channel_id
                LEFT JOIN LATERAL (SELECT view_count, like_count, comment_count FROM video_stat_snapshots WHERE video_id = v.id ORDER BY fetched_at DESC LIMIT 1) stats ON TRUE
                WHERE (v.youtube_video_id = %s OR v.id::text = %s)
                  AND (
                    %s::uuid IS NULL
                    OR EXISTS (
                      SELECT 1
                      FROM collection_target_videos membership
                      JOIN collection_subscriptions subscription ON subscription.target_id = membership.target_id
                      WHERE membership.video_id = v.id AND subscription.user_id = %s::uuid
                    )
                  )
                """,
                (video_id, video_id, owner_id, owner_id),
            )
            video_row = db_cursor.fetchone()
            if not video_row:
                raise NotFoundError(f"Video '{video_id}' was not found")
            video = self._video(video_row)

            params: list[Any] = [video_row["id"]]
            cursor_filter = ""
            if sort == "oldest":
                order_by = """
                  COALESCE(cm.published_at, cm.source_fetched_at, 'epoch'::timestamptz) ASC,
                  cm.youtube_comment_id ASC
                """
                if cursor_key:
                    cursor_filter = """
                      AND (COALESCE(cm.published_at, cm.source_fetched_at, 'epoch'::timestamptz), cm.youtube_comment_id)
                          > (%s::timestamptz, %s)
                    """
                    params.extend(cursor_key)
            elif sort == "recommended":
                order_by = """
                  COALESCE(cm.like_count, 0) DESC,
                  COALESCE(cm.published_at, cm.source_fetched_at, 'epoch'::timestamptz) DESC,
                  cm.youtube_comment_id DESC
                """
                if cursor_key:
                    cursor_filter = """
                      AND (COALESCE(cm.like_count, 0),
                           COALESCE(cm.published_at, cm.source_fetched_at, 'epoch'::timestamptz),
                           cm.youtube_comment_id)
                          < (%s::bigint, %s::timestamptz, %s)
                    """
                    params.extend(cursor_key)
            else:
                order_by = """
                  COALESCE(cm.published_at, cm.source_fetched_at, 'epoch'::timestamptz) DESC,
                  cm.youtube_comment_id DESC
                """
                if cursor_key:
                    cursor_filter = """
                      AND (COALESCE(cm.published_at, cm.source_fetched_at, 'epoch'::timestamptz), cm.youtube_comment_id)
                          < (%s::timestamptz, %s)
                    """
                    params.extend(cursor_key)
            params.append(limit + 1)
            db_cursor.execute(
                f"""
                SELECT cm.id::text, cm.youtube_comment_id, v.youtube_video_id, cm.youtube_parent_comment_id,
                       cm.youtube_thread_id, cm.author_channel_id, cm.author_display_name, cm.text_display,
                       cm.like_count, cm.published_at, cm.updated_at, cm.source_fetched_at
                FROM comments cm
                JOIN videos v ON v.id = cm.video_id
                WHERE cm.video_id = %s AND cm.youtube_parent_comment_id IS NULL
                {cursor_filter}
                ORDER BY {order_by}
                LIMIT %s
                """,
                tuple(params),
            )
            page = [self._comment(row) for row in db_cursor.fetchall()]
            has_more = len(page) > limit
            page = page[:limit]

            reply_groups: dict[str, list[CommentRecord]] = {item.youtube_comment_id: [] for item in page}
            reply_counts: dict[str, int] = {item.youtube_comment_id: 0 for item in page}
            parent_ids = [item.youtube_comment_id for item in page]
            if parent_ids:
                db_cursor.execute(
                    """
                    WITH ranked_replies AS (
                      SELECT cm.id::text, cm.youtube_comment_id, v.youtube_video_id, cm.youtube_parent_comment_id,
                             cm.youtube_thread_id, cm.author_channel_id, cm.author_display_name, cm.text_display,
                             cm.like_count, cm.published_at, cm.updated_at, cm.source_fetched_at,
                             COUNT(*) OVER (PARTITION BY cm.youtube_parent_comment_id) AS reply_count,
                             ROW_NUMBER() OVER (
                               PARTITION BY cm.youtube_parent_comment_id
                               ORDER BY COALESCE(cm.published_at, cm.source_fetched_at, 'epoch'::timestamptz) ASC,
                                        cm.youtube_comment_id ASC
                             ) AS reply_rank
                      FROM comments cm
                      JOIN videos v ON v.id = cm.video_id
                      WHERE cm.youtube_parent_comment_id = ANY(%s)
                    )
                    SELECT * FROM ranked_replies
                    WHERE reply_rank <= 2
                    ORDER BY youtube_parent_comment_id, reply_rank
                    """,
                    (parent_ids,),
                )
                for reply_row in db_cursor.fetchall():
                    parent_id = str(reply_row["youtube_parent_comment_id"])
                    reply_counts[parent_id] = int(reply_row["reply_count"])
                    reply_groups[parent_id].append(self._comment(reply_row))

            return {
                "video": video,
                "items": [
                    {
                        "comment": comment,
                        "replies_preview": reply_groups[comment.youtube_comment_id],
                        "stored_reply_count": reply_counts[comment.youtube_comment_id],
                    }
                    for comment in page
                ],
                "next_cursor": encode_comment_thread_cursor(page[-1], sort) if has_more and page else None,
            }

    def get_comment_replies(
        self, comment_id: str, *, owner_id: str | None = None, cursor: str | None = None, limit: int = 20
    ) -> dict[str, Any]:
        cursor_key = decode_comment_cursor(cursor)
        with self._connection() as connection, connection.cursor() as db_cursor:
            db_cursor.execute(
                """
                SELECT cm.youtube_comment_id, cm.youtube_parent_comment_id, cm.video_id
                FROM comments cm
                JOIN videos v ON v.id = cm.video_id
                WHERE cm.youtube_comment_id = %s
                  AND (
                    %s::uuid IS NULL
                    OR EXISTS (
                      SELECT 1
                      FROM collection_target_videos membership
                      JOIN collection_subscriptions subscription ON subscription.target_id = membership.target_id
                      WHERE membership.video_id = v.id AND subscription.user_id = %s::uuid
                    )
                  )
                """,
                (comment_id, owner_id, owner_id),
            )
            comment_row = db_cursor.fetchone()
            if not comment_row:
                raise NotFoundError(f"Comment '{comment_id}' was not found")
            root_comment_id = comment_row.get("youtube_parent_comment_id") or comment_row["youtube_comment_id"]

            params: list[Any] = [root_comment_id]
            cursor_filter = ""
            if cursor_key:
                cursor_filter = """
                  AND (COALESCE(cm.published_at, cm.source_fetched_at, 'epoch'::timestamptz), cm.youtube_comment_id)
                      > (%s::timestamptz, %s)
                """
                params.extend(cursor_key)
            params.append(limit + 1)
            db_cursor.execute(
                f"""
                SELECT cm.id::text, cm.youtube_comment_id, v.youtube_video_id, cm.youtube_parent_comment_id,
                       cm.youtube_thread_id, cm.author_channel_id, cm.author_display_name, cm.text_display,
                       cm.like_count, cm.published_at, cm.updated_at, cm.source_fetched_at
                FROM comments cm
                JOIN videos v ON v.id = cm.video_id
                WHERE cm.youtube_parent_comment_id = %s
                {cursor_filter}
                ORDER BY COALESCE(cm.published_at, cm.source_fetched_at, 'epoch'::timestamptz) ASC,
                         cm.youtube_comment_id ASC
                LIMIT %s
                """,
                tuple(params),
            )
            page = [self._comment(row) for row in db_cursor.fetchall()]
            has_more = len(page) > limit
            page = page[:limit]
            return {
                "comments": page,
                "next_cursor": encode_comment_cursor(page[-1]) if has_more and page else None,
            }

    def get_comment_detail(self, comment_id: str, *, owner_id: str | None = None) -> dict[str, Any]:
        with self._connection() as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT cm.id::text, cm.youtube_comment_id, cm.youtube_parent_comment_id, cm.youtube_thread_id,
                       cm.author_channel_id, cm.author_display_name, cm.text_display, cm.like_count,
                       cm.published_at, cm.updated_at, cm.source_fetched_at,
                       v.id::text AS video_db_id, v.youtube_video_id, c.youtube_channel_id, c.title AS channel_title, v.title, v.description,
                       v.published_at AS video_published_at, v.duration_seconds, v.privacy_status, v.made_for_kids,
                       v.source_fetched_at AS video_fetched_at,
                       jsonb_build_object('viewCount', COALESCE(stats.view_count, 0), 'likeCount', COALESCE(stats.like_count, 0), 'commentCount', COALESCE(stats.comment_count, 0)) AS statistics
                FROM comments cm
                JOIN videos v ON v.id = cm.video_id
                LEFT JOIN channels c ON c.id = v.channel_id
                LEFT JOIN LATERAL (SELECT view_count, like_count, comment_count FROM video_stat_snapshots WHERE video_id = v.id ORDER BY fetched_at DESC LIMIT 1) stats ON TRUE
                WHERE cm.youtube_comment_id = %s
                  AND (
                    %s::uuid IS NULL
                    OR EXISTS (
                      SELECT 1
                      FROM collection_target_videos membership
                      JOIN collection_subscriptions subscription ON subscription.target_id = membership.target_id
                      WHERE membership.video_id = v.id AND subscription.user_id = %s::uuid
                    )
                  )
                """, (comment_id, owner_id, owner_id),
            )
            row = cursor.fetchone()
            if not row:
                raise NotFoundError(f"Comment '{comment_id}' was not found")
            comment = self._comment(row)
            video = self._video({**row, "id": row["video_db_id"], "published_at": row.get("video_published_at"), "source_fetched_at": row.get("video_fetched_at")})
            parent_comment: CommentRecord | None = None
            root_comment_id = comment.youtube_comment_id
            if comment.youtube_parent_comment_id:
                cursor.execute(
                    """
                    SELECT parent.id::text, parent.youtube_comment_id, v.youtube_video_id,
                           parent.youtube_parent_comment_id, parent.youtube_thread_id,
                           parent.author_channel_id, parent.author_display_name, parent.text_display,
                           parent.like_count, parent.published_at, parent.updated_at, parent.source_fetched_at
                    FROM comments parent
                    JOIN videos v ON v.id = parent.video_id
                    WHERE parent.youtube_comment_id = %s
                    """,
                    (comment.youtube_parent_comment_id,),
                )
                parent_row = cursor.fetchone()
                if parent_row:
                    parent_comment = self._comment(parent_row)
                    root_comment_id = parent_comment.youtube_comment_id
            # ``youtube_parent_comment_id`` is the canonical YouTube relation
            # for a reply.  Use it instead of relying only on the optional
            # internal ``parent_id`` link so replies stored before their parent
            # are still available in the detail view.
            cursor.execute(
                """
                WITH ranked_replies AS (
                  SELECT cm.id::text, cm.youtube_comment_id, v.youtube_video_id, cm.youtube_parent_comment_id,
                         cm.youtube_thread_id, cm.author_channel_id, cm.author_display_name, cm.text_display,
                         cm.like_count, cm.published_at, cm.updated_at, cm.source_fetched_at,
                         COUNT(*) OVER () AS stored_reply_count,
                         ROW_NUMBER() OVER (
                           ORDER BY COALESCE(cm.published_at, cm.source_fetched_at, 'epoch'::timestamptz) ASC,
                                    cm.youtube_comment_id ASC
                         ) AS reply_rank
                  FROM comments cm
                  JOIN videos v ON v.id = cm.video_id
                  WHERE cm.youtube_parent_comment_id = %s
                    AND (
                      %s::uuid IS NULL
                      OR EXISTS (
                        SELECT 1
                        FROM collection_target_videos membership
                        JOIN collection_subscriptions subscription ON subscription.target_id = membership.target_id
                        WHERE membership.video_id = v.id AND subscription.user_id = %s::uuid
                      )
                    )
                )
                SELECT * FROM ranked_replies
                WHERE reply_rank <= 2 OR youtube_comment_id = %s
                ORDER BY reply_rank
                """,
                (root_comment_id, owner_id, owner_id, comment.youtube_comment_id),
            )
            reply_rows = cursor.fetchall()
            replies = [self._comment(reply) for reply in reply_rows]
            stored_reply_count = int(reply_rows[0]["stored_reply_count"]) if reply_rows else 0
            author_comments: list[dict[str, Any]] = []
            if comment.author_channel_id:
                cursor.execute(
                    """
                    SELECT cm.id::text, cm.youtube_comment_id, cm.youtube_parent_comment_id, cm.youtube_thread_id,
                           cm.author_channel_id, cm.author_display_name, cm.text_display, cm.like_count,
                           cm.published_at, cm.updated_at, cm.source_fetched_at,
                           v.id::text AS video_db_id, v.youtube_video_id, c.youtube_channel_id, c.title AS channel_title, v.title, v.description,
                           v.published_at AS video_published_at, v.duration_seconds, v.privacy_status, v.made_for_kids,
                           v.source_fetched_at AS video_fetched_at,
                           jsonb_build_object('viewCount', COALESCE(stats.view_count, 0), 'likeCount', COALESCE(stats.like_count, 0), 'commentCount', COALESCE(stats.comment_count, 0)) AS statistics
                    FROM comments cm
                    JOIN videos v ON v.id = cm.video_id
                    LEFT JOIN channels c ON c.id = v.channel_id
                    LEFT JOIN LATERAL (SELECT view_count, like_count, comment_count FROM video_stat_snapshots WHERE video_id = v.id ORDER BY fetched_at DESC LIMIT 1) stats ON TRUE
                    WHERE cm.author_channel_id = %s
                      AND cm.youtube_comment_id <> %s
                      -- Direct replies are already rendered in the dedicated
                      -- reply section above; do not list a self-reply twice.
                      AND cm.youtube_parent_comment_id IS DISTINCT FROM %s
                      AND (
                        %s::uuid IS NULL
                        OR EXISTS (
                          SELECT 1
                          FROM collection_target_videos membership
                          JOIN collection_subscriptions subscription ON subscription.target_id = membership.target_id
                          WHERE membership.video_id = v.id AND subscription.user_id = %s::uuid
                        )
                      )
                    ORDER BY cm.published_at DESC NULLS LAST, cm.source_fetched_at DESC
                    LIMIT 50
                    """, (comment.author_channel_id, comment_id, root_comment_id, owner_id, owner_id),
                )
                for related in cursor.fetchall():
                    author_comments.append({
                        "comment": self._comment(related),
                        "video": self._video({**related, "id": related["video_db_id"], "published_at": related.get("video_published_at"), "source_fetched_at": related.get("video_fetched_at")}),
                        "channel_title": related.get("channel_title"),
                    })
            return {
                "comment": comment,
                "video": video,
                "parent_comment": parent_comment,
                "replies": replies,
                "stored_reply_count": stored_reply_count,
                "author_comments": author_comments,
            }
