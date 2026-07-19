import type {
  CreateCollectionSourceRequest,
  JobStatus,
} from "@monitube/contracts";

export interface StartJobRequest {
  include_comments: boolean;
  max_videos?: number;
  max_comments_per_video?: number;
}

export interface SourceSummary {
  id: string;
  type: string;
  enabled: boolean;
  config: Record<string, unknown>;
  nextRunAt?: string;
  targetId?: string;
  canonicalKey?: string;
  coverage?: Record<string, unknown>;
  lastCompletedAt?: string;
  latestJob?: JobStatus;
}

export interface AuthUser { username: string; }

export type CollectionRequestDisposition = "cached" | "joined" | "queued" | "successor_queued";

export interface CollectionRequestResponse {
  id: string;
  disposition: CollectionRequestDisposition;
  targetId: string;
  source: SourceSummary;
  job?: JobStatus;
}

export interface CollectedVideo {
  id: string;
  youtubeVideoId: string;
  channelId?: string;
  title: string;
  description?: string;
  publishedAt?: string;
  viewCount?: number;
  likeCount?: number;
  commentCount?: number;
  durationSeconds?: number;
  privacyStatus?: string;
  madeForKids?: boolean;
  fetchedAt?: string;
}

export interface TopWord {
  label: string;
  count?: number;
}

export interface CommentSummary {
  total: number;
  latestPublishedAt?: string;
  topWords: TopWord[];
}

export interface SourceAnalysis {
  videoCount?: number;
  commentCount?: number;
  latestVideoPublishedAt?: string;
  latestCommentPublishedAt?: string;
  topWords: TopWord[];
  generatedAt?: string;
  asOfJobId?: string;
  dataVersion?: number;
  status?: "fresh" | "stale" | "building" | "failed";
  topWordsStatus?: "fresh" | "stale" | "building" | "failed";
  partialData?: boolean;
  coverage?: Record<string, unknown>;
}

export type SourceVideoMetric = "views" | "likes" | "comments";
export type SourceTopVideos = Record<SourceVideoMetric, CollectedVideo[]>;

export interface SourceResults {
  source: SourceSummary;
  latestJob?: JobStatus;
  videos: CollectedVideo[];
  commentSummary?: CommentSummary;
  analysis?: SourceAnalysis;
  topVideos?: SourceTopVideos;
  videosNextCursor?: string;
  videosSnapshotAt?: string;
  videosTotal?: number;
  videoPagination?: "cursor" | "legacy";
}

export interface SourceOverview {
  source: SourceSummary;
  latestJob?: JobStatus;
  summary?: SourceAnalysis;
  topVideos: SourceTopVideos;
}

export interface PagedSourceVideos {
  videos: CollectedVideo[];
  nextCursor?: string;
  snapshotAt?: string;
  total: number;
}

export interface ActiveSourceJob {
  sourceId: string;
  targetId?: string;
  job: JobStatus;
}

export interface RecentJobFailure {
  sourceId: string;
  targetId?: string;
  sourceType: string;
  sourceLabel: string;
  failedAt: string;
  reason: string;
  errorCode?: string;
  retryable?: boolean;
  failedChildCount: number;
  job: JobStatus;
}

export interface CollectedComment {
  id: string;
  text: string;
  videoId?: string;
  parentCommentId?: string;
  threadId?: string;
  publishedAt?: string;
  likeCount?: number;
  authorName?: string;
  authorChannelId?: string;
}

export interface CommentThreadItem {
  comment: CollectedComment;
  repliesPreview: CollectedComment[];
  storedReplyCount: number;
}

export type CommentThreadSort = "newest" | "oldest" | "recommended";

export interface PagedCommentThreads {
  sort: CommentThreadSort;
  items: CommentThreadItem[];
  nextCursor?: string;
}

export interface PagedCommentReplies {
  comments: CollectedComment[];
  nextCursor?: string;
}

export interface CommentDetailData {
  comment: CollectedComment;
  parentComment?: CollectedComment;
  storedReplyCount: number;
  video: CollectedVideo;
  replies: CollectedComment[];
  authorComments: Array<{
    comment: CollectedComment;
    video: CollectedVideo;
    channelTitle?: string;
  }>;
}

