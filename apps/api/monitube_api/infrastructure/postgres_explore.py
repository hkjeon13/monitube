"""PostgreSQL pins, explore gallery, history, and unified search."""

from typing import Any

from ..domain import utcnow
from ..fuzzy_search import normalize_search_text, rank_text_fields
from ..repositories import (
    NotFoundError,
    decode_explore_video_cursor,
    encode_explore_video_cursor,
    explore_video_filter_hash,
)


class PostgresExploreMixin:
    def set_target_pin(self, *, target_id: str, enabled: bool, interval_minutes: int) -> dict[str, Any]:
        with self._connection() as connection, connection.cursor() as cursor:
            cursor.execute("SELECT id FROM collection_targets WHERE id = %s FOR UPDATE", (target_id,))
            if not cursor.fetchone():
                raise NotFoundError(f"Collection target '{target_id}' was not found")
            cursor.execute(
                """
                INSERT INTO collection_target_pins (target_id, enabled, interval_minutes, next_run_at)
                VALUES (%s, %s, %s, CASE WHEN %s THEN now() ELSE now() END)
                ON CONFLICT (target_id) DO UPDATE
                SET enabled = EXCLUDED.enabled, interval_minutes = EXCLUDED.interval_minutes,
                    next_run_at = CASE WHEN EXCLUDED.enabled THEN now() ELSE collection_target_pins.next_run_at END,
                    updated_at = now()
                RETURNING target_id::text, enabled, interval_minutes, next_run_at, last_dispatched_at
                """,
                (target_id, enabled, interval_minutes, enabled),
            )
            return dict(cursor.fetchone())

    def get_target_pin(self, *, target_id: str) -> dict[str, Any] | None:
        with self._connection() as connection, connection.cursor() as cursor:
            cursor.execute(
                "SELECT target_id::text, enabled, interval_minutes, next_run_at, last_dispatched_at FROM collection_target_pins WHERE target_id = %s",
                (target_id,),
            )
            row = cursor.fetchone()
            return dict(row) if row else None

    def dispatch_due_pins(self, *, runtime_config_id: str | None = None, limit: int = 10) -> int:
        with self._connection() as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT target_id::text, interval_minutes
                FROM collection_target_pins
                WHERE enabled = TRUE
                  AND next_run_at <= now()
                  AND EXISTS (
                    SELECT 1 FROM collection_subscriptions subscription
                    WHERE subscription.target_id = collection_target_pins.target_id
                      AND subscription.enabled = TRUE
                  )
                ORDER BY next_run_at FOR UPDATE SKIP LOCKED LIMIT %s
                """,
                (limit,),
            )
            due = cursor.fetchall()
            dispatched = 0
            for pin in due:
                target_id = str(pin["target_id"])
                cursor.execute(
                    "SELECT 1 FROM sync_jobs WHERE target_id = %s AND state IN ('queued', 'running', 'waiting_retry', 'waiting_quota') LIMIT 1",
                    (target_id,),
                )
                if not cursor.fetchone():
                    source = self._target_source(cursor, target_id)
                    if source:
                        self._create_target_job(cursor, target_id=target_id, source=source, runtime_config_id=runtime_config_id)
                        cursor.execute(
                            "UPDATE collection_target_pins SET last_dispatched_at = now(), next_run_at = now() + (interval_minutes * interval '1 minute'), updated_at = now() WHERE target_id = %s",
                            (target_id,),
                        )
                        dispatched += 1
                        continue
                cursor.execute(
                    "UPDATE collection_target_pins SET next_run_at = now() + (interval_minutes * interval '1 minute'), updated_at = now() WHERE target_id = %s",
                    (target_id,),
                )
            return dispatched

    def list_explore_channels(self, *, owner_id: str | None = None) -> list[dict[str, Any]]:
        """Aggregate each channel once over the caller's distinct visible videos."""

        if owner_id is None:
            visible_cte = """
                SELECT video.id AS video_id, video.channel_id, video.source_fetched_at
                FROM videos video
                WHERE video.channel_id IS NOT NULL
            """
            visible_params: tuple[Any, ...] = ()
        else:
            visible_cte = """
                SELECT membership.video_id, video.channel_id,
                       max(video.source_fetched_at) AS source_fetched_at
                FROM collection_target_videos membership
                JOIN collection_subscriptions subscription
                  ON subscription.target_id = membership.target_id
                 AND subscription.user_id = %s::uuid
                JOIN videos video ON video.id = membership.video_id
                WHERE video.channel_id IS NOT NULL
                GROUP BY membership.video_id, video.channel_id
            """
            visible_params = (owner_id,)

        if self.enable_explore_rollup and self.enable_comment_rollup_read:
            comment_cte = """
                SELECT visible.channel_id,
                       COALESCE(sum(rollup.stored_count), 0)::bigint AS comment_count
                FROM visible_video visible
                LEFT JOIN video_comment_rollups rollup ON rollup.video_id = visible.video_id
                GROUP BY visible.channel_id
            """
        else:
            comment_cte = """
                SELECT visible.channel_id, count(comment.id)::bigint AS comment_count
                FROM visible_video visible
                LEFT JOIN comments comment ON comment.video_id = visible.video_id
                GROUP BY visible.channel_id
            """

        with self._connection() as connection, connection.cursor() as cursor:
            cursor.execute(
                f"""
                WITH visible_video AS ({visible_cte}),
                video_aggregate AS (
                  SELECT channel_id, count(*)::bigint AS video_count,
                         max(source_fetched_at) AS last_fetched_at
                  FROM visible_video GROUP BY channel_id
                ),
                comment_aggregate AS ({comment_cte}),
                latest_video_stats AS (
                  SELECT DISTINCT ON (snapshot.video_id)
                         snapshot.video_id, snapshot.comment_count
                  FROM video_stat_snapshots snapshot
                  JOIN visible_video visible ON visible.video_id = snapshot.video_id
                  ORDER BY snapshot.video_id, snapshot.fetched_at DESC
                ),
                youtube_comment_aggregate AS (
                  SELECT visible.channel_id,
                         COALESCE(sum(COALESCE(stats.comment_count, 0)), 0)::bigint AS youtube_comment_count
                  FROM visible_video visible
                  LEFT JOIN latest_video_stats stats ON stats.video_id = visible.video_id
                  GROUP BY visible.channel_id
                )
                SELECT channel.youtube_channel_id, channel.handle, channel.title,
                       channel.description, channel.thumbnail_url,
                       video_aggregate.video_count,
                       COALESCE(comment_aggregate.comment_count, 0) AS comment_count,
                       COALESCE(youtube_comments.youtube_comment_count, 0) AS youtube_comment_count,
                       channel_stats.subscriber_count, channel_stats.view_count,
                       channel_stats.video_count AS youtube_video_count,
                       channel_stats.hidden_subscriber_count,
                       GREATEST(channel.source_fetched_at, video_aggregate.last_fetched_at) AS last_fetched_at,
                       target.id::text AS target_id,
                       pin.enabled AS pin_enabled, pin.interval_minutes AS pin_interval_minutes,
                       pin.next_run_at AS pin_next_run_at,
                       pin.last_dispatched_at AS pin_last_dispatched_at
                FROM video_aggregate
                JOIN channels channel ON channel.id = video_aggregate.channel_id
                LEFT JOIN comment_aggregate ON comment_aggregate.channel_id = channel.id
                LEFT JOIN youtube_comment_aggregate youtube_comments ON youtube_comments.channel_id = channel.id
                LEFT JOIN LATERAL (
                  SELECT subscriber_count, view_count, video_count, hidden_subscriber_count
                  FROM channel_snapshots snapshot
                  WHERE snapshot.channel_id = channel.id
                    AND (subscriber_count IS NOT NULL OR view_count IS NOT NULL
                         OR video_count IS NOT NULL OR hidden_subscriber_count IS NOT NULL)
                  ORDER BY fetched_at DESC LIMIT 1
                ) channel_stats ON TRUE
                LEFT JOIN LATERAL (
                  SELECT candidate.id
                  FROM collection_targets candidate
                  WHERE candidate.resolved_channel_id = channel.id
                    AND (
                      %s::uuid IS NULL OR EXISTS (
                        SELECT 1 FROM collection_subscriptions subscription
                        WHERE subscription.target_id = candidate.id
                          AND subscription.user_id = %s::uuid
                      )
                    )
                  ORDER BY candidate.updated_at DESC, candidate.id
                  LIMIT 1
                ) target ON TRUE
                LEFT JOIN collection_target_pins pin ON pin.target_id = target.id
                ORDER BY GREATEST(channel.source_fetched_at, video_aggregate.last_fetched_at) DESC NULLS LAST,
                         channel.title, channel.youtube_channel_id
                """,
                (*visible_params, owner_id, owner_id),
            )
            channels: list[dict[str, Any]] = []
            for row in cursor.fetchall():
                pin = None
                if row.get("pin_enabled") is not None:
                    pin = {
                        "target_id": str(row["target_id"]),
                        "enabled": bool(row["pin_enabled"]),
                        "interval_minutes": int(row["pin_interval_minutes"]),
                        "next_run_at": row["pin_next_run_at"],
                        "last_dispatched_at": row["pin_last_dispatched_at"],
                    }
                video_count = int(row["video_count"] or 0)
                youtube_video_count = int(row["youtube_video_count"] or 0)
                comment_count = int(row["comment_count"] or 0)
                youtube_comment_count = int(row["youtube_comment_count"] or 0)
                channels.append(
                    {
                        "youtubeChannelId": row["youtube_channel_id"],
                        "handle": row["handle"],
                        "title": row["title"],
                        "description": row["description"],
                        "thumbnailUrl": row["thumbnail_url"],
                        "subscriberCount": row["subscriber_count"],
                        "viewCount": row["view_count"],
                        "youtubeVideoCount": row["youtube_video_count"],
                        "hiddenSubscriberCount": row["hidden_subscriber_count"],
                        "videoCount": video_count,
                        "commentCount": comment_count,
                        "youtubeCommentCount": youtube_comment_count,
                        "videoCollectionRate": (
                            min(100, round((video_count / youtube_video_count) * 100))
                            if youtube_video_count else 0
                        ),
                        "commentCollectionRate": (
                            min(100, round((comment_count / youtube_comment_count) * 100))
                            if youtube_comment_count else 0
                        ),
                        "lastFetchedAt": row["last_fetched_at"],
                        "targetId": str(row["target_id"]) if row.get("target_id") else None,
                        "pin": pin,
                    }
                )
            return channels

    def list_explore_videos_page(
        self,
        *,
        owner_id: str | None = None,
        channel_id: str | None = None,
        cursor: str | None = None,
        limit: int = 60,
    ) -> dict[str, Any]:
        scope = f"owner:{owner_id}" if owner_id is not None else "public"
        filter_hash = explore_video_filter_hash(channel_id)
        decoded = decode_explore_video_cursor(
            cursor, scope=scope, filter_hash=filter_hash
        )
        snapshot_at = decoded.snapshot_at if decoded else utcnow()

        if owner_id is None:
            visible_cte = """
                SELECT video.id AS video_id,
                       COALESCE(video.source_fetched_at, 'epoch'::timestamptz) AS first_seen_at
                FROM videos video
            """
            visible_params: list[Any] = []
        else:
            visible_cte = """
                SELECT membership.video_id,
                       min(GREATEST(membership.first_seen_at, subscription.created_at)) AS first_seen_at
                FROM collection_target_videos membership
                JOIN collection_subscriptions subscription
                  ON subscription.target_id = membership.target_id
                 AND subscription.user_id = %s::uuid
                GROUP BY membership.video_id
            """
            visible_params = [owner_id]

        channel_filter = ""
        filter_params: list[Any] = []
        if channel_id:
            channel_filter = "AND channel.youtube_channel_id = %s"
            filter_params.append(channel_id)
        cursor_filter = ""
        cursor_params: list[Any] = []
        if decoded:
            cursor_filter = """
              AND (
                COALESCE(video.published_at, video.source_fetched_at, 'epoch'::timestamptz),
                COALESCE(video.source_fetched_at, 'epoch'::timestamptz),
                video.youtube_video_id
              ) < (%s, %s, %s)
            """
            cursor_params.extend(
                [decoded.effective_at, decoded.fetched_at, decoded.youtube_video_id]
            )

        with self._connection() as connection, connection.cursor() as db_cursor:
            db_cursor.execute(
                f"""
                WITH visible_video AS ({visible_cte})
                SELECT count(*)::bigint AS total
                FROM visible_video visible
                JOIN videos video ON video.id = visible.video_id
                LEFT JOIN channels channel ON channel.id = video.channel_id
                WHERE visible.first_seen_at <= %s
                {channel_filter}
                """,
                (*visible_params, snapshot_at, *filter_params),
            )
            total = int(db_cursor.fetchone()["total"])
            db_cursor.execute(
                f"""
                WITH visible_video AS ({visible_cte})
                SELECT video.id::text, video.youtube_video_id, channel.youtube_channel_id,
                       video.title, video.description, video.published_at,
                       video.duration_seconds, video.privacy_status, video.made_for_kids,
                       COALESCE(video.source_fetched_at, 'epoch'::timestamptz) AS source_fetched_at,
                       jsonb_build_object(
                         'viewCount', COALESCE(stats.view_count, 0),
                         'likeCount', COALESCE(stats.like_count, 0),
                         'commentCount', COALESCE(stats.comment_count, 0)
                       ) AS statistics
                FROM visible_video visible
                JOIN videos video ON video.id = visible.video_id
                LEFT JOIN channels channel ON channel.id = video.channel_id
                LEFT JOIN LATERAL (
                  SELECT view_count, like_count, comment_count
                  FROM video_stat_snapshots snapshot
                  WHERE snapshot.video_id = video.id
                  ORDER BY snapshot.fetched_at DESC LIMIT 1
                ) stats ON TRUE
                WHERE visible.first_seen_at <= %s
                {channel_filter}
                {cursor_filter}
                ORDER BY COALESCE(video.published_at, video.source_fetched_at, 'epoch'::timestamptz) DESC,
                         COALESCE(video.source_fetched_at, 'epoch'::timestamptz) DESC,
                         video.youtube_video_id DESC
                LIMIT %s
                """,
                (
                    *visible_params,
                    snapshot_at,
                    *filter_params,
                    *cursor_params,
                    limit + 1,
                ),
            )
            candidates = [self._video(row) for row in db_cursor.fetchall()]
            has_more = len(candidates) > limit
            videos = candidates[:limit]
            next_cursor = (
                encode_explore_video_cursor(
                    videos[-1],
                    snapshot_at=snapshot_at,
                    scope=scope,
                    filter_hash=filter_hash,
                )
                if has_more and videos else None
            )
            return {
                "videos": videos,
                "next_cursor": next_cursor,
                "snapshot_at": snapshot_at,
                "total": total,
            }

    def list_explore(
        self, *, limit: int = 60, offset: int = 0, channel_id: str | None = None, owner_id: str | None = None
    ) -> dict[str, Any]:
        # Compatibility wrapper: keep the old offset contract while reusing the
        # set-based channel aggregate. New clients use the keyset endpoint.
        channels = self.list_explore_channels(owner_id=owner_id)
        with self._connection() as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                WITH visible_video AS (
                  SELECT DISTINCT video.id
                  FROM videos video
                  LEFT JOIN collection_target_videos membership ON membership.video_id = video.id
                  LEFT JOIN collection_subscriptions subscription
                    ON subscription.target_id = membership.target_id
                   AND subscription.user_id = %s::uuid
                  WHERE %s::uuid IS NULL OR subscription.id IS NOT NULL
                )
                SELECT video.id::text, video.youtube_video_id, channel.youtube_channel_id,
                       video.title, video.description, video.published_at,
                       video.duration_seconds, video.privacy_status, video.made_for_kids,
                       COALESCE(video.source_fetched_at, 'epoch'::timestamptz) AS source_fetched_at,
                       jsonb_build_object(
                         'viewCount', COALESCE(stats.view_count, 0),
                         'likeCount', COALESCE(stats.like_count, 0),
                         'commentCount', COALESCE(stats.comment_count, 0)
                       ) AS statistics
                FROM visible_video visible
                JOIN videos video ON video.id = visible.id
                LEFT JOIN channels channel ON channel.id = video.channel_id
                LEFT JOIN LATERAL (
                  SELECT view_count, like_count, comment_count
                  FROM video_stat_snapshots snapshot
                  WHERE snapshot.video_id = video.id
                  ORDER BY snapshot.fetched_at DESC LIMIT 1
                ) stats ON TRUE
                WHERE (%s::text IS NULL OR channel.youtube_channel_id = %s)
                ORDER BY COALESCE(video.published_at, video.source_fetched_at, 'epoch'::timestamptz) DESC,
                         video.source_fetched_at DESC, video.youtube_video_id DESC
                LIMIT %s OFFSET %s
                """,
                (owner_id, owner_id, channel_id, channel_id, limit + 1, offset),
            )
            rows = cursor.fetchall()
            page_rows = rows[:limit]
            return {
                "channels": channels,
                "videos": [self._video(row) for row in page_rows],
                "next_offset": offset + len(page_rows) if len(rows) > limit else None,
            }

    def list_channel_subscriber_history(
        self, *, youtube_channel_id: str, limit: int = 180, owner_id: str | None = None
    ) -> list[dict[str, Any]]:
        with self._connection() as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT snapshot.fetched_at, snapshot.subscriber_count, snapshot.hidden_subscriber_count
                FROM channel_snapshots snapshot
                JOIN channels channel ON channel.id = snapshot.channel_id
                WHERE channel.youtube_channel_id = %s
                  AND (snapshot.subscriber_count IS NOT NULL OR snapshot.hidden_subscriber_count IS NOT NULL)
                  AND (
                    %s::uuid IS NULL
                    OR EXISTS (
                      SELECT 1
                      FROM videos v
                      JOIN collection_target_videos membership ON membership.video_id = v.id
                      JOIN collection_subscriptions subscription ON subscription.target_id = membership.target_id
                      WHERE v.channel_id = channel.id AND subscription.user_id = %s::uuid
                    )
                  )
                ORDER BY snapshot.fetched_at DESC
                LIMIT %s
                """,
                (youtube_channel_id, owner_id, owner_id, limit),
            )
            return [
                {"fetchedAt": row["fetched_at"], "subscriberCount": row["subscriber_count"], "hiddenSubscriberCount": row["hidden_subscriber_count"]}
                for row in reversed(cursor.fetchall())
            ]

    def search_collected(self, *, query: str, limit: int = 20, owner_id: str | None = None, scope: str = "all") -> dict[str, Any]:
        """Search persisted public data with a Jaro-Winkler tolerance layer.

        Search scoring deliberately runs in the application so results are
        consistent for Korean text as well as Latin scripts, without depending on
        a database-specific fuzzy-search extension.
        """

        terms = query.split()

        def escaped_pattern(value: str) -> str:
            return "%" + value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"

        exact_patterns = [escaped_pattern(term.casefold()) for term in terms]
        normalized_query = normalize_search_text(query)
        short_query = len(normalized_query) == 2
        prefix_pattern = (
            normalized_query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
        )
        # ACL is applied inside each candidate query, before this hard cap.
        candidate_limit = min(300, max(100, limit * 10))

        # Scalar LIKE clauses are intentional. PostgreSQL cannot turn
        # ``ILIKE ALL(array_parameter)`` into pg_trgm index conditions, which
        # made a common term scan every visible video's comments. A fixed SQL
        # fragment per term remains injection-safe because only the values are
        # parameters, and is logically equivalent to ILIKE ALL.
        video_indexed_contains = " AND ".join(
            "search_document.document ILIKE %s" for _ in exact_patterns
        ) or "TRUE"
        comment_indexed_contains = " AND ".join(
            "lower(COALESCE(cm.text_display, '')) ILIKE %s" for _ in exact_patterns
        ) or "TRUE"

        with self._connection() as connection, connection.cursor() as cursor:
            video_results: list[dict[str, Any]] = []
            if scope in {"all", "videos"}:
                if short_query:
                    video_cte = ""
                    video_join = ""
                    video_acl = """
                      (%s::uuid IS NULL
                       OR EXISTS (
                         SELECT 1 FROM collection_target_videos membership
                         JOIN collection_subscriptions subscription
                           ON subscription.target_id = membership.target_id
                         WHERE membership.video_id = v.id
                           AND subscription.user_id = %s::uuid
                       ))
                    """
                    video_match = """
                      (v.youtube_video_id ILIKE %s ESCAPE '\\'
                       OR ltrim(COALESCE(c.handle, ''), '@') ILIKE %s ESCAPE '\\'
                       OR COALESCE(v.title, '') ILIKE %s ESCAPE '\\')
                    """
                    video_order = "v.source_fetched_at DESC NULLS LAST"
                    video_limit = "LIMIT %s"
                    video_params: tuple[Any, ...] = (
                        owner_id, owner_id, prefix_pattern, prefix_pattern, prefix_pattern, candidate_limit
                    )
                elif self.enable_search_trigram:
                    # MATERIALIZED is an optimization barrier: build the
                    # pg_trgm bitmap candidates first, then apply ACL and LIMIT.
                    # There must be no LIMIT in this CTE because that could
                    # discard authorized rows behind unauthorized candidates.
                    video_cte = f"""
                    WITH matched_videos AS MATERIALIZED (
                      SELECT search_document.video_id,
                             similarity(search_document.document, %s) AS search_score
                      FROM video_search_documents search_document
                      WHERE (({video_indexed_contains})
                             OR search_document.document %% %s)
                    ),
                    authorized_videos AS MATERIALIZED (
                      SELECT search_candidate.video_id, search_candidate.search_score,
                             v.source_fetched_at
                      FROM matched_videos search_candidate
                      JOIN videos v ON v.id = search_candidate.video_id
                      WHERE (
                        %s::uuid IS NULL
                        OR EXISTS (
                          SELECT 1 FROM collection_target_videos membership
                          JOIN collection_subscriptions subscription
                            ON subscription.target_id = membership.target_id
                          WHERE membership.video_id = search_candidate.video_id
                            AND subscription.user_id = %s::uuid
                        )
                      )
                      ORDER BY search_candidate.search_score DESC,
                               v.source_fetched_at DESC NULLS LAST
                      LIMIT %s
                    )
                    """
                    video_join = "JOIN authorized_videos search_candidate ON search_candidate.video_id = v.id"
                    video_acl = "TRUE"
                    video_match = "TRUE"
                    video_order = "search_candidate.search_score DESC, search_candidate.source_fetched_at DESC NULLS LAST"
                    video_limit = ""
                    video_params = (
                        normalized_query, *exact_patterns, normalized_query,
                        owner_id, owner_id, candidate_limit,
                    )
                else:
                    video_cte = ""
                    video_join = ""
                    video_acl = """
                      (%s::uuid IS NULL
                       OR EXISTS (
                         SELECT 1 FROM collection_target_videos membership
                         JOIN collection_subscriptions subscription
                           ON subscription.target_id = membership.target_id
                         WHERE membership.video_id = v.id
                           AND subscription.user_id = %s::uuid
                       ))
                    """
                    video_match = "lower(concat_ws(' ', v.title, v.description, c.title, c.handle)) ILIKE ALL(%s)"
                    video_order = "v.source_fetched_at DESC NULLS LAST"
                    video_limit = "LIMIT %s"
                    video_params = (owner_id, owner_id, exact_patterns, candidate_limit)
                cursor.execute(
                f"""
                {video_cte}
                SELECT v.id::text, v.youtube_video_id, c.youtube_channel_id, c.title AS channel_title,
                       c.handle AS channel_handle, v.title, v.description, v.published_at,
                       v.duration_seconds, v.privacy_status, v.made_for_kids, v.source_fetched_at,
                       jsonb_build_object('viewCount', COALESCE(stats.view_count, 0), 'likeCount', COALESCE(stats.like_count, 0), 'commentCount', COALESCE(stats.comment_count, 0)) AS statistics
                FROM videos v
                LEFT JOIN channels c ON c.id = v.channel_id
                {video_join}
                LEFT JOIN LATERAL (
                  SELECT view_count, like_count, comment_count FROM video_stat_snapshots
                  WHERE video_id = v.id ORDER BY fetched_at DESC LIMIT 1
                ) stats ON TRUE
                WHERE {video_acl}
                  AND {video_match}
                ORDER BY {video_order}
                {video_limit}
                """,
                video_params,
                )
                for row in cursor.fetchall():
                    if short_query:
                        short_fields = {
                            "id": row.get("youtube_video_id"),
                            "title": row.get("title"),
                            "handle": row.get("channel_handle"),
                        }
                        matched_fields = [
                            name
                            for name, value in short_fields.items()
                            if value
                            and normalize_search_text(str(value)).startswith(normalized_query)
                        ]
                        score = 1.0 if matched_fields else 0.0
                    else:
                        score, matched_fields = rank_text_fields(query, {
                            "id": row.get("youtube_video_id"),
                            "title": row.get("title"), "description": row.get("description"),
                            "channel": row.get("channel_title"), "handle": row.get("channel_handle"),
                        })
                    if matched_fields:
                        video_results.append({"video": self._video(row), "score": score, "matched_fields": matched_fields})

            comment_results: list[dict[str, Any]] = []
            if scope in {"all", "comments"} and not short_query:
                if self.enable_search_trigram:
                    # Keep every search match available until after ACL. This
                    # forces the GIN BitmapOr path without changing per-owner
                    # visibility or the existing ranking/candidate semantics.
                    # Only narrow identifiers/ranking columns cross the first
                    # barrier so a broad term cannot spill full comment rows.
                    comment_cte = f"""
                    WITH matched_comments AS MATERIALIZED (
                      SELECT cm.id AS comment_id, cm.video_id, cm.source_fetched_at,
                             similarity(lower(COALESCE(cm.text_display, '')), %s) AS search_score
                      FROM comments cm
                      WHERE cm.text_display IS NOT NULL
                        AND (({comment_indexed_contains})
                             OR lower(COALESCE(cm.text_display, '')) %% %s)
                    ),
                    authorized_comments AS MATERIALIZED (
                      SELECT search_candidate.comment_id, search_candidate.video_id,
                             search_candidate.source_fetched_at, search_candidate.search_score
                      FROM matched_comments search_candidate
                      WHERE (
                        %s::uuid IS NULL
                        OR EXISTS (
                          SELECT 1 FROM collection_target_videos membership
                          JOIN collection_subscriptions subscription
                            ON subscription.target_id = membership.target_id
                          WHERE membership.video_id = search_candidate.video_id
                            AND subscription.user_id = %s::uuid
                        )
                      )
                      ORDER BY search_candidate.search_score DESC,
                               search_candidate.source_fetched_at DESC NULLS LAST
                      LIMIT %s
                    )
                    """
                    comment_from = """authorized_comments search_candidate
                    JOIN comments cm ON cm.id = search_candidate.comment_id"""
                    comment_acl = "TRUE"
                    comment_match = "TRUE"
                    comment_order = "search_candidate.search_score DESC, search_candidate.source_fetched_at DESC NULLS LAST"
                    comment_limit = ""
                    comment_params: tuple[Any, ...] = (
                        normalized_query, *exact_patterns, normalized_query,
                        owner_id, owner_id, candidate_limit,
                    )
                else:
                    comment_cte = ""
                    comment_from = "comments cm"
                    comment_acl = """
                      (%s::uuid IS NULL
                       OR EXISTS (
                         SELECT 1 FROM collection_target_videos membership
                         JOIN collection_subscriptions subscription
                           ON subscription.target_id = membership.target_id
                         WHERE membership.video_id = v.id
                           AND subscription.user_id = %s::uuid
                       ))
                    """
                    comment_match = "lower(COALESCE(cm.text_display, '')) ILIKE ALL(%s)"
                    comment_order = "cm.source_fetched_at DESC NULLS LAST"
                    comment_limit = "LIMIT %s"
                    comment_params = (owner_id, owner_id, exact_patterns, candidate_limit)
                cursor.execute(
                f"""
                {comment_cte}
                SELECT cm.id::text AS comment_id, cm.youtube_comment_id, cm.youtube_parent_comment_id,
                       cm.youtube_thread_id, cm.author_channel_id, cm.author_display_name, cm.text_display, cm.like_count, cm.published_at AS comment_published_at,
                       cm.updated_at AS comment_updated_at, cm.source_fetched_at AS comment_fetched_at,
                       v.id::text AS video_db_id, v.youtube_video_id, c.youtube_channel_id,
                       c.title AS channel_title, c.handle AS channel_handle, v.title, v.description,
                       v.published_at, v.duration_seconds, v.privacy_status, v.made_for_kids,
                       v.source_fetched_at,
                       jsonb_build_object('viewCount', COALESCE(stats.view_count, 0), 'likeCount', COALESCE(stats.like_count, 0), 'commentCount', COALESCE(stats.comment_count, 0)) AS statistics
                FROM {comment_from}
                JOIN videos v ON v.id = cm.video_id
                LEFT JOIN channels c ON c.id = v.channel_id
                LEFT JOIN LATERAL (
                  SELECT view_count, like_count, comment_count FROM video_stat_snapshots
                  WHERE video_id = v.id ORDER BY fetched_at DESC LIMIT 1
                ) stats ON TRUE
                WHERE {comment_acl}
                  AND cm.text_display IS NOT NULL
                  AND {comment_match}
                ORDER BY {comment_order}
                {comment_limit}
                """,
                comment_params,
                )
                for row in cursor.fetchall():
                    score, matched_fields = rank_text_fields(query, {"comment": row.get("text_display")})
                    if not matched_fields:
                        continue
                    comment = self._comment({
                    "id": row["comment_id"], "youtube_comment_id": row["youtube_comment_id"],
                    "youtube_video_id": row["youtube_video_id"], "youtube_parent_comment_id": row.get("youtube_parent_comment_id"),
                    "youtube_thread_id": row.get("youtube_thread_id"), "author_channel_id": row.get("author_channel_id"),
                    "author_display_name": row.get("author_display_name"), "text_display": row.get("text_display"),
                    "like_count": row.get("like_count"), "published_at": row.get("comment_published_at"),
                    "updated_at": row.get("comment_updated_at"), "source_fetched_at": row.get("comment_fetched_at"),
                    })
                    video = self._video({
                    "id": row["video_db_id"], "youtube_video_id": row["youtube_video_id"],
                    "youtube_channel_id": row.get("youtube_channel_id"), "title": row.get("title"),
                    "description": row.get("description"), "published_at": row.get("published_at"),
                    "duration_seconds": row.get("duration_seconds"), "privacy_status": row.get("privacy_status"),
                    "made_for_kids": row.get("made_for_kids"), "source_fetched_at": row.get("source_fetched_at"),
                    "statistics": row.get("statistics"),
                    })
                    comment_results.append({
                        "comment": comment, "video": video, "channel_title": row.get("channel_title"),
                        "score": score, "matched_fields": matched_fields,
                    })

            video_results.sort(key=lambda item: (item["score"], item["video"].source_fetched_at), reverse=True)
            comment_results.sort(key=lambda item: (item["score"], item["comment"].source_fetched_at), reverse=True)
            return {"videos": video_results[:limit], "comments": comment_results[:limit]}
