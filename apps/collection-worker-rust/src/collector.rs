//! Incremental metadata persistence for direct and fanned-out video jobs.

use crate::searchapi::{SearchApiClient, SearchApiError};
use crate::youtube::{YouTubeClient, YouTubeError};
use chrono::{DateTime, Utc};
use monitube_collection_store::{ClaimedJob, CollectionSource, CollectionStore, StoreError};
use serde_json::{Value, json};
use sha2::Digest;
use sqlx::PgPool;
use thiserror::Error;
use uuid::Uuid;

struct CommentInput {
    id: String,
    parent_id: Option<String>,
    thread_id: String,
    author_channel_id: Option<String>,
    author_name: Option<String>,
    text: Option<String>,
    like_count: i64,
    published_at: Option<DateTime<Utc>>,
    updated_at: Option<DateTime<Utc>>,
}

struct TranscriptSegmentInput {
    sequence: i32,
    start_ms: i32,
    duration_ms: i32,
    text: String,
}

pub struct Collector {
    pool: PgPool,
    store: CollectionStore,
    youtube: YouTubeClient,
    config: CollectorConfig,
}

pub struct CollectorConfig {
    pub discovery_provider: String,
    pub searchapi: Option<SearchApiClient>,
    pub transcript_enabled: bool,
    pub transcript_primary_language: String,
    pub transcript_fallback_language: String,
    pub transcript_type: String,
    pub transcript_max_segments: usize,
    pub runtime_key_encryption_key: Option<String>,
}

impl Collector {
    #[must_use]
    pub fn new(pool: PgPool, youtube: YouTubeClient, config: CollectorConfig) -> Self {
        Self {
            store: CollectionStore::new(pool.clone()),
            pool,
            youtube,
            config,
        }
    }

    #[allow(clippy::too_many_lines)]
    pub async fn collect(&mut self, job: &ClaimedJob) -> Result<(), CollectorError> {
        if let Some(encryption_key) = self.config.runtime_key_encryption_key.as_deref() {
            let keys = self
                .store
                .load_runtime_keys(job.runtime_config_id, encryption_key)
                .await?;
            if !keys.is_empty() {
                self.youtube.replace_keys(keys)?;
            }
        }
        let source = self.store.source(job.source_id).await?;
        if job.checkpoint.get("jobKind").and_then(Value::as_str) == Some("comment") {
            return self.collect_comment_job(job, &source).await;
        }
        if job
            .checkpoint
            .get("fanoutDiscovered")
            .and_then(Value::as_bool)
            .unwrap_or(false)
        {
            let summary = self.store.child_summary(job.id).await?;
            if summary.terminal < summary.total {
                return Err(CollectorError::WaitingForChildren {
                    waiting_quota: summary.waiting_quota,
                });
            }
            if summary.failed > 0 {
                return Err(CollectorError::ChildJobsFailed(summary.failed));
            }
            if summary.warnings > 0 {
                self.store
                    .add_partial_error(
                        job,
                        "child_collection_warning",
                        &format!(
                            "{} child collection job(s) completed with warnings",
                            summary.warnings
                        ),
                        None,
                    )
                    .await?;
            }
            let checkpoint = self.store.current_checkpoint(job).await?;
            let total = checkpoint
                .get("fanoutVideoCount")
                .and_then(Value::as_u64)
                .and_then(|value| i32::try_from(value).ok())
                .unwrap_or(0)
                .max(0);
            self.store
                .checkpoint(job, "finalizing", &checkpoint, total, Some(total), "videos")
                .await?;
            return Ok(());
        }
        let mut ids = video_ids(job, &source)?;
        if ids.is_empty() {
            ids = self.discover(job, &source).await?;
        }
        if source.source_type != "video" && job.target_id.is_some() {
            self.store.enqueue_video_batches(job, &ids).await?;
            let mut checkpoint = self
                .store
                .current_checkpoint(job)
                .await?
                .as_object()
                .cloned()
                .unwrap_or_default();
            checkpoint.insert("fanoutDiscovered".to_owned(), Value::Bool(true));
            checkpoint.insert("fanoutVideoCount".to_owned(), Value::from(ids.len()));
            checkpoint.insert(
                "stage".to_owned(),
                Value::String("waiting_for_video_jobs".to_owned()),
            );
            checkpoint.insert("scopeKey".to_owned(), Value::String(source.id.to_string()));
            checkpoint.insert("batchCursor".to_owned(), Value::from(ids.len()));
            let checkpoint = Value::Object(checkpoint);
            self.store
                .checkpoint(
                    job,
                    "waiting_for_video_jobs",
                    &checkpoint,
                    0,
                    Some(i32::try_from(ids.len()).map_err(|_| CollectorError::TooManyVideos)?),
                    "videos",
                )
                .await?;
            return Err(CollectorError::WaitingForChildren { waiting_quota: 0 });
        }
        let total = i32::try_from(ids.len()).map_err(|_| CollectorError::TooManyVideos)?;
        let mut completed = 0_i32;
        for batch in ids.chunks(50) {
            self.store.renew(job.id, &job.lease_owner, 180).await?;
            let payload = self
                .youtube_request(
                    job,
                    "videos",
                    &[
                        (
                            "part",
                            "snippet,contentDetails,status,statistics".to_owned(),
                        ),
                        ("id", batch.join(",")),
                        ("maxResults", "50".to_owned()),
                    ],
                )
                .await?;
            self.persist_video_items(job, &payload).await?;
            if self.config.transcript_enabled {
                self.collect_transcripts(job, batch).await?;
            }
            completed = completed.saturating_add(i32::try_from(batch.len()).unwrap_or(0));
            let checkpoint = json!({
                "jobKind": job.checkpoint.get("jobKind").cloned().unwrap_or(Value::Null),
                "youtubeVideoIds": ids,
                "stage": "video_details",
                "scopeKey": job.source_id,
                "batchCursor": completed,
            });
            self.store
                .checkpoint(
                    job,
                    "collecting_videos",
                    &checkpoint,
                    completed,
                    Some(total),
                    "videos",
                )
                .await?;
        }
        let comments_requested = job.include_comments
            || source
                .config
                .get("includeComments")
                .and_then(Value::as_bool)
                .unwrap_or(false);
        let is_video_batch =
            job.checkpoint.get("jobKind").and_then(Value::as_str) == Some("video_batch");
        if comments_requested && (is_video_batch || source.source_type == "video") {
            self.store.enqueue_comment_jobs(job, &ids).await?;
            if job.parent_job_id.is_none() {
                let mut checkpoint = self
                    .store
                    .current_checkpoint(job)
                    .await?
                    .as_object()
                    .cloned()
                    .unwrap_or_default();
                checkpoint.insert("fanoutDiscovered".to_owned(), Value::Bool(true));
                checkpoint.insert("fanoutVideoCount".to_owned(), Value::from(ids.len()));
                checkpoint.insert(
                    "stage".to_owned(),
                    Value::String("waiting_for_comment_jobs".to_owned()),
                );
                self.store
                    .checkpoint(
                        job,
                        "waiting_for_comment_jobs",
                        &Value::Object(checkpoint),
                        completed,
                        Some(total),
                        "videos",
                    )
                    .await?;
                return Err(CollectorError::WaitingForChildren { waiting_quota: 0 });
            }
        }
        Ok(())
    }

