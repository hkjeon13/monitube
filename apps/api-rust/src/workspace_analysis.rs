//! Workspace analysis over visible rows and persisted sparse `BoW` documents.

use crate::{AppState, auth::AuthUser};
use axum::Json;
use axum::extract::{Extension, RawQuery, State};
use axum::http::StatusCode;
use axum::response::{IntoResponse, Response};
use chrono::{DateTime, Days, NaiveDate, Utc};
use monitube_analysis::{FrequencyAggregate, rank_by_frequency};
use serde::Serialize;
use serde_json::{Value, json};
use sqlx::FromRow;
use thiserror::Error;
use uuid::Uuid;

const ANALYZER_VERSION: &str = "mecab-nltk-v1";

const CHANNEL_BREAKDOWN_GROUPING: &str = r"
WITH video_group AS (
  SELECT COALESCE(video.youtube_channel_id, 'unknown') AS id,
         COALESCE(max(video.channel_title), video.youtube_channel_id,
                  '알 수 없는 채널') AS label,
         count(*)::bigint AS video_count,
         COALESCE(sum(COALESCE(video.view_count, 0)), 0)::bigint AS view_count,
         COALESCE(sum(COALESCE(video.like_count, 0)), 0)::bigint AS like_count,
         COALESCE(sum(COALESCE(video.youtube_comment_count, 0)), 0)::bigint
           AS youtube_comment_count,
         max(video.published_at) AS latest_published_at
  FROM video_period AS video
  GROUP BY video.youtube_channel_id
), comment_group AS (
  SELECT COALESCE(video.youtube_channel_id, 'unknown') AS id,
         count(*)::bigint AS collected_comment_count,
         count(*) FILTER (WHERE comment.youtube_parent_comment_id IS NULL)::bigint
           AS top_level_count,
         count(*) FILTER (WHERE comment.youtube_parent_comment_id IS NOT NULL)::bigint
           AS reply_count
  FROM comment_period AS comment
  JOIN video_period AS video ON video.id = comment.video_id
  GROUP BY video.youtube_channel_id
)
SELECT video.id, video.label, video.video_count, video.view_count,
       video.like_count, video.youtube_comment_count,
       COALESCE(comment.collected_comment_count, 0)::bigint AS collected_comment_count,
       COALESCE(comment.top_level_count, 0)::bigint AS top_level_count,
       COALESCE(comment.reply_count, 0)::bigint AS reply_count,
       video.latest_published_at
FROM video_group AS video
LEFT JOIN comment_group AS comment USING (id)
ORDER BY video.video_count DESC, video.view_count DESC, video.id
LIMIT $8
";

const KEYWORD_BREAKDOWN_GROUPING: &str = r"
WITH keyword_membership AS MATERIALIZED (
  SELECT DISTINCT target_id, video_id
  FROM visible_membership
  WHERE target_type = 'keyword'
), video_group AS (
  SELECT target.id::text AS id,
         COALESCE(NULLIF(target.config ->> 'query', ''), target.canonical_key) AS label,
         count(*)::bigint AS video_count,
         COALESCE(sum(COALESCE(video.view_count, 0)), 0)::bigint AS view_count,
         COALESCE(sum(COALESCE(video.like_count, 0)), 0)::bigint AS like_count,
         COALESCE(sum(COALESCE(video.youtube_comment_count, 0)), 0)::bigint
           AS youtube_comment_count,
         max(video.published_at) AS latest_published_at
  FROM keyword_membership AS membership
  JOIN collection_targets AS target ON target.id = membership.target_id
  JOIN video_period AS video ON video.id = membership.video_id
  GROUP BY target.id, target.config, target.canonical_key
), comment_group AS (
  SELECT membership.target_id::text AS id,
         count(*)::bigint AS collected_comment_count,
         count(*) FILTER (WHERE comment.youtube_parent_comment_id IS NULL)::bigint
           AS top_level_count,
         count(*) FILTER (WHERE comment.youtube_parent_comment_id IS NOT NULL)::bigint
           AS reply_count
  FROM keyword_membership AS membership
  JOIN comment_period AS comment ON comment.video_id = membership.video_id
  GROUP BY membership.target_id
)
SELECT video.id, video.label, video.video_count, video.view_count,
       video.like_count, video.youtube_comment_count,
       COALESCE(comment.collected_comment_count, 0)::bigint AS collected_comment_count,
       COALESCE(comment.top_level_count, 0)::bigint AS top_level_count,
       COALESCE(comment.reply_count, 0)::bigint AS reply_count,
       video.latest_published_at
FROM video_group AS video
LEFT JOIN comment_group AS comment USING (id)
ORDER BY video.video_count DESC, video.view_count DESC, video.id
LIMIT $8
";

#[derive(Debug, Default)]
pub struct AnalysisQuery {
    scope: Option<String>,
    target_ids: Vec<Uuid>,
    channel_ids: Vec<String>,
    from_date: Option<NaiveDate>,
    to_date: Option<NaiveDate>,
    comment_type: Option<String>,
    section: Option<String>,
    limit: Option<usize>,
}

#[derive(Debug, FromRow)]
struct SummaryRow {
    video_count: i64,
    total_view_count: i64,
    median_view_count: i64,
    total_like_count: i64,
    youtube_comment_count: i64,
    collected_comment_count: i64,
    commented_video_count: i64,
    identified_author_count: i64,
    top_level_count: i64,
    reply_count: i64,
    average_comment_like_count: f64,
    transcript_document_count: i64,
    transcript_whitespace_token_count: i64,
    transcript_counted_document_count: i64,
    comment_whitespace_token_count: i64,
    comment_counted_document_count: i64,
    latest_video_published_at: Option<DateTime<Utc>>,
    latest_comment_published_at: Option<DateTime<Utc>>,
    statistics_fetched_at: Option<DateTime<Utc>>,
    videos_with_statistics: i64,
    visible_target_count: i64,
}

#[derive(Debug, FromRow)]
struct KeywordRow {
    term: String,
    document_frequency: i64,
    total_term_frequency: i64,
}