export interface TargetPin {
  targetId: string;
  enabled: boolean;
  intervalMinutes: number;
  nextRunAt: string;
  lastDispatchedAt?: string;
}

export interface ExploreChannel {
  youtubeChannelId: string;
  handle?: string;
  title?: string;
  description?: string;
  thumbnailUrl?: string;
  subscriberCount?: number;
  viewCount?: number;
  youtubeVideoCount?: number;
  hiddenSubscriberCount?: boolean;
  videoCount: number;
  commentCount: number;
  youtubeCommentCount: number;
  videoCollectionRate: number;
  commentCollectionRate: number;
  lastFetchedAt?: string;
  targetId?: string;
  pin?: TargetPin;
}

export interface ExploreData {
  channels: ExploreChannel[];
  videos: CollectedVideo[];
  nextOffset?: number;
}

export interface ChannelSubscriberSnapshot {
  fetchedAt: string;
  subscriberCount?: number;
  hiddenSubscriberCount?: boolean;
}

export interface CollectedSearchVideo {
  video: CollectedVideo;
  score: number;
  matchedFields: string[];
}

export interface CollectedSearchComment {
  comment: CollectedComment;
  video: CollectedVideo;
  channelTitle?: string;
  score: number;
  matchedFields: string[];
}

export interface CollectedSearchData {
  query: string;
  videos: CollectedSearchVideo[];
  comments: CollectedSearchComment[];
}

export type CollectedSearchScope = "all" | "videos" | "comments";

const defaultApiBaseUrl = "http://localhost:8000";

function configuredBaseUrl() {
  const value = process.env.NEXT_PUBLIC_API_BASE_URL?.trim() || defaultApiBaseUrl;
  return value.replace(/\/+$/, "");
}

export function apiBaseUrl() {
  return configuredBaseUrl();
}

class ApiError extends Error {
  readonly status: number;

  constructor(message: string, status: number) {
    super(message);
    this.name = "ApiError";
    this.status = status;
  }
}

function asRecord(value: unknown): Record<string, unknown> | null {
  return typeof value === "object" && value !== null && !Array.isArray(value)
    ? value as Record<string, unknown>
    : null;
}

function asArray(value: unknown): unknown[] {
  return Array.isArray(value) ? value : [];
}

function asTextArray(value: unknown): string[] {
  return asArray(value).flatMap((item) => {
    const text = asText(item);
    return text ? [text] : [];
  });
}

function asText(value: unknown): string | undefined {
  if (typeof value !== "string") return undefined;
  const text = value.trim();
  return text || undefined;
}

function asNumber(value: unknown): number | undefined {
  if (typeof value === "number" && Number.isFinite(value)) return value;
  if (typeof value === "string" && value.trim()) {
    const parsed = Number(value);
    return Number.isFinite(parsed) ? parsed : undefined;
  }
  return undefined;
}

function asBoolean(value: unknown): boolean | undefined {
  return typeof value === "boolean" ? value : undefined;
}

function firstArray(record: Record<string, unknown>, keys: string[]) {
  for (const key of keys) {
    if (Array.isArray(record[key])) return record[key] as unknown[];
  }
  return [];
}

