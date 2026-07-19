import type { JobStatus } from "@monitube/contracts";
import type {
  CollectedComment,
  CollectedVideo,
  CommentSummary,
  ExploreChannel,
  SourceAnalysis,
  SourceSummary,
  SourceTopVideos,
  SourceVideoMetric,
  TargetPin,
  TopWord,
} from "./types";

export function asRecord(value: unknown): Record<string, unknown> | null {
  return typeof value === "object" && value !== null && !Array.isArray(value)
    ? value as Record<string, unknown>
    : null;
}

export function asArray(value: unknown): unknown[] {
  return Array.isArray(value) ? value : [];
}

export function asTextArray(value: unknown): string[] {
  return asArray(value).flatMap((item) => {
    const text = asText(item);
    return text ? [text] : [];
  });
}

export function asText(value: unknown): string | undefined {
  if (typeof value !== "string") return undefined;
  const text = value.trim();
  return text || undefined;
}

export function asNumber(value: unknown): number | undefined {
  if (typeof value === "number" && Number.isFinite(value)) return value;
  if (typeof value === "string" && value.trim()) {
    const parsed = Number(value);
    return Number.isFinite(parsed) ? parsed : undefined;
  }
  return undefined;
}

export function asBoolean(value: unknown): boolean | undefined {
  return typeof value === "boolean" ? value : undefined;
}

export function firstArray(record: Record<string, unknown>, keys: string[]) {
  for (const key of keys) {
    if (Array.isArray(record[key])) return record[key] as unknown[];
  }
  return [];
}

export function normalizeSource(value: unknown): SourceSummary | null {
  const record = asRecord(value);
  const id = asText(record?.id);
  if (!record || !id) return null;

  return {
    id,
    type: asText(record.type) ?? "unknown",
    enabled: record.enabled !== false,
    config: asRecord(record.config) ?? {},
    ...(asText(record.nextRunAt ?? record.next_run_at)
      ? { nextRunAt: asText(record.nextRunAt ?? record.next_run_at) }
      : {}),
    ...(asText(record.targetId ?? record.target_id)
      ? { targetId: asText(record.targetId ?? record.target_id) }
      : {}),
    ...(asText(record.canonicalKey ?? record.canonical_key)
      ? { canonicalKey: asText(record.canonicalKey ?? record.canonical_key) }
      : {}),
    ...(asRecord(record.coverage) ? { coverage: asRecord(record.coverage) ?? {} } : {}),
    ...(asText(record.lastCompletedAt ?? record.last_completed_at)
      ? { lastCompletedAt: asText(record.lastCompletedAt ?? record.last_completed_at) }
      : {}),
    ...(normalizeJob(record.latestJob ?? record.latest_job)
      ? { latestJob: normalizeJob(record.latestJob ?? record.latest_job) }
      : {}),
  };
}
export function normalizePin(value: unknown): TargetPin | undefined {
  const record = asRecord(value);
  const targetId = asText(record?.targetId ?? record?.target_id);
  const intervalMinutes = asNumber(record?.intervalMinutes ?? record?.interval_minutes);
  const nextRunAt = asText(record?.nextRunAt ?? record?.next_run_at);
  if (!targetId || intervalMinutes === undefined || !nextRunAt) return undefined;
  return { targetId, enabled: record?.enabled !== false, intervalMinutes, nextRunAt,
    ...(asText(record?.lastDispatchedAt ?? record?.last_dispatched_at) ? { lastDispatchedAt: asText(record?.lastDispatchedAt ?? record?.last_dispatched_at) } : {}) };
}