    async fn collect_comment_job(
        &mut self,
        job: &ClaimedJob,
        source: &CollectionSource,
    ) -> Result<(), CollectorError> {
        let video_id = job
            .checkpoint
            .get("youtubeVideoId")
            .and_then(Value::as_str)
            .filter(|value| !value.is_empty())
            .ok_or(CollectorError::InvalidSourceConfig)?
            .to_owned();
        let collect_all = source
            .config
            .get("collectAllComments")
            .and_then(Value::as_bool)
            .unwrap_or(false);
        let maximum_pages = if collect_all {
            10_000
        } else {
            job.max_comments_per_video
                .or_else(|| {
                    source
                        .config
                        .get("maxCommentPagesPerVideo")
                        .and_then(Value::as_i64)
                        .and_then(|value| i32::try_from(value).ok())
                })
                .unwrap_or(1)
                .clamp(1, 100)
        };
        let mut page_token = job
            .checkpoint
            .get("pageToken")
            .and_then(Value::as_str)
            .map(str::to_owned);
        let mut page = job
            .checkpoint
            .get("batchCursor")
            .and_then(Value::as_i64)
            .and_then(|value| i32::try_from(value).ok())
            .unwrap_or(0)
            .max(0);
        while page < maximum_pages {
            self.store.renew(job.id, &job.lease_owner, 180).await?;
            let mut params = vec![
                ("part", "snippet,replies".to_owned()),
                ("videoId", video_id.clone()),
                ("maxResults", "100".to_owned()),
                ("textFormat", "plainText".to_owned()),
                ("order", "time".to_owned()),
            ];
            if let Some(token) = page_token.as_ref() {
                params.push(("pageToken", token.clone()));
            }
            let payload = self.youtube_request(job, "commentThreads", &params).await?;
            let comments = parse_comment_threads(&payload)?;
            self.persist_comments(&video_id, &comments).await?;
            for (thread_id, parent_id) in reply_traversals(&payload) {
                self.collect_remaining_replies(job, &video_id, &thread_id, &parent_id)
                    .await?;
            }
            page = page.saturating_add(1);
            page_token = optional_text(&payload, "nextPageToken").map(str::to_owned);
            let phase_completed = page_token.is_none() || page >= maximum_pages;
            let checkpoint = json!({
                "jobKind": "comment", "youtubeVideoId": video_id,
                "stage": "comments", "scopeKey": video_id,
                "pageToken": page_token, "batchCursor": page,
                "phaseProgress": {"comments": {
                    "completed": i32::from(phase_completed),
                    "total": 1, "unit": "comments"
                }}
            });
            self.store
                .checkpoint(
                    job,
                    "collecting_comments",
                    &checkpoint,
                    i32::from(phase_completed),
                    Some(1),
                    "comments",
                )
                .await?;
            if page_token.is_none() {
                break;
            }
        }
        if page_token.is_some() {
            self.store
                .add_partial_error(
                    job,
                    "comment_page_limit",
                    "Comment collection reached the bounded 10,000-page safety limit",
                    Some(&video_id),
                )
                .await?;
        }
        Ok(())
    }

    async fn collect_remaining_replies(
        &mut self,
        job: &ClaimedJob,
        video_id: &str,
        thread_id: &str,
        parent_id: &str,
    ) -> Result<(), CollectorError> {
        let mut page_token: Option<String> = None;
        for _ in 0..100 {
            self.store.renew(job.id, &job.lease_owner, 180).await?;
            let mut params = vec![
                ("part", "snippet".to_owned()),
                ("parentId", parent_id.to_owned()),
                ("maxResults", "100".to_owned()),
                ("textFormat", "plainText".to_owned()),
            ];
            if let Some(token) = page_token.as_ref() {
                params.push(("pageToken", token.clone()));
            }
            let payload = self.youtube_request(job, "comments", &params).await?;
            let replies = payload
                .get("items")
                .and_then(Value::as_array)
                .ok_or(CollectorError::InvalidPayload)?
                .iter()
                .map(|item| comment_input(item, thread_id, Some(parent_id)))
                .collect::<Result<Vec<_>, _>>()?;
            self.persist_comments(video_id, &replies).await?;
            page_token = optional_text(&payload, "nextPageToken").map(str::to_owned);
            if page_token.is_none() {
                return Ok(());
            }
        }
        Err(CollectorError::TooManyReplyPages)
    }

    async fn persist_comments(
        &self,
        youtube_video_id: &str,
        comments: &[CommentInput],
    ) -> Result<(), CollectorError> {
        let mut transaction = self.pool.begin().await?;
        let video_id = sqlx::query_scalar::<_, Uuid>(
            "SELECT id FROM videos WHERE youtube_video_id = $1 FOR UPDATE",
        )
        .bind(youtube_video_id)
        .fetch_optional(&mut *transaction)
        .await?
        .ok_or(CollectorError::VideoNotPersisted)?;
        for reply_pass in [false, true] {
            for comment in comments
                .iter()
                .filter(|comment| comment.parent_id.is_some() == reply_pass)
            {
                sqlx::query(
                    r"
                    INSERT INTO comments (
                      youtube_comment_id, video_id, parent_id,
                      youtube_parent_comment_id, youtube_thread_id,
                      author_channel_id, author_display_name,
                      text_display, text_original, like_count,
                      published_at, updated_at, source_fetched_at
                    ) VALUES (
                      $1, $2,
                      (SELECT id FROM comments WHERE youtube_comment_id = $3),
                      $3, $4, $5, $6, $7, $7, $8, $9, $10, now()
                    )
                    ON CONFLICT (youtube_comment_id) DO UPDATE
                    SET video_id = EXCLUDED.video_id, parent_id = EXCLUDED.parent_id,
                        youtube_parent_comment_id = EXCLUDED.youtube_parent_comment_id,
                        youtube_thread_id = EXCLUDED.youtube_thread_id,
                        author_channel_id = EXCLUDED.author_channel_id,
                        author_display_name = EXCLUDED.author_display_name,
                        text_display = EXCLUDED.text_display,
                        text_original = EXCLUDED.text_original,
                        like_count = EXCLUDED.like_count,
                        published_at = EXCLUDED.published_at,
                        updated_at = EXCLUDED.updated_at,
                        source_fetched_at = EXCLUDED.source_fetched_at
                    ",
                )
                .bind(&comment.id)
                .bind(video_id)
                .bind(comment.parent_id.as_deref())
                .bind(&comment.thread_id)
                .bind(comment.author_channel_id.as_deref())
                .bind(comment.author_name.as_deref())
                .bind(comment.text.as_deref())
                .bind(comment.like_count.max(0))
                .bind(comment.published_at)
                .bind(comment.updated_at)
                .execute(&mut *transaction)
                .await?;
            }
        }
        sqlx::query(
            r"
            INSERT INTO video_comment_rollups (
              video_id, stored_count, top_level_count, reply_count,
              latest_published_at, updated_at
            )
            SELECT $1, count(*)::bigint,
                   count(*) FILTER (WHERE youtube_parent_comment_id IS NULL)::bigint,
                   count(*) FILTER (WHERE youtube_parent_comment_id IS NOT NULL)::bigint,
                   max(COALESCE(published_at, source_fetched_at)), now()
            FROM comments WHERE video_id = $1
            ON CONFLICT (video_id) DO UPDATE
            SET stored_count = EXCLUDED.stored_count,
                top_level_count = EXCLUDED.top_level_count,
                reply_count = EXCLUDED.reply_count,
                latest_published_at = EXCLUDED.latest_published_at,
                updated_at = now()
            ",
        )
        .bind(video_id)
        .execute(&mut *transaction)
        .await?;
        transaction.commit().await?;
        Ok(())
    }

    async fn discover(
        &mut self,
        job: &ClaimedJob,
        source: &CollectionSource,
    ) -> Result<Vec<String>, CollectorError> {
        match source.source_type.as_str() {
            "channel" => {
                self.discover_channel(job, &source.config, &source.coverage)
                    .await
            }
            "keyword" => {
                self.discover_keyword(job, &source.config, &source.coverage)
                    .await
            }
            other => Err(CollectorError::UnsupportedDiscovery(other.to_owned())),
        }
    }