struct ValidatedQuery {
    scope: String,
    target_ids: Vec<Uuid>,
    channel_ids: Vec<String>,
    from_at: Option<DateTime<Utc>>,
    to_at: Option<DateTime<Utc>>,
    comment_type: String,
    limit: usize,
}

pub async fn overview(
    State(state): State<AppState>,
    Extension(user): Extension<AuthUser>,
    RawQuery(raw_query): RawQuery,
) -> Result<Json<Value>, WorkspaceAnalysisError> {
    let query = parse_query(raw_query.as_deref())?;
    let query = validate_query(query, 25)?;
    let summary = load_summary(&state, user.id, &query).await?;
    let video_trend = load_trend(&state, user.id, &query, true).await?;
    let comment_trend = load_trend(&state, user.id, &query, false).await?;
    let channel_breakdown = if query.scope == "keyword" {
        Vec::new()
    } else {
        load_breakdown(&state, user.id, &query, "channel").await?
    };
    let keyword_breakdown = if query.scope == "keyword" {
        load_breakdown(&state, user.id, &query, "keyword").await?
    } else {
        Vec::new()
    };
    let top_videos = load_top_videos(&state, user.id, &query).await?;
    let top_comments = load_top_comments(&state, user.id, &query).await?;
    let (video_keywords, indexed_video_documents) =
        load_keywords(&state, user.id, &query, "transcript", "video").await?;
    let comment_source_kind = "comment";
    let comment_corpus = match query.comment_type.as_str() {
        "top_level" => "comment_top_level",
        "reply" => "comment_reply",
        _ => "comment",
    };
    let (comment_keywords, indexed_comment_documents) =
        load_keywords(&state, user.id, &query, comment_source_kind, comment_corpus).await?;
    let top_words = comment_keywords
        .iter()
        .map(|item| json!({"word": item["term"], "count": item["termCount"]}))
        .collect::<Vec<_>>();
    let question = load_question_signals(&state, user.id, &query).await?;
    let generated_at = Utc::now();
    let comments = summary.collected_comment_count.max(0);
    let reply_rate = percentage(summary.reply_count, comments);
    let author_diversity_rate = percentage(summary.identified_author_count, comments);

    Ok(Json(json!({
        "summary": {
            "videoCount": summary.video_count.max(0),
            "totalViewCount": summary.total_view_count.max(0),
            "medianViewCount": summary.median_view_count.max(0),
            "totalLikeCount": summary.total_like_count.max(0),
            "youtubeCommentCount": summary.youtube_comment_count.max(0),
            "collectedCommentCount": comments,
            "commentedVideoCount": summary.commented_video_count.max(0),
            "identifiedAuthorCount": summary.identified_author_count.max(0),
            "topLevelCount": summary.top_level_count.max(0),
            "replyCount": summary.reply_count.max(0),
            "averageCommentLikeCount": summary.average_comment_like_count.max(0.0),
            "latestVideoPublishedAt": summary.latest_video_published_at,
            "latestCommentPublishedAt": summary.latest_comment_published_at,
            "statisticsFetchedAt": summary.statistics_fetched_at,
        },
        "videoTrend": video_trend,
        "commentTrend": comment_trend,
        "channelBreakdown": channel_breakdown,
        "keywordBreakdown": keyword_breakdown,
        "topVideos": top_videos,
        "topComments": top_comments,
        "topWords": top_words,
        "videoKeywords": video_keywords,
        "commentKeywords": comment_keywords,
        "keywordCoverage": {
            "indexedVideoDocuments": indexed_video_documents,
            "indexedCommentDocuments": indexed_comment_documents,
            "analyzerVersion": ANALYZER_VERSION,
        },
        "commentSignals": {
            "replyRate": reply_rate,
            "authorDiversityRate": author_diversity_rate,
            "questionRate": percentage(question.0, question.1),
            "questionCount": question.0,
            "questionSampleSize": question.1,
        },
        "storageMetrics": {
            "transcriptDocumentCount": summary.transcript_document_count.max(0),
            "transcriptWhitespaceTokenCount":
                summary.transcript_whitespace_token_count.max(0),
            "transcriptCountedDocumentCount":
                summary.transcript_counted_document_count.max(0),
            "commentDocumentCount": comments,
            "commentWhitespaceTokenCount": summary.comment_whitespace_token_count.max(0),
            "commentCountedDocumentCount": summary.comment_counted_document_count.max(0),
        },
        "coverage": {
            "visibleTargetCount": summary.visible_target_count.max(0),
            "includedVideoCount": summary.video_count.max(0),
            "videosWithStatistics": summary.videos_with_statistics.max(0),
            "sampledComments": question.1,
            "totalComments": comments,
            "partialData": indexed_comment_documents < comments,
            "generatedAt": generated_at,
        }
    })))
}