export function normalizeExploreChannel(value: unknown): ExploreChannel | null {
  const record = asRecord(value);
  const youtubeChannelId = asText(record?.youtubeChannelId ?? record?.youtube_channel_id);
  if (!record || !youtubeChannelId) return null;
  const pin = normalizePin(record.pin);
  return {
    youtubeChannelId, videoCount: asNumber(record.videoCount ?? record.video_count) ?? 0,
    commentCount: asNumber(record.commentCount ?? record.comment_count) ?? 0,
    youtubeCommentCount: asNumber(record.youtubeCommentCount ?? record.youtube_comment_count) ?? 0,
    videoCollectionRate: asNumber(record.videoCollectionRate ?? record.video_collection_rate) ?? 0,
    commentCollectionRate: asNumber(record.commentCollectionRate ?? record.comment_collection_rate) ?? 0,
    ...(asText(record.handle) ? { handle: asText(record.handle) } : {}),
    ...(asText(record.title) ? { title: asText(record.title) } : {}),
    ...(asText(record.description) ? { description: asText(record.description) } : {}),
    ...(asText(record.thumbnailUrl ?? record.thumbnail_url) ? { thumbnailUrl: asText(record.thumbnailUrl ?? record.thumbnail_url) } : {}),
    ...(asNumber(record.subscriberCount ?? record.subscriber_count) !== undefined ? { subscriberCount: asNumber(record.subscriberCount ?? record.subscriber_count) } : {}),
    ...(asNumber(record.viewCount ?? record.view_count) !== undefined ? { viewCount: asNumber(record.viewCount ?? record.view_count) } : {}),
    ...(asNumber(record.youtubeVideoCount ?? record.youtube_video_count) !== undefined ? { youtubeVideoCount: asNumber(record.youtubeVideoCount ?? record.youtube_video_count) } : {}),
    ...(asBoolean(record.hiddenSubscriberCount ?? record.hidden_subscriber_count) !== undefined ? { hiddenSubscriberCount: asBoolean(record.hiddenSubscriberCount ?? record.hidden_subscriber_count) } : {}),
    ...(asText(record.lastFetchedAt ?? record.last_fetched_at) ? { lastFetchedAt: asText(record.lastFetchedAt ?? record.last_fetched_at) } : {}),
    ...(asText(record.targetId ?? record.target_id) ? { targetId: asText(record.targetId ?? record.target_id) } : {}),
    ...(pin ? { pin } : {}),
  };
}

export function normalizeJob(value: unknown): JobStatus | undefined {
  const record = asRecord(value);
  const id = asText(record?.id);
  const state = asText(record?.state);
  if (!record || !id || !state) return undefined;

  const progress = asRecord(record.progress);
  const completed = asNumber(progress?.completed ?? record.progressCompleted ?? record.progress_completed) ?? 0;
  const total = asNumber(progress?.total ?? record.progressTotal ?? record.progress_total);
  const unit = asText(progress?.unit ?? record.progressUnit ?? record.progress_unit) ?? "sources";
  const phaseProgress = (phaseValue: unknown, fallbackUnit: "videos" | "comments") => {
    const phase = asRecord(phaseValue);
    if (!phase) return undefined;
    const phaseCompleted = asNumber(phase.completed) ?? 0;
    const phaseTotal = asNumber(phase.total);
    return {
      completed: phaseCompleted,
      ...(phaseTotal === undefined ? {} : { total: phaseTotal }),
      unit: (asText(phase.unit) ?? fallbackUnit) as JobStatus["progress"]["unit"],
    };
  };

  return {
    id,
    state: state as JobStatus["state"],
    currentStage: asText(record.currentStage ?? record.current_stage) ?? "queued",
    progress: {
      completed,
      ...(total === undefined ? {} : { total }),
      unit: unit as JobStatus["progress"]["unit"],
    },
    ...(phaseProgress(record.videoProgress ?? record.video_progress, "videos")
      ? { videoProgress: phaseProgress(record.videoProgress ?? record.video_progress, "videos") }
      : {}),
    ...(phaseProgress(record.commentProgress ?? record.comment_progress, "comments")
      ? { commentProgress: phaseProgress(record.commentProgress ?? record.comment_progress, "comments") }
      : {}),
    ...(asText(record.pauseReason ?? record.pause_reason)
      ? { pauseReason: asText(record.pauseReason ?? record.pause_reason) }
      : {}),
    ...(asText(record.quotaBucket ?? record.quota_bucket)
      ? { quotaBucket: asText(record.quotaBucket ?? record.quota_bucket) as JobStatus["quotaBucket"] }
      : {}),
    ...(asText(record.resumeAt ?? record.resume_at)
      ? { resumeAt: asText(record.resumeAt ?? record.resume_at) }
      : {}),
    resumeIsAutomatic: record.resumeIsAutomatic === true || record.resume_is_automatic === true,
    partialErrors: Array.isArray(record.partialErrors ?? record.partial_errors)
      ? (record.partialErrors ?? record.partial_errors) as JobStatus["partialErrors"]
      : [],
  };
}