    #[allow(clippy::too_many_lines)]
    async fn discover_channel(
        &mut self,
        job: &ClaimedJob,
        config: &Value,
        coverage: &Value,
    ) -> Result<Vec<String>, CollectorError> {
        if self.config.discovery_provider == "searchapi" {
            return self.discover_channel_searchapi(job, config, coverage).await;
        }
        let input = required_text(config, "input")?;
        let channel_id = if input.starts_with("UC") && input.len() == 24 {
            input.to_owned()
        } else if input.starts_with('@') {
            let payload = self
                .youtube_request(
                    job,
                    "channels",
                    &[
                        ("part", "contentDetails".to_owned()),
                        ("forHandle", input.to_owned()),
                    ],
                )
                .await?;
            first_id(&payload)?
        } else {
            let payload = self
                .youtube_request(
                    job,
                    "search",
                    &[
                        ("part", "snippet".to_owned()),
                        ("type", "channel".to_owned()),
                        ("q", input.to_owned()),
                        ("maxResults", "1".to_owned()),
                    ],
                )
                .await?;
            payload
                .get("items")
                .and_then(Value::as_array)
                .and_then(|items| items.first())
                .and_then(|item| item.get("id"))
                .and_then(|id| id.get("channelId"))
                .and_then(Value::as_str)
                .map(str::to_owned)
                .ok_or(CollectorError::InvalidPayload)?
        };
        let channel = self
            .youtube_request(
                job,
                "channels",
                &[("part", "contentDetails".to_owned()), ("id", channel_id)],
            )
            .await?;
        let uploads = channel
            .get("items")
            .and_then(Value::as_array)
            .and_then(|items| items.first())
            .and_then(|item| item.pointer("/contentDetails/relatedPlaylists/uploads"))
            .and_then(Value::as_str)
            .ok_or(CollectorError::InvalidPayload)?;
        let collect_all = config
            .get("collectAllVideos")
            .and_then(Value::as_bool)
            .unwrap_or(false);
        let maximum = if collect_all {
            5_000
        } else {
            config
                .get("maxVideos")
                .and_then(Value::as_u64)
                .and_then(|value| usize::try_from(value).ok())
                .unwrap_or(50)
                .clamp(1, 5_000)
        };
        let mut ids = job
            .checkpoint
            .get("discoveredIds")
            .and_then(Value::as_array)
            .into_iter()
            .flatten()
            .filter_map(Value::as_str)
            .take(maximum)
            .map(str::to_owned)
            .collect::<Vec<_>>();
        ids.reserve(maximum.min(500).saturating_sub(ids.len()));
        let mut page_token = job
            .checkpoint
            .get("pageToken")
            .and_then(Value::as_str)
            .or_else(|| {
                coverage
                    .get("channelReconciliationNextPageToken")
                    .and_then(Value::as_str)
            })
            .map(str::to_owned);
        let mut pages = job
            .checkpoint
            .get("batchCursor")
            .and_then(Value::as_i64)
            .and_then(|value| i32::try_from(value).ok())
            .unwrap_or(0)
            .max(0);
        while ids.len() < maximum {
            self.store.renew(job.id, &job.lease_owner, 180).await?;
            let mut params = vec![
                ("part", "contentDetails".to_owned()),
                ("playlistId", uploads.to_owned()),
                ("maxResults", "50".to_owned()),
            ];
            if let Some(token) = page_token.as_ref() {
                params.push(("pageToken", token.clone()));
            }
            let payload = self.youtube_request(job, "playlistItems", &params).await?;
            append_video_ids(&mut ids, &payload, "/contentDetails/videoId", maximum);
            pages = pages.saturating_add(1);
            page_token = optional_text(&payload, "nextPageToken").map(str::to_owned);
            let checkpoint = json!({
                "stage": "channel_playlist", "scopeKey": uploads,
                "pageToken": page_token, "batchCursor": pages,
                "channelReconciliationNextPageToken": page_token,
                "channelReconciliationComplete": page_token.is_none(),
                "channelStoredVideoCount": ids.len(), "discoveredIds": ids,
            });
            self.store
                .checkpoint(
                    job,
                    "discovering_channel",
                    &checkpoint,
                    pages,
                    None,
                    "pages",
                )
                .await?;
            if page_token.is_none() {
                break;
            }
        }
        Ok(ids)
    }

    async fn discover_keyword(
        &mut self,
        job: &ClaimedJob,
        config: &Value,
        coverage: &Value,
    ) -> Result<Vec<String>, CollectorError> {
        if self.config.discovery_provider == "searchapi" {
            return self.discover_keyword_searchapi(job, config, coverage).await;
        }
        let query = required_text(config, "query")?;
        let maximum_pages = config
            .get("maxPagesPerRun")
            .and_then(Value::as_u64)
            .and_then(|value| usize::try_from(value).ok())
            .unwrap_or(1)
            .clamp(1, 100);
        let mut ids = job
            .checkpoint
            .get("discoveredIds")
            .and_then(Value::as_array)
            .into_iter()
            .flatten()
            .filter_map(Value::as_str)
            .take(5_000)
            .map(str::to_owned)
            .collect::<Vec<_>>();
        ids.reserve(maximum_pages.saturating_mul(50).saturating_sub(ids.len()));
        let mut page_token = job
            .checkpoint
            .get("pageToken")
            .and_then(Value::as_str)
            .or_else(|| coverage.get("keywordNextPageToken").and_then(Value::as_str))
            .map(str::to_owned);
        let completed_pages = job
            .checkpoint
            .get("batchCursor")
            .and_then(Value::as_u64)
            .and_then(|value| usize::try_from(value).ok())
            .unwrap_or(0)
            .min(maximum_pages);
        for page in completed_pages.saturating_add(1)..=maximum_pages {
            self.store.renew(job.id, &job.lease_owner, 180).await?;
            let mut params = vec![
                ("part", "snippet".to_owned()),
                ("type", "video".to_owned()),
                ("q", query.to_owned()),
                ("maxResults", "50".to_owned()),
                (
                    "order",
                    optional_text(config, "order").unwrap_or("date").to_owned(),
                ),
            ];
            for (key, name) in [
                ("publishedAfter", "publishedAfter"),
                ("publishedBefore", "publishedBefore"),
                ("regionCode", "regionCode"),
                ("relevanceLanguage", "relevanceLanguage"),
            ] {
                if let Some(value) = optional_text(config, name) {
                    params.push((key, value.to_owned()));
                }
            }
            if let Some(token) = page_token.as_ref() {
                params.push(("pageToken", token.clone()));
            }
            let payload = self.youtube_request(job, "search", &params).await?;
            append_video_ids(&mut ids, &payload, "/id/videoId", 5_000);
            page_token = optional_text(&payload, "nextPageToken").map(str::to_owned);
            let checkpoint = json!({
                "stage": "youtube_keyword", "scopeKey": query,
                "pageToken": page_token, "batchCursor": page,
                "keywordHistoricalBackfillComplete": page_token.is_none(),
                "keywordNextPageToken": page_token,
                "keywordCoverage": if page_token.is_some() && page == maximum_pages {"limited"} else {"complete"},
                "discoveredIds": ids,
            });
            self.store
                .checkpoint(
                    job,
                    "discovering_keyword",
                    &checkpoint,
                    i32::try_from(page).map_err(|_| CollectorError::TooManyVideos)?,
                    Some(i32::try_from(maximum_pages).map_err(|_| CollectorError::TooManyVideos)?),
                    "pages",
                )
                .await?;
            if page_token.is_none() {
                break;
            }
        }
        Ok(ids)
    }