pub async fn insights(
    State(state): State<AppState>,
    Extension(user): Extension<AuthUser>,
    RawQuery(raw_query): RawQuery,
) -> Result<Json<Value>, WorkspaceAnalysisError> {
    let query = parse_query(raw_query.as_deref())?;
    let query = validate_query(query, 50)?;
    let base = insights_cte();
    let summary_sql = format!(
        "{base}\n{}",
        r"
        SELECT jsonb_build_object(
          'videoCount', count(*),
          'comparableVideoCount', count(*) FILTER (WHERE channel_median_views_per_day IS NOT NULL),
          'snapshotEligible7d', count(*) FILTER (WHERE baseline_fetched_at IS NOT NULL),
          'medianViewsPerDay', COALESCE(percentile_disc(0.5) WITHIN GROUP (ORDER BY views_per_day), 0),
          'medianLikeRate', COALESCE(percentile_disc(0.5) WITHIN GROUP (ORDER BY like_rate), 0),
          'medianCommentRatePerThousand', COALESCE(percentile_disc(0.5) WITHIN GROUP (ORDER BY comment_rate_per_thousand), 0),
          'totalViewGrowth7d', COALESCE(sum(view_growth_7d), 0),
          'collectionCoverageRate', CASE WHEN sum(youtube_comment_count) > 0
            THEN round((sum(collected_comment_count)::numeric / sum(youtube_comment_count)) * 100, 2)
            ELSE NULL END)
        FROM performance
        "
    );
    let performance_summary = insights_value(&state, &summary_sql, user.id, &query)
        .await?
        .unwrap_or_else(|| json!({}));
    let videos_sql = format!(
        "{base}\n{}",
        r"
        SELECT jsonb_build_object(
          'id', youtube_video_id, 'channelId', youtube_channel_id,
          'channelTitle', channel_title, 'title', title,
          'publishedAt', published_at, 'statisticsFetchedAt', statistics_fetched_at,
          'viewCount', view_count, 'likeCount', like_count,
          'youtubeCommentCount', youtube_comment_count,
          'collectedCommentCount', collected_comment_count,
          'ageDays', age_days, 'viewsPerDay', views_per_day,
          'likeRate', like_rate, 'commentRatePerThousand', comment_rate_per_thousand,
          'engagementRate', engagement_rate,
          'collectionCoverageRate', collection_coverage_rate,
          'viewGrowth7d', view_growth_7d, 'likeGrowth7d', like_growth_7d,
          'commentGrowth7d', comment_growth_7d, 'growthWindowDays', growth_window_days,
          'channelMedianViewsPerDay', channel_median_views_per_day,
          'channelMedianEngagementRate', channel_median_engagement_rate,
          'channelMedianMultiple', CASE WHEN channel_median_views_per_day > 0
            THEN round((views_per_day / channel_median_views_per_day)::numeric, 2)
            ELSE NULL END)
        FROM performance
        ORDER BY views_per_day DESC, view_count DESC, youtube_video_id
        LIMIT $8
        "
    );
    let performance_videos = insights_values(&state, &videos_sql, user.id, &query).await?;
    let heatmap_sql = format!(
        "{base}\n{}",
        r"
        SELECT jsonb_build_object(
          'weekday', weekday, 'hourBucket', hour_bucket,
          'videoCount', count(*),
          'medianViewsPerDay', COALESCE(percentile_disc(0.5) WITHIN GROUP (ORDER BY views_per_day), 0))
        FROM (
          SELECT EXTRACT(ISODOW FROM published_at AT TIME ZONE 'Asia/Seoul')::integer AS weekday,
                 (floor(EXTRACT(HOUR FROM published_at AT TIME ZONE 'Asia/Seoul') / 3) * 3)::integer AS hour_bucket,
                 views_per_day
          FROM performance WHERE published_at IS NOT NULL
        ) AS publishing
        GROUP BY weekday, hour_bucket
        ORDER BY weekday, hour_bucket
        LIMIT $8
        "
    );
    let publishing_heatmap = insights_values(&state, &heatmap_sql, user.id, &query).await?;
    let video_count = performance_summary
        .get("videoCount")
        .and_then(Value::as_i64)
        .unwrap_or(0);
    let comparable = performance_summary
        .get("comparableVideoCount")
        .and_then(Value::as_i64)
        .unwrap_or(0);
    let eligible = performance_summary
        .get("snapshotEligible7d")
        .and_then(Value::as_i64)
        .unwrap_or(0);
    Ok(Json(json!({
        "performanceSummary": performance_summary,
        "insights": build_insight_cards(&performance_videos),
        "performanceVideos": performance_videos,
        "publishingHeatmap": publishing_heatmap,
        "coverage": {
            "generatedAt": Utc::now(),
            "videoCount": video_count.max(0),
            "comparableVideoCount": comparable.max(0),
            "snapshotEligible7d": eligible.max(0),
        }
    })))
}

fn insights_cte() -> String {
    format!(
        "{SCOPE_CTE}\n{}",
        r"
        , measured AS MATERIALIZED (
          SELECT video.*,
                 COALESCE(rollup.stored_count, 0)::bigint AS collected_comment_count,
                 baseline.fetched_at AS baseline_fetched_at,
                 baseline.view_count AS baseline_view_count,
                 baseline.like_count AS baseline_like_count,
                 baseline.comment_count AS baseline_comment_count
          FROM video_period AS video
          LEFT JOIN video_comment_rollups AS rollup ON rollup.video_id = video.id
          LEFT JOIN LATERAL (
            SELECT snapshot.fetched_at, snapshot.view_count,
                   snapshot.like_count, snapshot.comment_count
            FROM video_stat_snapshots AS snapshot
            WHERE snapshot.video_id = video.id AND snapshot.deleted_at IS NULL
              AND (snapshot.expires_at IS NULL OR snapshot.expires_at > now())
              AND video.statistics_fetched_at IS NOT NULL
              AND snapshot.fetched_at <= video.statistics_fetched_at - interval '7 days'
            ORDER BY snapshot.fetched_at DESC LIMIT 1
          ) AS baseline ON TRUE
        ), rates AS MATERIALIZED (
          SELECT measured.*,
                 GREATEST(EXTRACT(EPOCH FROM
                   (COALESCE(statistics_fetched_at, now()) - published_at)) / 86400.0, 1.0)
                   AS age_days,
                 GREATEST(COALESCE(view_count, 0), 0)::bigint AS safe_views,
                 GREATEST(COALESCE(like_count, 0), 0)::bigint AS safe_likes,
                 GREATEST(COALESCE(youtube_comment_count, 0), 0)::bigint AS safe_comments
          FROM measured
        ), scored AS MATERIALIZED (
          SELECT rates.*,
                 round((safe_views / age_days)::numeric, 2)::double precision AS views_per_day,
                 CASE WHEN safe_views > 0 THEN round((safe_likes::numeric / safe_views) * 100, 2)::double precision ELSE 0 END AS like_rate,
                 CASE WHEN safe_views > 0 THEN round((safe_comments::numeric / safe_views) * 1000, 2)::double precision ELSE 0 END AS comment_rate_per_thousand,
                 CASE WHEN safe_views > 0 THEN round(((safe_likes + safe_comments)::numeric / safe_views) * 100, 2)::double precision ELSE 0 END AS engagement_rate
          FROM rates
        ), channel_medians AS (
          SELECT youtube_channel_id,
                 percentile_disc(0.5) WITHIN GROUP (ORDER BY views_per_day) AS median_views_per_day,
                 percentile_disc(0.5) WITHIN GROUP (ORDER BY engagement_rate) AS median_engagement_rate
          FROM scored GROUP BY youtube_channel_id
        ), performance AS (
          SELECT scored.youtube_video_id, scored.youtube_channel_id,
                 scored.channel_title, scored.title, scored.published_at,
                 scored.statistics_fetched_at, scored.safe_views AS view_count,
                 scored.safe_likes AS like_count, scored.safe_comments AS youtube_comment_count,
                 scored.collected_comment_count, scored.age_days, scored.views_per_day,
                 scored.like_rate, scored.comment_rate_per_thousand, scored.engagement_rate,
                 CASE WHEN scored.safe_comments > 0 THEN round((scored.collected_comment_count::numeric / scored.safe_comments) * 100, 2)::double precision ELSE NULL END AS collection_coverage_rate,
                 scored.baseline_fetched_at,
                 CASE WHEN baseline_view_count IS NOT NULL THEN GREATEST(scored.safe_views - baseline_view_count, 0) ELSE NULL END AS view_growth_7d,
                 CASE WHEN baseline_like_count IS NOT NULL THEN GREATEST(scored.safe_likes - baseline_like_count, 0) ELSE NULL END AS like_growth_7d,
                 CASE WHEN baseline_comment_count IS NOT NULL THEN GREATEST(scored.safe_comments - baseline_comment_count, 0) ELSE NULL END AS comment_growth_7d,
                 CASE WHEN baseline_fetched_at IS NOT NULL THEN EXTRACT(EPOCH FROM (statistics_fetched_at - baseline_fetched_at)) / 86400.0 ELSE NULL END AS growth_window_days,
                 median.median_views_per_day AS channel_median_views_per_day,
                 median.median_engagement_rate AS channel_median_engagement_rate
          FROM scored LEFT JOIN channel_medians AS median
            ON median.youtube_channel_id IS NOT DISTINCT FROM scored.youtube_channel_id
        )
        "
    )
}