export function normalizeVideo(value: unknown): CollectedVideo | null {
  const record = asRecord(value);
  if (!record) return null;

  const id = asText(record.id ?? record.videoId ?? record.video_id ?? record.youtubeVideoId ?? record.youtube_video_id);
  if (!id) return null;
  const statistics = asRecord(record.statistics) ?? {};
  const viewCount = asNumber(record.viewCount ?? record.view_count ?? record.views ?? statistics.viewCount ?? statistics.view_count);
  const likeCount = asNumber(record.likeCount ?? record.like_count ?? record.likes ?? statistics.likeCount ?? statistics.like_count);
  const commentCount = asNumber(record.commentCount ?? record.comment_count ?? record.comments ?? statistics.commentCount ?? statistics.comment_count);

  return {
    id,
    youtubeVideoId: asText(record.youtubeVideoId ?? record.youtube_video_id ?? record.videoId ?? record.video_id) ?? id,
    ...(asText(record.channelId ?? record.channel_id ?? record.youtubeChannelId ?? record.youtube_channel_id)
      ? { channelId: asText(record.channelId ?? record.channel_id ?? record.youtubeChannelId ?? record.youtube_channel_id) }
      : {}),
    title: asText(record.title ?? record.name) ?? "제목 없는 동영상",
    ...(asText(record.description ?? record.summary) ? { description: asText(record.description ?? record.summary) } : {}),
    ...(asText(record.publishedAt ?? record.published_at ?? record.published) ? {
      publishedAt: asText(record.publishedAt ?? record.published_at ?? record.published),
    } : {}),
    ...(viewCount === undefined ? {} : { viewCount }),
    ...(likeCount === undefined ? {} : { likeCount }),
    ...(commentCount === undefined ? {} : { commentCount }),
    ...(asNumber(record.durationSeconds ?? record.duration_seconds) === undefined
      ? {}
      : { durationSeconds: asNumber(record.durationSeconds ?? record.duration_seconds) }),
    ...(asText(record.privacyStatus ?? record.privacy_status)
      ? { privacyStatus: asText(record.privacyStatus ?? record.privacy_status) }
      : {}),
    ...(asBoolean(record.madeForKids ?? record.made_for_kids) === undefined
      ? {}
      : { madeForKids: asBoolean(record.madeForKids ?? record.made_for_kids) }),
    ...(asText(record.fetchedAt ?? record.fetched_at)
      ? { fetchedAt: asText(record.fetchedAt ?? record.fetched_at) }
      : {}),
  };
}

export function normalizeTopWords(value: unknown): TopWord[] {
  return asArray(value).flatMap((entry) => {
    const simple = asText(entry);
    if (simple) return [{ label: simple }];

    const record = asRecord(entry);
    const label = asText(record?.label ?? record?.word ?? record?.text);
    if (!label) return [];
    const count = asNumber(record?.count ?? record?.value);
    return [{ label, ...(count === undefined ? {} : { count }) }];
  });
}