    #[allow(clippy::too_many_lines)]
    async fn discover_channel_searchapi(
        &mut self,
        job: &ClaimedJob,
        config: &Value,
        coverage: &Value,
    ) -> Result<Vec<String>, CollectorError> {
        let input = required_text(config, "input")?;
        let mut channel_input = normalized_channel_input(input);
        if !is_channel_identifier(&channel_input) {
            let payload = self.searchapi()?.youtube(&channel_input, None).await;
            match payload {
                Ok(payload) => {
                    self.log_provider_request(job.id, "youtube", 200, None, item_count(&payload))
                        .await?;
                    channel_input = first_searchapi_channel_id(&payload)?;
                }
                Err(error) => {
                    self.log_searchapi_error(job.id, "youtube", &error).await?;
                    return Err(error.into());
                }
            }
        }
        let channel_payload = self.searchapi()?.channel(&channel_input).await;
        let channel_payload = match channel_payload {
            Ok(payload) => {
                self.log_provider_request(
                    job.id,
                    "youtube_channel",
                    200,
                    None,
                    item_count(&payload),
                )
                .await?;
                payload
            }
            Err(error) => {
                self.log_searchapi_error(job.id, "youtube_channel", &error)
                    .await?;
                return Err(error.into());
            }
        };
        let channel = channel_payload
            .get("channel")
            .ok_or(CollectorError::InvalidPayload)?;
        let channel_id = required_text(channel, "id")?.to_owned();
        self.persist_searchapi_channel(job, &channel_payload)
            .await?;
        let collect_all = config
            .get("collectAllVideos")
            .and_then(Value::as_bool)
            .unwrap_or(false);
        let maximum = if collect_all {
            5_000
        } else {
            usize::try_from(
                job.max_videos
                    .or_else(|| {
                        config
                            .get("maxVideos")
                            .and_then(Value::as_i64)
                            .and_then(|value| i32::try_from(value).ok())
                    })
                    .unwrap_or(50)
                    .clamp(1, 5_000),
            )
            .map_err(|_| CollectorError::TooManyVideos)?
        };
        let mut ids = job
            .checkpoint
            .get("discoveredIds")
            .and_then(Value::as_array)
            .into_iter()
            .flatten()
            .filter_map(Value::as_str)
            .take(maximum)
            .map(str::to_owned)
            .collect::<Vec<_>>();
        ids.reserve(maximum.min(500).saturating_sub(ids.len()));
        let mut page_token = job
            .checkpoint
            .get("channelReconciliationNextPageToken")
            .and_then(Value::as_str)
            .or_else(|| {
                coverage
                    .get("channelReconciliationNextPageToken")
                    .and_then(Value::as_str)
            })
            .map(str::to_owned);
        let mut pages = job
            .checkpoint
            .get("batchCursor")
            .and_then(Value::as_i64)
            .and_then(|value| i32::try_from(value).ok())
            .unwrap_or(0)
            .max(0);
        while ids.len() < maximum {
            self.store.renew(job.id, &job.lease_owner, 180).await?;
            let response = self
                .searchapi()?
                .channel_videos(&channel_id, page_token.as_deref())
                .await;
            let payload = match response {
                Ok(payload) => {
                    self.log_provider_request(
                        job.id,
                        "youtube_channel_videos",
                        200,
                        None,
                        item_count(&payload),
                    )
                    .await?;
                    payload
                }
                Err(error) => {
                    self.log_searchapi_error(job.id, "youtube_channel_videos", &error)
                        .await?;
                    return Err(error.into());
                }
            };
            append_searchapi_video_ids(&mut ids, &payload, maximum);
            pages = pages.saturating_add(1);
            page_token = payload
                .pointer("/pagination/next_page_token")
                .and_then(Value::as_str)
                .filter(|value| !value.is_empty())
                .map(str::to_owned);
            let checkpoint = json!({
                "stage": "searchapi_channel_videos", "scopeKey": channel_id,
                "pageToken": page_token, "batchCursor": pages,
                "channelReconciliationNextPageToken": page_token,
                "channelReconciliationComplete": page_token.is_none(),
                "channelReportedVideoCount": optional_count(channel, "videos"),
                "channelStoredVideoCount": ids.len(),
                "discoveredIds": ids,
            });
            self.store
                .checkpoint(
                    job,
                    "discovering_channel",
                    &checkpoint,
                    pages,
                    None,
                    "pages",
                )
                .await?;
            if page_token.is_none() {
                break;
            }
        }
        Ok(ids)
    }

    async fn discover_keyword_searchapi(
        &mut self,
        job: &ClaimedJob,
        config: &Value,
        coverage: &Value,
    ) -> Result<Vec<String>, CollectorError> {
        let query = required_text(config, "query")?;
        let maximum_pages = config
            .get("maxPagesPerRun")
            .and_then(Value::as_i64)
            .and_then(|value| i32::try_from(value).ok())
            .unwrap_or(1)
            .clamp(1, 100);
        let mut ids = job
            .checkpoint
            .get("discoveredIds")
            .and_then(Value::as_array)
            .into_iter()
            .flatten()
            .filter_map(Value::as_str)
            .take(5_000)
            .map(str::to_owned)
            .collect::<Vec<_>>();
        ids.reserve(
            usize::try_from(maximum_pages)
                .unwrap_or(1)
                .saturating_mul(50)
                .saturating_sub(ids.len()),
        );
        let mut page_token = job
            .checkpoint
            .get("pageToken")
            .and_then(Value::as_str)
            .or_else(|| coverage.get("keywordNextPageToken").and_then(Value::as_str))
            .map(str::to_owned);
        let completed_pages = job
            .checkpoint
            .get("batchCursor")
            .and_then(Value::as_i64)
            .and_then(|value| i32::try_from(value).ok())
            .unwrap_or(0)
            .clamp(0, maximum_pages);
        for page in (completed_pages.saturating_add(1))..=maximum_pages {
            self.store.renew(job.id, &job.lease_owner, 180).await?;
            let response = self
                .searchapi()?
                .youtube(query, page_token.as_deref())
                .await;
            let payload = match response {
                Ok(payload) => {
                    self.log_provider_request(job.id, "youtube", 200, None, item_count(&payload))
                        .await?;
                    payload
                }
                Err(error) => {
                    self.log_searchapi_error(job.id, "youtube", &error).await?;
                    return Err(error.into());
                }
            };
            append_searchapi_video_ids(&mut ids, &payload, 5_000);
            page_token = payload
                .pointer("/pagination/next_page_token")
                .and_then(Value::as_str)
                .filter(|value| !value.is_empty())
                .map(str::to_owned);
            let checkpoint = json!({
                "stage": "searchapi_keyword", "scopeKey": query,
                "pageToken": page_token, "batchCursor": page,
                "keywordHistoricalBackfillComplete": page_token.is_none(),
                "keywordNextPageToken": page_token,
                "keywordCoverage": if page_token.is_some() && page == maximum_pages {"limited"} else {"complete"},
                "discoveredIds": ids,
            });
            self.store
                .checkpoint(
                    job,
                    "discovering_keyword",
                    &checkpoint,
                    page,
                    Some(maximum_pages),
                    "pages",
                )
                .await?;
            if page_token.is_none() {
                break;
            }
        }
        Ok(ids)
    }

    async fn youtube_request(
        &mut self,
        job: &ClaimedJob,
        endpoint: &'static str,
        parameters: &[(&str, String)],
    ) -> Result<Value, CollectorError> {
        self.store.renew(job.id, &job.lease_owner, 180).await?;
        let result = self.youtube.request(endpoint, parameters).await;
        let fingerprint = self.youtube.key_fingerprint();
        let bucket = if endpoint == "search" {
            "search_queries"
        } else {
            "core"
        };
        match result {
            Ok(payload) => {
                if let Some(fingerprint) = fingerprint.as_deref() {
                    self.store
                        .record_runtime_key_state(job.runtime_config_id, fingerprint, None)
                        .await?;
                }
                self.store
                    .record_api_request(job, bucket, endpoint, 200, None)
                    .await?;
                Ok(payload)
            }
            Err(error) => {
                if let Some(fingerprint) = fingerprint.as_deref() {
                    self.store
                        .record_runtime_key_state(
                            job.runtime_config_id,
                            fingerprint,
                            Some(error.reason().unwrap_or("upstream_error")),
                        )
                        .await?;
                }
                self.store
                    .record_api_request(job, bucket, endpoint, error.status_code(), error.reason())
                    .await?;
                Err(error.into())
            }
        }
    }

    fn searchapi(&self) -> Result<&SearchApiClient, CollectorError> {
        self.config
            .searchapi
            .as_ref()
            .ok_or(CollectorError::MissingSearchApi)
    }

    async fn log_searchapi_error(
        &self,
        job_id: Uuid,
        operation: &str,
        error: &SearchApiError,
    ) -> Result<(), CollectorError> {
        self.log_provider_request(
            job_id,
            operation,
            i32::from(error.status_code()),
            Some(error.code()),
            None,
        )
        .await
    }