async fn insights_value(
    state: &AppState,
    sql: &str,
    user_id: Uuid,
    params: &ValidatedQuery,
) -> Result<Option<Value>, WorkspaceAnalysisError> {
    sqlx::query_scalar(sql)
        .bind(user_id)
        .bind(&params.scope)
        .bind(&params.target_ids)
        .bind(&params.channel_ids)
        .bind(params.from_at)
        .bind(params.to_at)
        .bind(&params.comment_type)
        .fetch_optional(&state.pool)
        .await
        .map_err(WorkspaceAnalysisError::Database)
}

async fn insights_values(
    state: &AppState,
    sql: &str,
    user_id: Uuid,
    params: &ValidatedQuery,
) -> Result<Vec<Value>, WorkspaceAnalysisError> {
    sqlx::query_scalar(sql)
        .bind(user_id)
        .bind(&params.scope)
        .bind(&params.target_ids)
        .bind(&params.channel_ids)
        .bind(params.from_at)
        .bind(params.to_at)
        .bind(&params.comment_type)
        .bind(i64::try_from(params.limit).map_err(|_| WorkspaceAnalysisError::InvalidQuery)?)
        .fetch_all(&state.pool)
        .await
        .map_err(WorkspaceAnalysisError::Database)
}

fn build_insight_cards(videos: &[Value]) -> Vec<Value> {
    let Some(video) = videos.first() else {
        return Vec::new();
    };
    let id = video.get("id").and_then(Value::as_str).unwrap_or_default();
    let title = video.get("title").and_then(Value::as_str).unwrap_or(id);
    let views_per_day = video
        .get("viewsPerDay")
        .and_then(Value::as_f64)
        .unwrap_or(0.0);
    if id.is_empty() {
        return Vec::new();
    }
    vec![json!({
        "id": format!("breakout-{id}"), "kind": "breakout", "tone": "positive",
        "title": "현재 조회 속도 상위 영상",
        "description": format!("{title} 영상이 일평균 조회수 기준으로 가장 높습니다."),
        "videoId": id, "value": views_per_day, "unit": "views"
    })]
}

fn parse_query(raw_query: Option<&str>) -> Result<AnalysisQuery, WorkspaceAnalysisError> {
    let mut query = AnalysisQuery::default();
    for (key, value) in url::form_urlencoded::parse(raw_query.unwrap_or_default().as_bytes()) {
        match key.as_ref() {
            "scope" => query.scope = Some(value.into_owned()),
            "targetId" => query.target_ids.push(
                value
                    .parse::<Uuid>()
                    .map_err(|_| WorkspaceAnalysisError::InvalidQuery)?,
            ),
            "channelId" => query.channel_ids.push(value.into_owned()),
            "from" => {
                query.from_date = Some(
                    NaiveDate::parse_from_str(&value, "%Y-%m-%d")
                        .map_err(|_| WorkspaceAnalysisError::InvalidQuery)?,
                );
            }
            "to" => {
                query.to_date = Some(
                    NaiveDate::parse_from_str(&value, "%Y-%m-%d")
                        .map_err(|_| WorkspaceAnalysisError::InvalidQuery)?,
                );
            }
            "commentType" => query.comment_type = Some(value.into_owned()),
            "section" => query.section = Some(value.into_owned()),
            "limit" => {
                query.limit = Some(
                    value
                        .parse::<usize>()
                        .map_err(|_| WorkspaceAnalysisError::InvalidQuery)?,
                );
            }
            _ => {}
        }
    }
    Ok(query)
}