export function normalizeAnalysis(value: unknown): SourceAnalysis | undefined {
  const analysis = asRecord(value);
  if (!analysis) return undefined;

  const status = asText(analysis.status);
  const topWordsStatus = asText(analysis.topWordsStatus ?? analysis.top_words_status);
  return {
    ...(asNumber(analysis.videoCount ?? analysis.video_count) === undefined
      ? {}
      : { videoCount: asNumber(analysis.videoCount ?? analysis.video_count) }),
    ...(asNumber(analysis.commentCount ?? analysis.comment_count) === undefined
      ? {}
      : { commentCount: asNumber(analysis.commentCount ?? analysis.comment_count) }),
    ...(asText(analysis.latestVideoPublishedAt ?? analysis.latest_video_published_at)
      ? { latestVideoPublishedAt: asText(analysis.latestVideoPublishedAt ?? analysis.latest_video_published_at) }
      : {}),
    ...(asText(analysis.latestCommentPublishedAt ?? analysis.latest_comment_published_at)
      ? { latestCommentPublishedAt: asText(analysis.latestCommentPublishedAt ?? analysis.latest_comment_published_at) }
      : {}),
    topWords: normalizeTopWords(analysis.topWords ?? analysis.top_words),
    ...(asText(analysis.generatedAt ?? analysis.generated_at)
      ? { generatedAt: asText(analysis.generatedAt ?? analysis.generated_at) }
      : {}),
    ...(asText(analysis.asOfJobId ?? analysis.as_of_job_id)
      ? { asOfJobId: asText(analysis.asOfJobId ?? analysis.as_of_job_id) }
      : {}),
    ...(asNumber(analysis.dataVersion ?? analysis.data_version) === undefined
      ? {}
      : { dataVersion: asNumber(analysis.dataVersion ?? analysis.data_version) }),
    ...(["fresh", "stale", "building", "failed"].includes(status ?? "")
      ? { status: status as SourceAnalysis["status"] }
      : {}),
    ...(["fresh", "stale", "building", "failed"].includes(topWordsStatus ?? "")
      ? { topWordsStatus: topWordsStatus as SourceAnalysis["topWordsStatus"] }
      : {}),
    ...(asBoolean(analysis.partialData ?? analysis.partial_data) === undefined
      ? {}
      : { partialData: asBoolean(analysis.partialData ?? analysis.partial_data) }),
    ...(asRecord(analysis.coverage) ? { coverage: asRecord(analysis.coverage) ?? {} } : {}),
  };
}

export function normalizeTopVideos(value: unknown): SourceTopVideos {
  const record = asRecord(value) ?? {};
  const normalizeList = (metric: SourceVideoMetric) => asArray(record[metric]).flatMap((video) => {
    const normalized = normalizeVideo(video);
    return normalized ? [normalized] : [];
  });
  return {
    views: normalizeList("views"),
    likes: normalizeList("likes"),
    comments: normalizeList("comments"),
  };
}

export function normalizeCommentSummary(value: unknown): CommentSummary | undefined {
  const record = asRecord(value);
  if (!record) return undefined;

  const total = asNumber(record.total ?? record.totalCount ?? record.total_count) ?? 0;
  return {
    total,
    ...(asText(record.latestPublishedAt ?? record.latest_published_at)
      ? { latestPublishedAt: asText(record.latestPublishedAt ?? record.latest_published_at) }
      : {}),
    topWords: normalizeTopWords(record.topWords ?? record.top_words),
  };
}

export function normalizeComment(value: unknown): CollectedComment | null {
  const record = asRecord(value);
  if (!record) return null;
  const id = asText(record.id ?? record.commentId ?? record.comment_id);
  const text = asText(record.text ?? record.textDisplay ?? record.text_display ?? record.body ?? record.message) ?? "내용이 제공되지 않았습니다.";
  if (!id) return null;

  return {
    id,
    text,
    ...(asText(record.videoId ?? record.video_id) ? { videoId: asText(record.videoId ?? record.video_id) } : {}),
    ...(asText(record.parentCommentId ?? record.parent_comment_id)
      ? { parentCommentId: asText(record.parentCommentId ?? record.parent_comment_id) }
      : {}),
    ...(asText(record.threadId ?? record.thread_id) ? { threadId: asText(record.threadId ?? record.thread_id) } : {}),
    ...(asText(record.publishedAt ?? record.published_at ?? record.published)
      ? { publishedAt: asText(record.publishedAt ?? record.published_at ?? record.published) }
      : {}),
    ...(asNumber(record.likeCount ?? record.like_count ?? record.likes) === undefined
      ? {}
      : { likeCount: asNumber(record.likeCount ?? record.like_count ?? record.likes) }),
    ...(asText(record.authorName ?? record.author_name ?? record.authorDisplayName ?? record.author_display_name)
      ? { authorName: asText(record.authorName ?? record.author_name ?? record.authorDisplayName ?? record.author_display_name) }
      : {}),
    ...(asText(record.authorChannelId ?? record.author_channel_id ?? asRecord(record.authorChannelId ?? record.author_channel_id)?.value)
      ? { authorChannelId: asText(record.authorChannelId ?? record.author_channel_id ?? asRecord(record.authorChannelId ?? record.author_channel_id)?.value) }
      : {}),
  };
}