    async fn log_provider_request(
        &self,
        job_id: Uuid,
        operation: &str,
        status_code: i32,
        error_code: Option<&str>,
        item_count: Option<i32>,
    ) -> Result<(), CollectorError> {
        sqlx::query(
            r"
            INSERT INTO provider_request_logs (
              job_id, provider, operation, status_code, error_code, item_count
            ) VALUES ($1, 'searchapi', $2, $3, $4, $5)
            ",
        )
        .bind(job_id)
        .bind(operation.chars().take(100).collect::<String>())
        .bind(status_code)
        .bind(error_code.map(|value| value.chars().take(80).collect::<String>()))
        .bind(item_count)
        .execute(&self.pool)
        .await?;
        Ok(())
    }

    async fn persist_searchapi_channel(
        &self,
        job: &ClaimedJob,
        payload: &Value,
    ) -> Result<(), CollectorError> {
        let channel = payload
            .get("channel")
            .ok_or(CollectorError::InvalidPayload)?;
        let about = payload.get("about").unwrap_or(&Value::Null);
        let youtube_channel_id = required_text(channel, "id")?;
        let fetched_at = Utc::now();
        let mut transaction = self.pool.begin().await?;
        let channel_id = sqlx::query_scalar::<_, Uuid>(
            r"
            INSERT INTO channels (
              youtube_channel_id, handle, title, description, thumbnail_url,
              source_fetched_at
            ) VALUES ($1, $2, $3, $4, $5, $6)
            ON CONFLICT (youtube_channel_id) DO UPDATE
            SET handle = COALESCE(EXCLUDED.handle, channels.handle),
                title = COALESCE(EXCLUDED.title, channels.title),
                description = COALESCE(EXCLUDED.description, channels.description),
                thumbnail_url = COALESCE(EXCLUDED.thumbnail_url, channels.thumbnail_url),
                source_fetched_at = EXCLUDED.source_fetched_at
            RETURNING id
            ",
        )
        .bind(youtube_channel_id)
        .bind(optional_text(channel, "handle"))
        .bind(optional_text(channel, "title"))
        .bind(optional_text(channel, "description").or_else(|| optional_text(about, "description")))
        .bind(optional_text(channel, "avatar"))
        .bind(fetched_at)
        .fetch_one(&mut *transaction)
        .await?;
        sqlx::query(
            r"
            INSERT INTO channel_snapshots (
              channel_id, fetched_at, subscriber_count, view_count, video_count,
              hidden_subscriber_count, source_attribution
            ) VALUES ($1, $2, $3, $4, $5, FALSE, 'searchapi_youtube_channel')
            ON CONFLICT (channel_id, fetched_at) DO UPDATE
            SET subscriber_count = EXCLUDED.subscriber_count,
                view_count = EXCLUDED.view_count,
                video_count = EXCLUDED.video_count
            ",
        )
        .bind(channel_id)
        .bind(fetched_at)
        .bind(
            optional_count(channel, "subscribers").or_else(|| optional_count(about, "subscribers")),
        )
        .bind(optional_count(channel, "views").or_else(|| optional_count(about, "views")))
        .bind(optional_count(channel, "videos").or_else(|| optional_count(about, "videos")))
        .execute(&mut *transaction)
        .await?;
        sqlx::query(
            r"
            INSERT INTO channel_provider_profiles (
              channel_id, provider, keywords, tags, available_countries, badges,
              is_verified, is_family_safe, banner_url, avatar_url,
              external_links, source_fetched_at
            ) VALUES (
              $1, 'searchapi_youtube_channel', $2, $3, $4, $5, $6, $7, $8, $9,
              $10, $11
            )
            ON CONFLICT (channel_id, provider) DO UPDATE
            SET keywords = EXCLUDED.keywords, tags = EXCLUDED.tags,
                available_countries = EXCLUDED.available_countries,
                badges = EXCLUDED.badges, is_verified = EXCLUDED.is_verified,
                is_family_safe = EXCLUDED.is_family_safe,
                banner_url = EXCLUDED.banner_url, avatar_url = EXCLUDED.avatar_url,
                external_links = EXCLUDED.external_links,
                source_fetched_at = EXCLUDED.source_fetched_at
            ",
        )
        .bind(channel_id)
        .bind(optional_text(channel, "keywords"))
        .bind(channel.get("tags").cloned().unwrap_or_else(|| json!([])))
        .bind(
            channel
                .get("available_countries")
                .cloned()
                .unwrap_or_else(|| json!([])),
        )
        .bind(channel.get("badges").cloned().unwrap_or_else(|| json!([])))
        .bind(channel.get("is_verified").and_then(Value::as_bool))
        .bind(channel.get("is_family_safe").and_then(Value::as_bool))
        .bind(optional_text(channel, "banner"))
        .bind(optional_text(channel, "avatar"))
        .bind(about.get("links").cloned().unwrap_or_else(|| json!([])))
        .bind(fetched_at)
        .execute(&mut *transaction)
        .await?;
        transaction.commit().await?;
        self.store
            .promote_channel_target(
                job.source_id,
                youtube_channel_id,
                optional_text(channel, "handle"),
            )
            .await?;
        Ok(())
    }

    async fn collect_transcripts(
        &mut self,
        job: &ClaimedJob,
        youtube_video_ids: &[String],
    ) -> Result<(), CollectorError> {
        for youtube_video_id in youtube_video_ids {
            let exists = sqlx::query_scalar::<_, bool>(
                r"
                SELECT EXISTS (
                  SELECT 1 FROM video_transcripts AS transcript
                  JOIN videos AS video ON video.id = transcript.video_id
                  WHERE video.youtube_video_id = $1 AND transcript.provider = 'searchapi'
                )
                ",
            )
            .bind(youtube_video_id)
            .fetch_one(&self.pool)
            .await?;
            if exists {
                continue;
            }
            self.store.renew(job.id, &job.lease_owner, 180).await?;
            self.collect_transcript(job, youtube_video_id).await?;
        }
        Ok(())
    }

    #[allow(clippy::too_many_lines)]
    async fn collect_transcript(
        &self,
        job: &ClaimedJob,
        youtube_video_id: &str,
    ) -> Result<(), CollectorError> {
        let primary = &self.config.transcript_primary_language;
        let fallback = &self.config.transcript_fallback_language;
        let transcript_type = &self.config.transcript_type;
        let first = self
            .searchapi()?
            .transcripts(youtube_video_id, primary, transcript_type)
            .await;
        let mut payload = match first {
            Ok(payload) => {
                self.log_transcript_request(job.id, 200, None, primary, &payload)
                    .await?;
                payload
            }
            Err(error) if error.is_retryable() => {
                self.log_transcript_error(job.id, primary, &error).await?;
                return Err(error.into());
            }
            Err(error) => {
                self.log_transcript_error(job.id, primary, &error).await?;
                self.persist_transcript_state(
                    youtube_video_id,
                    primary,
                    None,
                    "failed",
                    Some(error.code()),
                    &[],
                    None,
                )
                .await?;
                return Ok(());
            }
        };
        let mut requested = primary.clone();
        let mut selection_reason = "primary_language";
        let mut options = transcript_language_options(&payload);
        if payload
            .get("transcripts")
            .and_then(Value::as_array)
            .is_none_or(Vec::is_empty)
        {
            let fallback_language = options
                .iter()
                .find(|(language, _)| language_matches(language, fallback))
                .map(|(language, _)| language.clone());
            let Some(fallback_language) = fallback_language else {
                self.persist_transcript_state(
                    youtube_video_id,
                    primary,
                    None,
                    "unavailable",
                    Some("preferred_language_unavailable"),
                    &[],
                    None,
                )
                .await?;
                return Ok(());
            };
            requested = fallback_language;
            selection_reason = "fallback_language";
            let fallback_result = self
                .searchapi()?
                .transcripts(youtube_video_id, &requested, transcript_type)
                .await;
            payload = match fallback_result {
                Ok(payload) => {
                    self.log_transcript_request(job.id, 200, None, &requested, &payload)
                        .await?;
                    payload
                }
                Err(error) if error.is_retryable() => {
                    self.log_transcript_error(job.id, &requested, &error)
                        .await?;
                    return Err(error.into());
                }
                Err(error) => {
                    self.log_transcript_error(job.id, &requested, &error)
                        .await?;
                    self.persist_transcript_state(
                        youtube_video_id,
                        &requested,
                        None,
                        "failed",
                        Some(error.code()),
                        &[],
                        None,
                    )
                    .await?;
                    return Ok(());
                }
            };
            let fallback_options = transcript_language_options(&payload);
            if !fallback_options.is_empty() {
                options = fallback_options;
            }
        }
        let mut segments = Vec::new();
        for item in payload
            .get("transcripts")
            .and_then(Value::as_array)
            .into_iter()
            .flatten()
            .take(self.config.transcript_max_segments)
        {
            let Some(text) = optional_text(item, "text").map(str::trim) else {
                continue;
            };
            if text.is_empty() {
                continue;
            }
            let Some(start_ms) = duration_millis(item.get("start")) else {
                continue;
            };
            let Some(duration_ms) = duration_millis(item.get("duration")) else {
                continue;
            };
            segments.push(TranscriptSegmentInput {
                sequence: i32::try_from(segments.len())
                    .map_err(|_| CollectorError::TooManyTranscriptSegments)?,
                start_ms,
                duration_ms,
                text: text.to_owned(),
            });
        }
        if segments.is_empty() {
            self.persist_transcript_state(
                youtube_video_id,
                &requested,
                None,
                "unavailable",
                Some("transcript_empty"),
                &[],
                None,
            )
            .await?;
            return Ok(());
        }
        let resolved = payload
            .pointer("/search_parameters/lang")
            .and_then(Value::as_str)
            .filter(|value| !value.is_empty())
            .unwrap_or(&requested)
            .to_owned();
        let language_name = options
            .iter()
            .find(|(language, _)| language.eq_ignore_ascii_case(&resolved))
            .map(|(_, name)| name.as_str());
        let full_text = segments
            .iter()
            .map(|segment| segment.text.as_str())
            .collect::<Vec<_>>()
            .join("\n");
        let content_hash = hex::encode(sha2::Sha256::digest(full_text.as_bytes()));
        self.persist_transcript_state(
            youtube_video_id,
            &requested,
            Some((&resolved, language_name, selection_reason, &content_hash)),
            "available",
            None,
            &segments,
            Some(&full_text),
        )
        .await
    }