fn validate_query(
    query: AnalysisQuery,
    max_limit: usize,
) -> Result<ValidatedQuery, WorkspaceAnalysisError> {
    let scope = query.scope.unwrap_or_else(|| "all".to_owned());
    let comment_type = query.comment_type.unwrap_or_else(|| "all".to_owned());
    let section = query.section.unwrap_or_else(|| "all".to_owned());
    let limit = query.limit.unwrap_or(10);
    if !matches!(scope.as_str(), "all" | "channel" | "keyword")
        || !matches!(comment_type.as_str(), "all" | "top_level" | "reply")
        || !matches!(section.as_str(), "all" | "core" | "content")
        || !(1..=max_limit).contains(&limit)
        || query.target_ids.len() > 100
        || query.channel_ids.len() > 100
        || query
            .channel_ids
            .iter()
            .any(|value| value.is_empty() || value.len() > 64)
    {
        return Err(WorkspaceAnalysisError::InvalidQuery);
    }
    let from_at = query
        .from_date
        .and_then(|date| date.and_hms_opt(0, 0, 0))
        .map(|value| value.and_utc());
    let to_at = query
        .to_date
        .and_then(|date| date.checked_add_days(Days::new(1)))
        .and_then(|date| date.and_hms_opt(0, 0, 0))
        .map(|value| value.and_utc());
    if from_at.zip(to_at).is_some_and(|(from, to)| from >= to) {
        return Err(WorkspaceAnalysisError::InvalidQuery);
    }
    Ok(ValidatedQuery {
        scope,
        target_ids: query.target_ids,
        channel_ids: query.channel_ids,
        from_at,
        to_at,
        comment_type,
        limit,
    })
}

const SCOPE_CTE: &str = r"
WITH visible_membership AS MATERIALIZED (
  SELECT DISTINCT membership.video_id, target.id AS target_id,
         target.type::text AS target_type, target.config, target.canonical_key
  FROM collection_subscriptions AS subscription
  JOIN collection_targets AS target ON target.id = subscription.target_id
  JOIN collection_target_videos AS membership ON membership.target_id = target.id
  WHERE subscription.user_id = $1
    AND ($2::text <> 'keyword' OR target.type = 'keyword')
    AND (cardinality($3::uuid[]) = 0 OR target.id = ANY($3))
), visible_video AS MATERIALIZED (
  SELECT DISTINCT video.id
  FROM visible_membership AS membership
  JOIN videos AS video ON video.id = membership.video_id
  LEFT JOIN channels AS channel ON channel.id = video.channel_id
  WHERE video.deleted_at IS NULL
    AND ($2::text <> 'channel' OR cardinality($4::text[]) = 0
         OR channel.youtube_channel_id = ANY($4))
), video_period AS MATERIALIZED (
  SELECT video.*, channel.youtube_channel_id, channel.title AS channel_title,
         stats.view_count, stats.like_count,
         stats.comment_count AS youtube_comment_count,
         stats.fetched_at AS statistics_fetched_at
  FROM visible_video AS visible
  JOIN videos AS video ON video.id = visible.id
  LEFT JOIN channels AS channel ON channel.id = video.channel_id
  LEFT JOIN LATERAL (
    SELECT snapshot.view_count, snapshot.like_count, snapshot.comment_count,
           snapshot.fetched_at
    FROM video_stat_snapshots AS snapshot
    WHERE snapshot.video_id = video.id AND snapshot.deleted_at IS NULL
      AND (snapshot.expires_at IS NULL OR snapshot.expires_at > now())
    ORDER BY snapshot.fetched_at DESC LIMIT 1
  ) AS stats ON TRUE
  WHERE ($5::timestamptz IS NULL OR video.published_at >= $5)
    AND ($6::timestamptz IS NULL OR video.published_at < $6)
), comment_period AS MATERIALIZED (
  SELECT comment.*
  FROM comments AS comment
  JOIN visible_video AS visible ON visible.id = comment.video_id
  WHERE comment.deleted_at IS NULL
    AND (comment.expires_at IS NULL OR comment.expires_at > now())
    AND ($5::timestamptz IS NULL OR comment.published_at >= $5)
    AND ($6::timestamptz IS NULL OR comment.published_at < $6)
    AND ($7::text = 'all'
      OR ($7 = 'top_level' AND comment.youtube_parent_comment_id IS NULL)
      OR ($7 = 'reply' AND comment.youtube_parent_comment_id IS NOT NULL))
)
";

fn bind_scope<'q>(
    query: sqlx::query::QueryAs<'q, sqlx::Postgres, SummaryRow, sqlx::postgres::PgArguments>,
    user_id: Uuid,
    params: &'q ValidatedQuery,
) -> sqlx::query::QueryAs<'q, sqlx::Postgres, SummaryRow, sqlx::postgres::PgArguments> {
    query
        .bind(user_id)
        .bind(&params.scope)
        .bind(&params.target_ids)
        .bind(&params.channel_ids)
        .bind(params.from_at)
        .bind(params.to_at)
        .bind(&params.comment_type)
}