function normalizeSource(value: unknown): SourceSummary | null {
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

function normalizePin(value: unknown): TargetPin | undefined {
  const record = asRecord(value);
  const targetId = asText(record?.targetId ?? record?.target_id);
  const intervalMinutes = asNumber(record?.intervalMinutes ?? record?.interval_minutes);
  const nextRunAt = asText(record?.nextRunAt ?? record?.next_run_at);
  if (!targetId || intervalMinutes === undefined || !nextRunAt) return undefined;
  return { targetId, enabled: record?.enabled !== false, intervalMinutes, nextRunAt,
    ...(asText(record?.lastDispatchedAt ?? record?.last_dispatched_at) ? { lastDispatchedAt: asText(record?.lastDispatchedAt ?? record?.last_dispatched_at) } : {}) };
}

function normalizeExploreChannel(value: unknown): ExploreChannel | null {
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

function normalizeJob(value: unknown): JobStatus | undefined {
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

function normalizeVideo(value: unknown): CollectedVideo | null {
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

function normalizeTopWords(value: unknown): TopWord[] {
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

function normalizeAnalysis(value: unknown): SourceAnalysis | undefined {
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

function normalizeTopVideos(value: unknown): SourceTopVideos {
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

function normalizeCommentSummary(value: unknown): CommentSummary | undefined {
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

function normalizeComment(value: unknown): CollectedComment | null {
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

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(`${configuredBaseUrl()}${path}`, {
    ...init,
    credentials: "include",
    headers: {
      Accept: "application/json",
      "Content-Type": "application/json",
      ...init?.headers,
    },
  });

  const contentType = response.headers.get("content-type") ?? "";
  const body: unknown = contentType.includes("application/json")
    ? await response.json()
    : await response.text();

  if (!response.ok) {
    const detail =
      typeof body === "object" && body !== null && "detail" in body
        ? String(body.detail)
        : `요청에 실패했습니다. (HTTP ${response.status})`;
    throw new ApiError(detail, response.status);
  }

  return body as T;
}

export async function getCurrentUser(): Promise<AuthUser | null> {
  try {
    const response = await request<unknown>("/v1/auth/me", { method: "GET" });
    const username = asText(asRecord(response)?.username);
    return username ? { username } : null;
  } catch (error) {
    if (error instanceof ApiError && error.status === 401) return null;
    throw error;
  }
}

export async function login(username: string, password: string): Promise<AuthUser> {
  const response = await request<unknown>("/v1/auth/login", { method: "POST", body: JSON.stringify({ username, password }) });
  const value = asText(asRecord(response)?.username);
  if (!value) throw new ApiError("로그인 응답을 해석할 수 없습니다.", 502);
  return { username: value };
}

export async function register(username: string, password: string): Promise<AuthUser> {
  const response = await request<unknown>("/v1/auth/register", { method: "POST", body: JSON.stringify({ username, password }) });
  const value = asText(asRecord(response)?.username);
  if (!value) throw new ApiError("회원 생성 응답을 해석할 수 없습니다.", 502);
  return { username: value };
}

export async function createSource(requestBody: CreateCollectionSourceRequest) {
  const response = await request<unknown>("/v1/sources", {
    method: "POST",
    body: JSON.stringify(requestBody),
  });

  const source = normalizeSource(response);
  if (!source) throw new ApiError("수집 source 응답을 해석할 수 없습니다.", 502);
  return source;
}

/**
 * Update the current user's subscription settings for a collection target.
 *
 * A source ID in the browser API is deliberately a user-scoped subscription
 * ID.  Toggling it must not change another user's shared collection target or
 * its worker schedule.
 */
export async function updateSource(
  sourceId: string,
  payload: { enabled?: boolean },
): Promise<SourceSummary> {
  const response = await request<unknown>(`/v1/sources/${encodeURIComponent(sourceId)}`, {
    method: "PATCH",
    body: JSON.stringify(payload),
  });
  const source = normalizeSource(response);
  if (!source) throw new ApiError("수집 대상 업데이트 응답을 해석하지 못했습니다.", 502);
  return source;
}

export async function startJob(sourceId: string, requestBody: StartJobRequest) {
  return request<JobStatus>(
    `/v1/sources/${encodeURIComponent(sourceId)}/jobs`,
    {
      method: "POST",
      body: JSON.stringify(requestBody),
    },
  );
}

export async function createCollectionRequest(
  requestBody: CreateCollectionSourceRequest,
  options: { forceRefresh?: boolean; idempotencyKey?: string } = {},
): Promise<CollectionRequestResponse> {
  const response = await request<unknown>("/v1/collection-requests", {
    method: "POST",
    headers: options.idempotencyKey ? { "Idempotency-Key": options.idempotencyKey } : undefined,
    body: JSON.stringify({
      ...requestBody,
      ...(options.forceRefresh ? { forceRefresh: true } : {}),
    }),
  });
  const record = asRecord(response);
  const source = normalizeSource(record?.source);
  const id = asText(record?.id);
  const targetId = asText(record?.targetId ?? record?.target_id);
  const disposition = asText(record?.disposition);

  if (!record || !id || !targetId || !source || !disposition) {
    throw new ApiError("수집 요청 응답을 해석할 수 없습니다.", 502);
  }
  if (!["cached", "joined", "queued", "successor_queued"].includes(disposition)) {
    throw new ApiError("알 수 없는 수집 요청 상태입니다.", 502);
  }

  return {
    id,
    targetId,
    disposition: disposition as CollectionRequestDisposition,
    source,
    ...(normalizeJob(record.job) ? { job: normalizeJob(record.job) } : {}),
  };
}

export async function getJob(jobId: string, signal?: AbortSignal): Promise<JobStatus> {
  const response = await request<unknown>(`/v1/jobs/${encodeURIComponent(jobId)}`, { method: "GET", signal });
  const job = normalizeJob(response);
  if (!job) throw new ApiError("작업 상태 응답을 해석할 수 없습니다.", 502);
  return job;
}

export async function listActiveJobs(signal?: AbortSignal): Promise<ActiveSourceJob[]> {
  const response = await request<unknown>("/v1/jobs/active", { method: "GET", signal });
  const record = asRecord(response);
  return firstArray(record ?? {}, ["jobs", "items", "data"]).flatMap((item) => {
    const itemRecord = asRecord(item);
    const sourceId = asText(itemRecord?.sourceId ?? itemRecord?.source_id);
    const job = normalizeJob(itemRecord?.job ?? item);
    if (!sourceId || !job) return [];
    const targetId = asText(itemRecord?.targetId ?? itemRecord?.target_id);
    return [{ sourceId, ...(targetId ? { targetId } : {}), job }];
  });
}

export async function listRecentJobFailures(limit = 10, signal?: AbortSignal): Promise<RecentJobFailure[]> {
  const safeLimit = Number.isFinite(limit) ? Math.max(1, Math.min(50, Math.floor(limit))) : 10;
  const response = await request<unknown>(`/v1/jobs/recent-failures?limit=${safeLimit}`, { method: "GET", signal });
  const record = asRecord(response);
  const values = Array.isArray(response) ? response : firstArray(record ?? {}, ["failures", "items", "data"]);
  return values.flatMap((item) => {
    const itemRecord = asRecord(item);
    const sourceId = asText(itemRecord?.sourceId ?? itemRecord?.source_id);
    const failedAt = asText(itemRecord?.failedAt ?? itemRecord?.failed_at);
    const reason = asText(itemRecord?.reason);
    const retryable = asBoolean(itemRecord?.retryable);
    const failedChildCount = asNumber(itemRecord?.failedChildCount ?? itemRecord?.failed_child_count);
    const job = normalizeJob(itemRecord?.job);
    if (!sourceId || !failedAt || !reason || failedChildCount === undefined || !job) return [];
    const targetId = asText(itemRecord?.targetId ?? itemRecord?.target_id);
    const errorCode = asText(itemRecord?.errorCode ?? itemRecord?.error_code);
    return [{
      sourceId,
      ...(targetId ? { targetId } : {}),
      sourceType: asText(itemRecord?.sourceType ?? itemRecord?.source_type) ?? "unknown",
      sourceLabel: asText(itemRecord?.sourceLabel ?? itemRecord?.source_label) ?? sourceId,
      failedAt,
      reason,
      ...(errorCode ? { errorCode } : {}),
      ...(retryable === undefined ? {} : { retryable }),
      failedChildCount: Math.max(0, Math.floor(failedChildCount)),
      job,
    }];
  });
}

export async function listSourceJobs(sourceId: string): Promise<JobStatus[]> {
  const response = await request<unknown>(`/v1/sources/${encodeURIComponent(sourceId)}/jobs`, { method: "GET" });
  return (Array.isArray(response) ? response : []).flatMap((job) => {
    const normalized = normalizeJob(job);
    return normalized ? [normalized] : [];
  });
}

export async function listSources() {
  const response = await request<unknown>("/v1/sources", { method: "GET" });
  const record = asRecord(response);
  const sourceValues = Array.isArray(response)
    ? response
    : firstArray(record ?? {}, ["sources", "items", "data"]);
  return sourceValues.flatMap((source) => {
    const normalized = normalizeSource(source);
    return normalized ? [normalized] : [];
  });
}

export async function deleteSource(sourceId: string): Promise<void> {
  await request<void>(`/v1/sources/${encodeURIComponent(sourceId)}`, { method: "DELETE" });
}

export async function getSourceResults(sourceId: string, signal?: AbortSignal): Promise<SourceResults> {
  const response = await request<unknown>(`/v1/sources/${encodeURIComponent(sourceId)}/results`, {
    method: "GET",
    signal,
  });
  const record = asRecord(response) ?? {};
  const source = normalizeSource(record.source) ?? {
    id: sourceId,
    type: "unknown",
    enabled: true,
    config: {},
  };
  const videoValues = firstArray(record, ["videos", "items", "results"]);

  const directSummary = normalizeCommentSummary(record.commentSummary ?? record.comment_summary);
  const analysis = asRecord(record.analysis);
  const analysisSummary = normalizeAnalysis(analysis);
  const analysisTopWords = analysisSummary?.topWords ?? [];
  const commentSummary = directSummary
    ? {
        ...directSummary,
        topWords: directSummary.topWords.length > 0 ? directSummary.topWords : analysisTopWords,
      }
    : analysis
      ? {
          total: asNumber(analysis.commentCount ?? analysis.comment_count) ?? 0,
          ...(asText(analysis.latestCommentPublishedAt ?? analysis.latest_comment_published_at)
            ? { latestPublishedAt: asText(analysis.latestCommentPublishedAt ?? analysis.latest_comment_published_at) }
            : {}),
          topWords: analysisTopWords,
        }
      : undefined;

  return {
    source,
    ...(normalizeJob(record.latestJob ?? record.latest_job)
      ? { latestJob: normalizeJob(record.latestJob ?? record.latest_job) }
      : {}),
    videos: videoValues.flatMap((video) => {
      const normalized = normalizeVideo(video);
      return normalized ? [normalized] : [];
    }),
    ...(commentSummary ? { commentSummary } : {}),
    ...(analysisSummary ? { analysis: analysisSummary } : {}),
    videoPagination: "legacy",
  };
}

export async function getSourceOverview(sourceId: string, signal?: AbortSignal): Promise<SourceOverview> {
  const response = await request<unknown>(`/v1/sources/${encodeURIComponent(sourceId)}/overview`, {
    method: "GET",
    signal,
  });
  const record = asRecord(response) ?? {};
  const source = normalizeSource(record.source);
  if (!source) throw new ApiError("수집 대상 개요를 해석할 수 없습니다.", 502);
  const latestJob = normalizeJob(record.latestJob ?? record.latest_job);
  const summary = normalizeAnalysis(record.summary ?? record.analysis);
  return {
    source,
    ...(latestJob ? { latestJob } : {}),
    ...(summary ? { summary } : {}),
    topVideos: normalizeTopVideos(record.topVideos ?? record.top_videos),
  };
}

export async function getSourceVideos(
  sourceId: string,
  options: { cursor?: string; limit?: number; signal?: AbortSignal } = {},
): Promise<PagedSourceVideos> {
  const query = new URLSearchParams({ limit: String(Math.min(100, Math.max(1, options.limit ?? 60))) });
  if (options.cursor) query.set("cursor", options.cursor);
  const response = await request<unknown>(
    `/v1/sources/${encodeURIComponent(sourceId)}/videos?${query.toString()}`,
    { method: "GET", signal: options.signal },
  );
  const record = asRecord(response) ?? {};
  const videos = firstArray(record, ["videos", "items", "results"]).flatMap((video) => {
    const normalized = normalizeVideo(video);
    return normalized ? [normalized] : [];
  });
  const nextCursor = asText(record.nextCursor ?? record.next_cursor);
  const snapshotAt = asText(record.snapshotAt ?? record.snapshot_at);
  return {
    videos,
    ...(nextCursor ? { nextCursor } : {}),
    ...(snapshotAt ? { snapshotAt } : {}),
    total: asNumber(record.total ?? record.totalCount ?? record.total_count) ?? videos.length,
  };
}

function isUnavailableAdditiveEndpoint(error: unknown) {
  return error instanceof ApiError && [404, 405, 501].includes(error.status);
}

/**
 * Load the bounded source workspace when the additive API is available. During
 * rolling deployments an older API may not expose it yet, so only unsupported
 * endpoint responses fall back to the legacy, unbounded results contract.
 */
export async function getSourceWorkspace(sourceId: string, signal?: AbortSignal): Promise<SourceResults> {
  try {
    const [overview, page] = await Promise.all([
      getSourceOverview(sourceId, signal),
      getSourceVideos(sourceId, { signal }),
    ]);
    const commentSummary = overview.summary
      ? {
          total: overview.summary.commentCount ?? 0,
          ...(overview.summary.latestCommentPublishedAt
            ? { latestPublishedAt: overview.summary.latestCommentPublishedAt }
            : {}),
          topWords: overview.summary.topWords,
        }
      : undefined;
    return {
      source: overview.source,
      ...(overview.latestJob ? { latestJob: overview.latestJob } : {}),
      videos: page.videos,
      ...(commentSummary ? { commentSummary } : {}),
      ...(overview.summary ? { analysis: overview.summary } : {}),
      topVideos: overview.topVideos,
      ...(page.nextCursor ? { videosNextCursor: page.nextCursor } : {}),
      ...(page.snapshotAt ? { videosSnapshotAt: page.snapshotAt } : {}),
      videosTotal: page.total,
      videoPagination: "cursor",
    };
  } catch (error) {
    if (!isUnavailableAdditiveEndpoint(error)) throw error;
    return getSourceResults(sourceId, signal);
  }
}

export async function getVideoCommentThreads(
  videoId: string,
  sort: CommentThreadSort = "newest",
  cursor?: string,
): Promise<PagedCommentThreads> {
  const query = new URLSearchParams({ limit: "20", sort });
  if (cursor) query.set("cursor", cursor);
  const response = await request<unknown>(`/v1/videos/${encodeURIComponent(videoId)}/comment-threads?${query.toString()}`, {
    method: "GET",
  });
  const record = asRecord(response) ?? {};
  const nextCursor = asText(record?.nextCursor ?? record?.next_cursor ?? record?.nextPageToken ?? record?.next_page_token);
  const responseSort = asText(record.sort);

  return {
    sort: responseSort === "oldest" || responseSort === "recommended" ? responseSort : "newest",
    items: firstArray(record, ["items", "threads", "results"]).flatMap((item) => {
      const itemRecord = asRecord(item);
      const comment = normalizeComment(itemRecord?.comment);
      if (!comment) return [];
      const repliesPreview = firstArray(itemRecord ?? {}, ["repliesPreview", "replies_preview", "replies"]).flatMap((reply) => {
        const normalized = normalizeComment(reply);
        return normalized ? [normalized] : [];
      });
      return [{
        comment,
        repliesPreview,
        storedReplyCount: asNumber(itemRecord?.storedReplyCount ?? itemRecord?.stored_reply_count) ?? repliesPreview.length,
      }];
    }),
    ...(nextCursor ? { nextCursor } : {}),
  };
}

export async function getCommentReplies(commentId: string, cursor?: string): Promise<PagedCommentReplies> {
  const query = new URLSearchParams({ limit: "20" });
  if (cursor) query.set("cursor", cursor);
  const response = await request<unknown>(`/v1/comments/${encodeURIComponent(commentId)}/replies?${query.toString()}`, {
    method: "GET",
  });
  const record = asRecord(response) ?? {};
  const nextCursor = asText(record.nextCursor ?? record.next_cursor);
  return {
    comments: firstArray(record, ["comments", "items", "replies"]).flatMap((comment) => {
      const normalized = normalizeComment(comment);
      return normalized ? [normalized] : [];
    }),
    ...(nextCursor ? { nextCursor } : {}),
  };
}

export async function getCommentDetail(commentId: string): Promise<CommentDetailData> {
  const response = await request<unknown>(`/v1/comments/${encodeURIComponent(commentId)}`, { method: "GET" });
  const record = asRecord(response) ?? {};
  const comment = normalizeComment(record.comment);
  const parentComment = normalizeComment(record.parentComment ?? record.parent_comment);
  const video = normalizeVideo(record.video);
  if (!comment || !video) throw new ApiError("댓글 상세 정보를 해석하지 못했습니다.", 500);

  return {
    comment,
    ...(parentComment ? { parentComment } : {}),
    storedReplyCount: asNumber(record.storedReplyCount ?? record.stored_reply_count) ?? 0,
    video,
    replies: firstArray(record, ["replies", "replyComments", "reply_comments"]).flatMap((item) => {
      const itemRecord = asRecord(item);
      const reply = normalizeComment(itemRecord?.comment ?? item);
      return reply ? [reply] : [];
    }),
    authorComments: firstArray(record, ["authorComments", "author_comments"]).flatMap((item) => {
      const itemRecord = asRecord(item);
      const relatedComment = normalizeComment(itemRecord?.comment);
      const relatedVideo = normalizeVideo(itemRecord?.video);
      if (!relatedComment || !relatedVideo) return [];
      const channelTitle = asText(itemRecord?.channelTitle ?? itemRecord?.channel_title);
      return [{ comment: relatedComment, video: relatedVideo, ...(channelTitle ? { channelTitle } : {}) }];
    }),
  };
}

export async function getExplore(channelId?: string, offset = 0, limit = 60): Promise<ExploreData> {
  const query = new URLSearchParams({ offset: String(offset), limit: String(limit) });
  if (channelId) query.set("channelId", channelId);
  const response = await request<unknown>(`/v1/explore?${query.toString()}`, { method: "GET" });
  const record = asRecord(response);
  return {
    channels: firstArray(record ?? {}, ["channels"]).flatMap((item) => {
      const channel = normalizeExploreChannel(item);
      return channel ? [channel] : [];
    }),
    videos: firstArray(record ?? {}, ["videos"]).flatMap((item) => {
      const video = normalizeVideo(item);
      return video ? [video] : [];
    }),
    ...(asNumber(record?.nextOffset ?? record?.next_offset) !== undefined ? { nextOffset: asNumber(record?.nextOffset ?? record?.next_offset) } : {}),
  };
}

export async function getChannelSubscriberHistory(channelId: string): Promise<ChannelSubscriberSnapshot[]> {
  const response = await request<unknown>(`/v1/channels/${encodeURIComponent(channelId)}/subscriber-history`, { method: "GET" });
  return (Array.isArray(response) ? response : []).flatMap((item) => {
    const record = asRecord(item);
    const fetchedAt = asText(record?.fetchedAt ?? record?.fetched_at);
    if (!fetchedAt) return [];
    return [{
      fetchedAt,
      ...(asNumber(record?.subscriberCount ?? record?.subscriber_count) !== undefined ? { subscriberCount: asNumber(record?.subscriberCount ?? record?.subscriber_count) } : {}),
      ...(asBoolean(record?.hiddenSubscriberCount ?? record?.hidden_subscriber_count) !== undefined ? { hiddenSubscriberCount: asBoolean(record?.hiddenSubscriberCount ?? record?.hidden_subscriber_count) } : {}),
    }];
  });
}

export async function searchCollected(query: string, scope: CollectedSearchScope = "all"): Promise<CollectedSearchData> {
  const response = await request<unknown>(`/v1/search?q=${encodeURIComponent(query)}&scope=${encodeURIComponent(scope)}&limit=20`, { method: "GET" });
  const record = asRecord(response);
  return {
    query: asText(record?.query) ?? query,
    videos: firstArray(record ?? {}, ["videos"]).flatMap((item) => {
      const itemRecord = asRecord(item);
      const video = normalizeVideo(itemRecord?.video);
      const score = asNumber(itemRecord?.score);
      if (!video || score === undefined) return [];
      return [{ video, score, matchedFields: asTextArray(firstArray(itemRecord ?? {}, ["matchedFields", "matched_fields"])) }];
    }),
    comments: firstArray(record ?? {}, ["comments"]).flatMap((item) => {
      const itemRecord = asRecord(item);
      const comment = normalizeComment(itemRecord?.comment);
      const video = normalizeVideo(itemRecord?.video);
      const score = asNumber(itemRecord?.score);
      if (!comment || !video || score === undefined) return [];
      return [{
        comment, video, score,
        matchedFields: asTextArray(firstArray(itemRecord ?? {}, ["matchedFields", "matched_fields"])),
        ...(asText(itemRecord?.channelTitle ?? itemRecord?.channel_title) ? { channelTitle: asText(itemRecord?.channelTitle ?? itemRecord?.channel_title) } : {}),
      }];
    }),
  };
}

export async function updateTargetPin(targetId: string, payload: { enabled: boolean; intervalMinutes: number }): Promise<TargetPin> {
  const response = await request<unknown>(`/v1/collection-targets/${encodeURIComponent(targetId)}/pin`, {
    method: "PUT", body: JSON.stringify(payload),
  });
  const pin = normalizePin(response);
  if (!pin) throw new ApiError("핀 상태를 해석하지 못했습니다.", 502);
  return pin;
}

export { ApiError };