    async fn log_transcript_request(
        &self,
        job_id: Uuid,
        status: i32,
        error: Option<&str>,
        requested_language: &str,
        payload: &Value,
    ) -> Result<(), CollectorError> {
        sqlx::query(
            r"
            INSERT INTO provider_request_logs (
              job_id, provider, operation, status_code, error_code, item_count,
              requested_language, resolved_language
            ) VALUES (
              $1, 'searchapi', 'youtube_transcripts', $2, $3, $4, $5, $6
            )
            ",
        )
        .bind(job_id)
        .bind(status)
        .bind(error)
        .bind(item_count(payload))
        .bind(requested_language)
        .bind(
            payload
                .pointer("/search_parameters/lang")
                .and_then(Value::as_str),
        )
        .execute(&self.pool)
        .await?;
        Ok(())
    }

    async fn log_transcript_error(
        &self,
        job_id: Uuid,
        requested_language: &str,
        error: &SearchApiError,
    ) -> Result<(), CollectorError> {
        self.log_transcript_request(
            job_id,
            i32::from(error.status_code()),
            Some(error.code()),
            requested_language,
            &Value::Null,
        )
        .await
    }

    #[allow(clippy::too_many_arguments)]
    async fn persist_transcript_state(
        &self,
        youtube_video_id: &str,
        requested_language: &str,
        available: Option<(&str, Option<&str>, &str, &str)>,
        state: &str,
        error_code: Option<&str>,
        segments: &[TranscriptSegmentInput],
        full_text: Option<&str>,
    ) -> Result<(), CollectorError> {
        let mut transaction = self.pool.begin().await?;
        let video_id = sqlx::query_scalar::<_, Uuid>(
            "SELECT id FROM videos WHERE youtube_video_id = $1 FOR UPDATE",
        )
        .bind(youtube_video_id)
        .fetch_optional(&mut *transaction)
        .await?
        .ok_or(CollectorError::VideoNotPersisted)?;
        let (resolved, language_name, selection_reason, content_hash) = available.map_or(
            (None, None, None, None),
            |(resolved, name, reason, hash)| (Some(resolved), name, Some(reason), Some(hash)),
        );
        let lowered_name = language_name.unwrap_or("").to_ascii_lowercase();
        let transcript_id = sqlx::query_scalar::<_, Uuid>(
            r"
            INSERT INTO video_transcripts (
              video_id, provider, requested_language, resolved_language,
              language_name, selection_reason, transcript_type,
              is_auto_generated, is_translated, state, full_text, content_hash,
              fetched_at, last_attempted_at, error_code
            ) VALUES (
              $1, 'searchapi', $2, $3, $4, $5, $6, $7, $8, $9, $10, $11,
              CASE WHEN $9 = 'available' THEN now() ELSE NULL END, now(), $12
            )
            ON CONFLICT (video_id, provider) DO UPDATE
            SET requested_language = EXCLUDED.requested_language,
                resolved_language = EXCLUDED.resolved_language,
                language_name = EXCLUDED.language_name,
                selection_reason = EXCLUDED.selection_reason,
                transcript_type = EXCLUDED.transcript_type,
                is_auto_generated = EXCLUDED.is_auto_generated,
                is_translated = EXCLUDED.is_translated, state = EXCLUDED.state,
                full_text = EXCLUDED.full_text, content_hash = EXCLUDED.content_hash,
                fetched_at = EXCLUDED.fetched_at,
                last_attempted_at = EXCLUDED.last_attempted_at,
                error_code = EXCLUDED.error_code, updated_at = now()
            RETURNING id
            ",
        )
        .bind(video_id)
        .bind(requested_language)
        .bind(resolved)
        .bind(language_name)
        .bind(selection_reason)
        .bind(&self.config.transcript_type)
        .bind(available.map(|_| lowered_name.contains("auto-generated")))
        .bind(resolved.map(|value| !value.eq_ignore_ascii_case(requested_language)))
        .bind(state)
        .bind(full_text)
        .bind(content_hash)
        .bind(error_code)
        .fetch_one(&mut *transaction)
        .await?;
        sqlx::query("DELETE FROM video_transcript_segments WHERE transcript_id = $1")
            .bind(transcript_id)
            .execute(&mut *transaction)
            .await?;
        for segment in segments {
            sqlx::query(
                r"
                INSERT INTO video_transcript_segments (
                  transcript_id, sequence, start_ms, duration_ms, text
                ) VALUES ($1, $2, $3, $4, $5)
                ",
            )
            .bind(transcript_id)
            .bind(segment.sequence)
            .bind(segment.start_ms)
            .bind(segment.duration_ms)
            .bind(&segment.text)
            .execute(&mut *transaction)
            .await?;
        }
        transaction.commit().await?;
        Ok(())
    }