async fn load_summary(
    state: &AppState,
    user_id: Uuid,
    params: &ValidatedQuery,
) -> Result<SummaryRow, WorkspaceAnalysisError> {
    let sql = format!(
        "{SCOPE_CTE}\n{}",
        r"
        , transcript_period AS MATERIALIZED (
          SELECT DISTINCT ON (transcript.video_id)
                 transcript.video_id, transcript.whitespace_token_count
          FROM video_transcripts AS transcript
          JOIN video_period AS video ON video.id = transcript.video_id
          WHERE transcript.state = 'available'
            AND transcript.full_text IS NOT NULL
            AND btrim(transcript.full_text) <> ''
          ORDER BY transcript.video_id, transcript.fetched_at DESC NULLS LAST,
                   transcript.updated_at DESC, transcript.id DESC
        ), video_summary AS (
          SELECT count(*)::bigint AS video_count,
                 COALESCE(sum(COALESCE(view_count, 0)), 0)::bigint AS total_view_count,
                 COALESCE(percentile_disc(0.5) WITHIN GROUP
                   (ORDER BY COALESCE(view_count, 0)), 0)::bigint AS median_view_count,
                 COALESCE(sum(COALESCE(like_count, 0)), 0)::bigint AS total_like_count,
                 COALESCE(sum(COALESCE(youtube_comment_count, 0)), 0)::bigint
                   AS youtube_comment_count,
                 max(published_at) AS latest_video_published_at,
                 max(statistics_fetched_at) AS statistics_fetched_at,
                 count(*) FILTER (WHERE statistics_fetched_at IS NOT NULL)::bigint
                   AS videos_with_statistics
          FROM video_period
        ), comment_summary AS (
          SELECT count(*)::bigint AS collected_comment_count,
                 count(DISTINCT video_id)::bigint AS commented_video_count,
                 count(DISTINCT author_channel_id) FILTER
                   (WHERE author_channel_id IS NOT NULL)::bigint AS identified_author_count,
                 count(*) FILTER (WHERE youtube_parent_comment_id IS NULL)::bigint
                   AS top_level_count,
                 count(*) FILTER (WHERE youtube_parent_comment_id IS NOT NULL)::bigint
                   AS reply_count,
                 COALESCE(avg(GREATEST(like_count, 0)), 0)::double precision
                   AS average_comment_like_count,
                 COALESCE(sum(whitespace_token_count), 0)::bigint
                   AS comment_whitespace_token_count,
                 count(*) FILTER (WHERE whitespace_token_count IS NOT NULL)::bigint
                   AS comment_counted_document_count,
                 max(published_at) AS latest_comment_published_at
          FROM comment_period
        ), transcript_summary AS (
          SELECT count(*)::bigint AS transcript_document_count,
                 COALESCE(sum(whitespace_token_count), 0)::bigint
                   AS transcript_whitespace_token_count,
                 count(*) FILTER (WHERE whitespace_token_count IS NOT NULL)::bigint
                   AS transcript_counted_document_count
          FROM transcript_period
        ), target_summary AS (
          SELECT count(DISTINCT target_id)::bigint AS visible_target_count
          FROM visible_membership
        )
        SELECT video.*, comment.*, transcript.*, target.visible_target_count
        FROM video_summary AS video CROSS JOIN comment_summary AS comment
        CROSS JOIN transcript_summary AS transcript
        CROSS JOIN target_summary AS target
        "
    );
    bind_scope(sqlx::query_as(&sql), user_id, params)
        .fetch_one(&state.pool)
        .await
        .map_err(WorkspaceAnalysisError::Database)
}

async fn load_trend(
    state: &AppState,
    user_id: Uuid,
    params: &ValidatedQuery,
    videos: bool,
) -> Result<Vec<Value>, WorkspaceAnalysisError> {
    let bucket = match (params.from_at, params.to_at) {
        (Some(from), Some(to)) if (to - from).num_days() <= 45 => "day",
        (Some(from), Some(to)) if (to - from).num_days() <= 365 => "week",
        _ => "month",
    };
    let body = if videos {
        format!(
            "SELECT date_trunc('{bucket}', published_at) AS period, count(*)::bigint AS count,\
             0::bigint AS top_level_count, 0::bigint AS reply_count FROM video_period \
             WHERE published_at IS NOT NULL GROUP BY period ORDER BY period"
        )
    } else {
        format!(
            "SELECT date_trunc('{bucket}', published_at) AS period, count(*)::bigint AS count,\
             count(*) FILTER (WHERE youtube_parent_comment_id IS NULL)::bigint AS top_level_count,\
             count(*) FILTER (WHERE youtube_parent_comment_id IS NOT NULL)::bigint AS reply_count \
             FROM comment_period WHERE published_at IS NOT NULL GROUP BY period ORDER BY period"
        )
    };
    let sql = format!("{SCOPE_CTE}\n{body}");
    sqlx::query_scalar::<_, Value>(&format!(
        "SELECT jsonb_build_object('period', period, 'count', count, \
         'topLevelCount', top_level_count, 'replyCount', reply_count) FROM ({sql}) AS trend"
    ))
    .bind(user_id)
    .bind(&params.scope)
    .bind(&params.target_ids)
    .bind(&params.channel_ids)
    .bind(params.from_at)
    .bind(params.to_at)
    .bind(&params.comment_type)
    .fetch_all(&state.pool)
    .await
    .map_err(WorkspaceAnalysisError::Database)
}

async fn load_breakdown(
    state: &AppState,
    user_id: Uuid,
    params: &ValidatedQuery,
    kind: &'static str,
) -> Result<Vec<Value>, WorkspaceAnalysisError> {
    if !matches!(kind, "channel" | "keyword") {
        return Err(WorkspaceAnalysisError::InvalidInternalKind);
    }
    let grouping = if kind == "channel" {
        CHANNEL_BREAKDOWN_GROUPING
    } else {
        KEYWORD_BREAKDOWN_GROUPING
    };
    let sql = format!(
        "{SCOPE_CTE}\nSELECT jsonb_build_object(\
         'id', id, 'label', label, 'kind', '{kind}', 'videoCount', video_count,\
         'viewCount', view_count, 'likeCount', like_count,\
         'youtubeCommentCount', youtube_comment_count,\
         'collectedCommentCount', collected_comment_count,\
         'topLevelCount', top_level_count, 'replyCount', reply_count,\
         'latestPublishedAt', latest_published_at) FROM ({grouping}) AS breakdown"
    );
    sqlx::query_scalar(&sql)
        .bind(user_id)
        .bind(&params.scope)
        .bind(&params.target_ids)
        .bind(&params.channel_ids)
        .bind(params.from_at)
        .bind(params.to_at)
        .bind(&params.comment_type)
        .bind(i64::try_from(params.limit).map_err(|_| WorkspaceAnalysisError::InvalidQuery)?)
        .fetch_all(&state.pool)
        .await
        .map_err(WorkspaceAnalysisError::Database)
}