    #[allow(clippy::too_many_lines)]
    async fn persist_video_items(
        &self,
        job: &ClaimedJob,
        payload: &Value,
    ) -> Result<(), CollectorError> {
        let items = payload
            .get("items")
            .and_then(Value::as_array)
            .ok_or(CollectorError::InvalidPayload)?;
        let fetched_at = Utc::now();
        let mut transaction = self.pool.begin().await?;
        let current_target_id = sqlx::query_scalar::<_, Option<Uuid>>(
            "SELECT target_id FROM collection_sources WHERE id = $1",
        )
        .bind(job.source_id)
        .fetch_one(&mut *transaction)
        .await?;
        for item in items {
            let youtube_video_id = required_text(item, "id")?;
            let snippet = item.get("snippet").ok_or(CollectorError::InvalidPayload)?;
            let channel_external_id = required_text(snippet, "channelId")?;
            let channel_id = sqlx::query_scalar::<_, Uuid>(
                r"
                INSERT INTO channels (youtube_channel_id, title, source_fetched_at)
                VALUES ($1, $2, $3)
                ON CONFLICT (youtube_channel_id) DO UPDATE
                SET title = COALESCE(EXCLUDED.title, channels.title),
                    source_fetched_at = GREATEST(channels.source_fetched_at, EXCLUDED.source_fetched_at)
                RETURNING id
                ",
            )
            .bind(channel_external_id)
            .bind(optional_text(snippet, "channelTitle"))
            .bind(fetched_at)
            .fetch_one(&mut *transaction)
            .await?;
            let published_at = optional_text(snippet, "publishedAt")
                .map(DateTime::parse_from_rfc3339)
                .transpose()
                .map_err(|_| CollectorError::InvalidPayload)?
                .map(|value| value.with_timezone(&Utc));
            let duration = item
                .get("contentDetails")
                .and_then(|value| optional_text(value, "duration"))
                .and_then(parse_iso_duration);
            let status = item.get("status").unwrap_or(&Value::Null);
            let statistics = item.get("statistics").unwrap_or(&Value::Null);
            let video_id = sqlx::query_scalar::<_, Uuid>(
                r"
                INSERT INTO videos (
                  youtube_video_id, channel_id, title, description, published_at,
                  duration_seconds, privacy_status, made_for_kids, source_fetched_at
                ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)
                ON CONFLICT (youtube_video_id) DO UPDATE
                SET channel_id = EXCLUDED.channel_id, title = EXCLUDED.title,
                    description = EXCLUDED.description, published_at = EXCLUDED.published_at,
                    duration_seconds = EXCLUDED.duration_seconds,
                    privacy_status = EXCLUDED.privacy_status,
                    made_for_kids = EXCLUDED.made_for_kids,
                    source_fetched_at = EXCLUDED.source_fetched_at
                RETURNING id
                ",
            )
            .bind(youtube_video_id)
            .bind(channel_id)
            .bind(optional_text(snippet, "title"))
            .bind(optional_text(snippet, "description"))
            .bind(published_at)
            .bind(duration)
            .bind(optional_text(status, "privacyStatus"))
            .bind(status.get("madeForKids").and_then(Value::as_bool))
            .bind(fetched_at)
            .fetch_one(&mut *transaction)
            .await?;
            sqlx::query(
                r"
                INSERT INTO video_stat_snapshots (
                  video_id, fetched_at, view_count, like_count, comment_count
                ) VALUES ($1, $2, $3, $4, $5)
                ON CONFLICT (video_id, fetched_at) DO UPDATE
                SET view_count = EXCLUDED.view_count,
                    like_count = EXCLUDED.like_count,
                    comment_count = EXCLUDED.comment_count
                ",
            )
            .bind(video_id)
            .bind(fetched_at)
            .bind(statistic(statistics, "viewCount"))
            .bind(statistic(statistics, "likeCount"))
            .bind(statistic(statistics, "commentCount"))
            .execute(&mut *transaction)
            .await?;
            sqlx::query(
                r"
                INSERT INTO source_videos (source_id, video_id)
                VALUES ($1, $2)
                ON CONFLICT (source_id, video_id) DO UPDATE SET last_seen_at = now()
                ",
            )
            .bind(job.source_id)
            .bind(video_id)
            .execute(&mut *transaction)
            .await?;
            if let Some(target_id) = current_target_id {
                sqlx::query(
                    r"
                    INSERT INTO collection_target_videos (target_id, video_id)
                    VALUES ($1, $2)
                    ON CONFLICT (target_id, video_id) DO UPDATE SET last_seen_at = now()
                    ",
                )
                .bind(target_id)
                .bind(video_id)
                .execute(&mut *transaction)
                .await?;
            }
        }
        transaction.commit().await?;
        Ok(())
    }
}

fn video_ids(job: &ClaimedJob, source: &CollectionSource) -> Result<Vec<String>, CollectorError> {
    if let Some(values) = job
        .checkpoint
        .get("youtubeVideoIds")
        .and_then(Value::as_array)
    {
        let ids = values
            .iter()
            .filter_map(Value::as_str)
            .filter(|value| !value.is_empty())
            .map(str::to_owned)
            .collect::<Vec<_>>();
        if !ids.is_empty() {
            return Ok(ids);
        }
    }
    if let Some(value) = job.checkpoint.get("youtubeVideoId").and_then(Value::as_str) {
        return Ok(vec![value.to_owned()]);
    }
    if source.source_type == "video" {
        return source
            .config
            .get("input")
            .and_then(Value::as_str)
            .filter(|value| !value.is_empty())
            .map(|value| vec![value.to_owned()])
            .ok_or(CollectorError::InvalidSourceConfig);
    }
    Ok(Vec::new())
}

fn required_text<'a>(value: &'a Value, key: &str) -> Result<&'a str, CollectorError> {
    optional_text(value, key).ok_or(CollectorError::InvalidPayload)
}

fn optional_text<'a>(value: &'a Value, key: &str) -> Option<&'a str> {
    value
        .get(key)
        .and_then(Value::as_str)
        .filter(|value| !value.is_empty())
}

fn statistic(value: &Value, key: &str) -> i64 {
    value
        .get(key)
        .and_then(Value::as_str)
        .and_then(|value| value.parse::<i64>().ok())
        .unwrap_or(0)
        .max(0)
}

fn parse_iso_duration(value: &str) -> Option<i32> {
    let value = value.strip_prefix("PT")?;
    let mut number = String::new();
    let mut seconds = 0_i64;
    for character in value.chars() {
        if character.is_ascii_digit() {
            number.push(character);
            continue;
        }
        let amount = number.parse::<i64>().ok()?;
        number.clear();
        seconds = seconds.checked_add(match character {
            'H' => amount.checked_mul(3_600)?,
            'M' => amount.checked_mul(60)?,
            'S' => amount,
            _ => return None,
        })?;
    }
    if !number.is_empty() {
        return None;
    }
    i32::try_from(seconds).ok()
}

fn first_id(payload: &Value) -> Result<String, CollectorError> {
    payload
        .get("items")
        .and_then(Value::as_array)
        .and_then(|items| items.first())
        .and_then(|item| item.get("id"))
        .and_then(Value::as_str)
        .map(str::to_owned)
        .ok_or(CollectorError::InvalidPayload)
}

fn append_video_ids(ids: &mut Vec<String>, payload: &Value, pointer: &str, maximum: usize) {
    let Some(items) = payload.get("items").and_then(Value::as_array) else {
        return;
    };
    for id in items
        .iter()
        .filter_map(|item| item.pointer(pointer))
        .filter_map(Value::as_str)
    {
        if ids.len() >= maximum {
            break;
        }
        if !ids.iter().any(|existing| existing == id) {
            ids.push(id.to_owned());
        }
    }
}

fn append_searchapi_video_ids(ids: &mut Vec<String>, payload: &Value, maximum: usize) {
    let mut append = |items: Option<&Vec<Value>>| {
        for id in items
            .into_iter()
            .flatten()
            .filter_map(|item| item.get("id").and_then(Value::as_str))
        {
            if ids.len() >= maximum {
                break;
            }
            if !ids.iter().any(|known| known == id) {
                ids.push(id.to_owned());
            }
        }
    };
    append(payload.get("videos").and_then(Value::as_array));
    if let Some(sections) = payload.get("sections").and_then(Value::as_array) {
        for section in sections {
            let name = optional_text(section, "section_name")
                .or_else(|| optional_text(section, "section_title"))
                .unwrap_or("")
                .to_ascii_lowercase();
            if name.contains("short") {
                append(section.get("items").and_then(Value::as_array));
            }
        }
    }
}

fn normalized_channel_input(input: &str) -> String {
    let trimmed = input.trim().trim_end_matches('/');
    if let Ok(parsed) = url::Url::parse(trimmed) {
        if parsed
            .host_str()
            .is_some_and(|host| host == "youtube.com" || host.ends_with(".youtube.com"))
        {
            let segments = parsed
                .path_segments()
                .into_iter()
                .flatten()
                .filter(|segment| !segment.is_empty())
                .collect::<Vec<_>>();
            if let Some(identifier) = segments
                .iter()
                .find(|segment| segment.starts_with('@') || segment.starts_with("UC"))
            {
                return (*identifier).to_owned();
            }
            if segments.first() == Some(&"channel") {
                if let Some(identifier) = segments.get(1) {
                    return (*identifier).to_owned();
                }
            }
        }
    }
    trimmed.to_owned()
}