async fn load_top_videos(
    state: &AppState,
    user_id: Uuid,
    params: &ValidatedQuery,
) -> Result<Vec<Value>, WorkspaceAnalysisError> {
    let sql = format!(
        "{SCOPE_CTE}\n{}",
        r"
        , comment_counts AS (
          SELECT video_id, count(*)::bigint AS collected_comment_count,
                 count(*) FILTER (WHERE youtube_parent_comment_id IS NULL)::bigint AS top_level_count,
                 count(*) FILTER (WHERE youtube_parent_comment_id IS NOT NULL)::bigint AS reply_count
          FROM comment_period GROUP BY video_id
        )
        SELECT jsonb_build_object(
          'id', video.youtube_video_id, 'channelId', video.youtube_channel_id,
          'channelTitle', video.channel_title, 'title', video.title,
          'publishedAt', video.published_at, 'durationSeconds', GREATEST(video.duration_seconds, 0),
          'viewCount', GREATEST(COALESCE(video.view_count, 0), 0),
          'likeCount', GREATEST(COALESCE(video.like_count, 0), 0),
          'youtubeCommentCount', GREATEST(COALESCE(video.youtube_comment_count, 0), 0),
          'collectedCommentCount', COALESCE(comment.collected_comment_count, 0),
          'topLevelCount', COALESCE(comment.top_level_count, 0),
          'replyCount', COALESCE(comment.reply_count, 0),
          'statisticsFetchedAt', video.statistics_fetched_at)
        FROM video_period AS video
        LEFT JOIN comment_counts AS comment ON comment.video_id = video.id
        ORDER BY COALESCE(video.view_count, 0) DESC,
                 COALESCE(video.published_at, video.source_fetched_at) DESC,
                 video.youtube_video_id DESC
        LIMIT $8
        "
    );
    scope_value_query(&state.pool, &sql, user_id, params).await
}

async fn load_top_comments(
    state: &AppState,
    user_id: Uuid,
    params: &ValidatedQuery,
) -> Result<Vec<Value>, WorkspaceAnalysisError> {
    let sql = format!(
        "{SCOPE_CTE}\n{}",
        r"
        SELECT jsonb_build_object(
          'id', comment.youtube_comment_id, 'videoId', video.youtube_video_id,
          'videoTitle', video.title, 'channelTitle', video.channel_title,
          'text', comment.text_display, 'authorName', comment.author_display_name,
          'publishedAt', comment.published_at,
          'likeCount', GREATEST(comment.like_count, 0),
          'isReply', comment.youtube_parent_comment_id IS NOT NULL)
        FROM comment_period AS comment
        JOIN video_period AS video ON video.id = comment.video_id
        ORDER BY GREATEST(comment.like_count, 0) DESC,
                 COALESCE(comment.published_at, comment.source_fetched_at) DESC,
                 comment.youtube_comment_id DESC
        LIMIT $8
        "
    );
    scope_value_query(&state.pool, &sql, user_id, params).await
}

async fn scope_value_query(
    pool: &sqlx::PgPool,
    sql: &str,
    user_id: Uuid,
    params: &ValidatedQuery,
) -> Result<Vec<Value>, WorkspaceAnalysisError> {
    sqlx::query_scalar(sql)
        .bind(user_id)
        .bind(&params.scope)
        .bind(&params.target_ids)
        .bind(&params.channel_ids)
        .bind(params.from_at)
        .bind(params.to_at)
        .bind(&params.comment_type)
        .bind(i64::try_from(params.limit).map_err(|_| WorkspaceAnalysisError::InvalidQuery)?)
        .fetch_all(pool)
        .await
        .map_err(WorkspaceAnalysisError::Database)
}