fn is_channel_identifier(value: &str) -> bool {
    value.starts_with('@') || (value.starts_with("UC") && value.len() == 24)
}

fn first_searchapi_channel_id(payload: &Value) -> Result<String, CollectorError> {
    if let Some(id) = payload
        .get("channels")
        .and_then(Value::as_array)
        .and_then(|items| items.first())
        .and_then(|item| item.get("id"))
        .and_then(Value::as_str)
    {
        return Ok(id.to_owned());
    }
    payload
        .get("sections")
        .and_then(Value::as_array)
        .into_iter()
        .flatten()
        .flat_map(|section| {
            section
                .get("items")
                .and_then(Value::as_array)
                .into_iter()
                .flatten()
        })
        .find(|item| item.get("length").is_none())
        .and_then(|item| item.get("id"))
        .and_then(Value::as_str)
        .map(str::to_owned)
        .ok_or(CollectorError::InvalidPayload)
}

fn optional_count(value: &Value, key: &str) -> Option<i64> {
    value.get(key).and_then(|count| {
        count
            .as_i64()
            .or_else(|| count.as_u64().and_then(|number| i64::try_from(number).ok()))
            .or_else(|| {
                count
                    .as_str()
                    .map(|text| {
                        text.chars()
                            .filter(char::is_ascii_digit)
                            .collect::<String>()
                    })
                    .and_then(|text| text.parse::<i64>().ok())
            })
    })
}

fn item_count(payload: &Value) -> Option<i32> {
    let count = payload
        .get("videos")
        .or_else(|| payload.get("transcripts"))
        .and_then(Value::as_array)
        .map_or(0, Vec::len);
    i32::try_from(count).ok()
}

fn transcript_language_options(payload: &Value) -> Vec<(String, String)> {
    payload
        .get("available_languages")
        .and_then(Value::as_array)
        .into_iter()
        .flatten()
        .filter_map(|item| {
            let language = optional_text(item, "lang")?.trim();
            if language.is_empty() {
                return None;
            }
            Some((
                language.to_owned(),
                optional_text(item, "name").unwrap_or("").to_owned(),
            ))
        })
        .collect()
}

fn language_matches(candidate: &str, requested: &str) -> bool {
    candidate.eq_ignore_ascii_case(requested)
        || candidate
            .to_ascii_lowercase()
            .starts_with(&format!("{}-", requested.to_ascii_lowercase()))
}

fn duration_millis(value: Option<&Value>) -> Option<i32> {
    let seconds = value.and_then(|item| {
        item.as_f64()
            .or_else(|| item.as_str().and_then(|text| text.parse::<f64>().ok()))
    })?;
    if !seconds.is_finite() || seconds < 0.0 {
        return None;
    }
    let duration = std::time::Duration::try_from_secs_f64(seconds).ok()?;
    i32::try_from(duration.as_millis()).ok()
}

fn parse_comment_threads(payload: &Value) -> Result<Vec<CommentInput>, CollectorError> {
    let items = payload
        .get("items")
        .and_then(Value::as_array)
        .ok_or(CollectorError::InvalidPayload)?;
    let mut comments = Vec::new();
    for thread in items {
        let thread_id = required_text(thread, "id")?;
        let top = thread
            .pointer("/snippet/topLevelComment")
            .ok_or(CollectorError::InvalidPayload)?;
        let top_comment = comment_input(top, thread_id, None)?;
        let top_id = top_comment.id.clone();
        comments.push(top_comment);
        if let Some(replies) = thread
            .pointer("/replies/comments")
            .and_then(Value::as_array)
        {
            for reply in replies {
                comments.push(comment_input(reply, thread_id, Some(&top_id))?);
            }
        }
    }
    Ok(comments)
}

fn reply_traversals(payload: &Value) -> Vec<(String, String)> {
    payload
        .get("items")
        .and_then(Value::as_array)
        .into_iter()
        .flatten()
        .filter_map(|thread| {
            let thread_id = optional_text(thread, "id")?;
            let parent_id = thread
                .pointer("/snippet/topLevelComment/id")
                .and_then(Value::as_str)?;
            let total = thread
                .pointer("/snippet/totalReplyCount")
                .and_then(Value::as_i64)
                .unwrap_or(0);
            let inline = thread
                .pointer("/replies/comments")
                .and_then(Value::as_array)
                .map_or(0, |items| i64::try_from(items.len()).unwrap_or(i64::MAX));
            (total > inline).then(|| (thread_id.to_owned(), parent_id.to_owned()))
        })
        .collect()
}

fn comment_input(
    item: &Value,
    thread_id: &str,
    fallback_parent_id: Option<&str>,
) -> Result<CommentInput, CollectorError> {
    let snippet = item.get("snippet").ok_or(CollectorError::InvalidPayload)?;
    let timestamp = |key: &str| -> Result<Option<DateTime<Utc>>, CollectorError> {
        optional_text(snippet, key)
            .map(DateTime::parse_from_rfc3339)
            .transpose()
            .map(|value| value.map(|date| date.with_timezone(&Utc)))
            .map_err(|_| CollectorError::InvalidPayload)
    };
    Ok(CommentInput {
        id: required_text(item, "id")?.to_owned(),
        parent_id: optional_text(snippet, "parentId")
            .or(fallback_parent_id)
            .map(str::to_owned),
        thread_id: thread_id.to_owned(),
        author_channel_id: snippet
            .pointer("/authorChannelId/value")
            .and_then(Value::as_str)
            .map(str::to_owned),
        author_name: optional_text(snippet, "authorDisplayName").map(str::to_owned),
        text: optional_text(snippet, "textDisplay")
            .or_else(|| optional_text(snippet, "textOriginal"))
            .map(str::to_owned),
        like_count: snippet
            .get("likeCount")
            .and_then(Value::as_i64)
            .unwrap_or(0),
        published_at: timestamp("publishedAt")?,
        updated_at: timestamp("updatedAt")?,
    })
}

#[derive(Debug, Error)]
pub enum CollectorError {
    #[error("collection store operation failed")]
    Store(#[from] StoreError),
    #[error("YouTube request failed")]
    YouTube(#[from] YouTubeError),
    #[error("SearchAPI request failed")]
    SearchApi(#[from] SearchApiError),
    #[error("SearchAPI client is required for the configured collection mode")]
    MissingSearchApi,
    #[error("collection database operation failed")]
    Database(#[from] sqlx::Error),
    #[error("YouTube response is invalid")]
    InvalidPayload,
    #[error("source configuration is invalid")]
    InvalidSourceConfig,
    #[error("discovery for source type {0} is not implemented")]
    UnsupportedDiscovery(String),
    #[error("waiting for child collection jobs")]
    WaitingForChildren { waiting_quota: i64 },
    #[error("{0} child collection jobs failed")]
    ChildJobsFailed(i64),
    #[error("video batch is too large")]
    TooManyVideos,
    #[error("comment job video was not persisted")]
    VideoNotPersisted,
    #[error("reply pagination exceeded 100 pages")]
    TooManyReplyPages,
    #[error("transcript contains too many segments")]
    TooManyTranscriptSegments,
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn parses_youtube_duration() {
        assert_eq!(parse_iso_duration("PT1H2M3S"), Some(3_723));
        assert_eq!(parse_iso_duration("PT45S"), Some(45));
        assert_eq!(parse_iso_duration("P1D"), None);
    }

    #[test]
    fn parses_top_comment_and_inline_reply() -> Result<(), CollectorError> {
        let payload = json!({"items": [{
            "id": "thread-1",
            "snippet": {"topLevelComment": {"id": "top-1", "snippet": {
                "textDisplay": "top", "likeCount": 2, "publishedAt": "2026-01-01T00:00:00Z"
            }}},
            "replies": {"comments": [{"id": "reply-1", "snippet": {
                "parentId": "top-1", "textDisplay": "reply", "likeCount": 0
            }}]}
        }]});
        let comments = parse_comment_threads(&payload)?;
        assert_eq!(comments.len(), 2);
        assert_eq!(comments[1].parent_id.as_deref(), Some("top-1"));
        Ok(())
    }
}