#[allow(clippy::too_many_lines)]
async fn load_keywords(
    state: &AppState,
    user_id: Uuid,
    params: &ValidatedQuery,
    source_kind: &'static str,
    corpus_kind: &'static str,
) -> Result<(Vec<Value>, i64), WorkspaceAnalysisError> {
    if !matches!(source_kind, "transcript" | "comment") {
        return Err(WorkspaceAnalysisError::InvalidInternalKind);
    }
    let comment_filter = match corpus_kind {
        "comment_top_level" => "AND document.indexed_comment_type = 'top_level'",
        "comment_reply" => "AND document.indexed_comment_type = 'reply'",
        "video" | "comment" => "",
        _ => return Err(WorkspaceAnalysisError::InvalidInternalKind),
    };
    let sql = format!(
        r"
        WITH visible_membership AS MATERIALIZED (
          SELECT DISTINCT membership.video_id
          FROM collection_subscriptions AS subscription
          JOIN collection_targets AS target ON target.id = subscription.target_id
          JOIN collection_target_videos AS membership ON membership.target_id = target.id
          WHERE subscription.user_id = $1
            AND ($2::text <> 'keyword' OR target.type = 'keyword')
            AND (cardinality($3::uuid[]) = 0 OR target.id = ANY($3))
        ), visible_video AS MATERIALIZED (
          SELECT DISTINCT video.id
          FROM visible_membership AS membership
          JOIN videos AS video ON video.id = membership.video_id
          LEFT JOIN channels AS channel ON channel.id = video.channel_id
          WHERE video.deleted_at IS NULL
            AND ($2::text <> 'channel' OR cardinality($4::text[]) = 0
                 OR channel.youtube_channel_id = ANY($4))
        ), visible_document AS MATERIALIZED (
          SELECT document.source_kind, document.source_id
          FROM nlp_documents AS document
          JOIN visible_video AS video ON video.id = document.video_id
          WHERE document.source_kind = $5
            AND document.state = 'ready'
            AND document.analyzer_version = $6
            AND ($7::timestamptz IS NULL OR document.indexed_source_date >= $7::date)
            AND ($8::timestamptz IS NULL OR document.indexed_source_date < $8::date)
            {comment_filter}
        ), corpus AS (
          SELECT count(*)::bigint AS document_count FROM visible_document
        ), terms AS (
          SELECT term.term,
                 count(*)::bigint AS document_frequency,
                 sum(term.term_frequency)::bigint AS total_term_frequency
          FROM visible_document AS document
          JOIN nlp_document_terms AS term
            ON term.source_kind = document.source_kind AND term.source_id = document.source_id
          WHERE NOT EXISTS (
            SELECT 1 FROM analysis_excluded_terms AS excluded
            WHERE excluded.user_id = $1 AND excluded.corpus_kind = $9
              AND excluded.term = term.term
          )
          GROUP BY term.term
          ORDER BY total_term_frequency DESC, term.term
          LIMIT $10
        )
        SELECT term, document_frequency, total_term_frequency FROM terms
        "
    );
    let rows = sqlx::query_as::<_, KeywordRow>(&sql)
        .bind(user_id)
        .bind(&params.scope)
        .bind(&params.target_ids)
        .bind(&params.channel_ids)
        .bind(source_kind)
        .bind(ANALYZER_VERSION)
        .bind(params.from_at)
        .bind(params.to_at)
        .bind(if source_kind == "transcript" {
            "video"
        } else {
            "comment"
        })
        .bind(
            i64::try_from(params.limit.saturating_mul(4).max(50))
                .map_err(|_| WorkspaceAnalysisError::InvalidQuery)?,
        )
        .fetch_all(&state.pool)
        .await?;
    let count_sql = format!(
        r"
        WITH visible_membership AS (
          SELECT DISTINCT membership.video_id
          FROM collection_subscriptions AS subscription
          JOIN collection_targets AS target ON target.id = subscription.target_id
          JOIN collection_target_videos AS membership ON membership.target_id = target.id
          WHERE subscription.user_id = $1
            AND ($2::text <> 'keyword' OR target.type = 'keyword')
            AND (cardinality($3::uuid[]) = 0 OR target.id = ANY($3))
        ), visible_video AS (
          SELECT DISTINCT video.id FROM visible_membership AS membership
          JOIN videos AS video ON video.id = membership.video_id
          LEFT JOIN channels AS channel ON channel.id = video.channel_id
          WHERE ($2::text <> 'channel' OR cardinality($4::text[]) = 0
                 OR channel.youtube_channel_id = ANY($4))
        )
        SELECT count(*)::bigint FROM nlp_documents AS document
        JOIN visible_video AS video ON video.id = document.video_id
        WHERE document.source_kind = $5 AND document.state = 'ready'
          AND document.analyzer_version = $6
          AND ($7::timestamptz IS NULL OR document.indexed_source_date >= $7::date)
          AND ($8::timestamptz IS NULL OR document.indexed_source_date < $8::date)
          {comment_filter}
        "
    );
    let document_count = sqlx::query_scalar::<_, i64>(&count_sql)
        .bind(user_id)
        .bind(&params.scope)
        .bind(&params.target_ids)
        .bind(&params.channel_ids)
        .bind(source_kind)
        .bind(ANALYZER_VERSION)
        .bind(params.from_at)
        .bind(params.to_at)
        .fetch_one(&state.pool)
        .await?
        .max(0);
    let ranked = rank_by_frequency(
        rows.into_iter()
            .map(|row| FrequencyAggregate {
                term: row.term,
                total_term_frequency: u64::try_from(row.total_term_frequency.max(0)).unwrap_or(0),
                document_frequency: u64::try_from(row.document_frequency.max(0)).unwrap_or(0),
            })
            .collect::<Vec<_>>(),
        u64::try_from(document_count).unwrap_or(0),
        params.limit,
    );
    Ok((
        ranked
            .into_iter()
            .map(|item| {
                json!({
                    "term": item.term,
                    "termCount": item.term_count,
                    "documentCount": item.document_count,
                    "documentRate": item.document_rate,
                })
            })
            .collect(),
        document_count,
    ))
}

async fn load_question_signals(
    state: &AppState,
    user_id: Uuid,
    params: &ValidatedQuery,
) -> Result<(i64, i64), WorkspaceAnalysisError> {
    let sql = format!(
        "{SCOPE_CTE}\n{}",
        r"
        SELECT count(*) FILTER (
                 WHERE text_display LIKE '%?%' OR text_display LIKE '%？%'
               )::bigint AS question_count,
               count(*)::bigint AS sample_size
        FROM (SELECT text_display FROM comment_period
              ORDER BY COALESCE(published_at, source_fetched_at) DESC LIMIT 5000) AS sample
        "
    );
    sqlx::query_as::<_, (i64, i64)>(&sql)
        .bind(user_id)
        .bind(&params.scope)
        .bind(&params.target_ids)
        .bind(&params.channel_ids)
        .bind(params.from_at)
        .bind(params.to_at)
        .bind(&params.comment_type)
        .fetch_one(&state.pool)
        .await
        .map_err(WorkspaceAnalysisError::Database)
}

fn percentage(numerator: i64, denominator: i64) -> f64 {
    if denominator <= 0 {
        return 0.0;
    }
    #[allow(clippy::cast_precision_loss)]
    let value = numerator.max(0) as f64 / denominator as f64 * 100.0;
    (value * 100.0).round() / 100.0
}

#[derive(Debug, Error)]
pub enum WorkspaceAnalysisError {
    #[error("analysis query is invalid")]
    InvalidQuery,
    #[error("internal analysis kind is invalid")]
    InvalidInternalKind,
    #[error("analysis database operation failed")]
    Database(#[from] sqlx::Error),
}

impl IntoResponse for WorkspaceAnalysisError {
    fn into_response(self) -> Response {
        let (status, detail, retryable) = match self {
            Self::InvalidQuery => (
                StatusCode::UNPROCESSABLE_ENTITY,
                "Analysis filters are invalid",
                false,
            ),
            Self::InvalidInternalKind => {
                tracing::error!("invalid internal analysis kind");
                (
                    StatusCode::INTERNAL_SERVER_ERROR,
                    "Analysis configuration is invalid",
                    false,
                )
            }
            Self::Database(error) => {
                let failure = crate::db_error::classify(&error, "workspace analysis");
                (failure.status, failure.detail, failure.retryable)
            }
        };
        (
            status,
            Json(WorkspaceAnalysisErrorResponse { detail, retryable }),
        )
            .into_response()
    }
}

#[derive(Serialize)]
struct WorkspaceAnalysisErrorResponse {
    detail: &'static str,
    #[serde(skip_serializing_if = "crate::is_false")]
    retryable: bool,
}
