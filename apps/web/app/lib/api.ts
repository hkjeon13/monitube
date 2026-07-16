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
}

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
  title: string;
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
}

export interface SourceResults {
  source: SourceSummary;
  latestJob?: JobStatus;
  videos: CollectedVideo[];
  commentSummary?: CommentSummary;
  analysis?: SourceAnalysis;
}

export interface CollectedComment {
  id: string;
  text: string;
  publishedAt?: string;
  likeCount?: number;
  authorName?: string;
}

export interface PagedComments {
  comments: CollectedComment[];
  nextCursor?: string;
}

const defaultApiBaseUrl = "http://localhost:8000";
const defaultTimeoutMs = 10_000;

function configuredBaseUrl() {
  const value = process.env.NEXT_PUBLIC_API_BASE_URL?.trim() || defaultApiBaseUrl;
  return value.replace(/\/+$/, "");
}

function configuredTimeout() {
  const value = Number(process.env.NEXT_PUBLIC_API_REQUEST_TIMEOUT_MS);
  return Number.isFinite(value) && value > 0 ? value : defaultTimeoutMs;
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

  return {
    id,
    state: state as JobStatus["state"],
    currentStage: asText(record.currentStage ?? record.current_stage) ?? "queued",
    progress: {
      completed,
      ...(total === undefined ? {} : { total }),
      unit: unit as JobStatus["progress"]["unit"],
    },
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
    title: asText(record.title ?? record.name) ?? "제목 없는 동영상",
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
    ...(asText(record.publishedAt ?? record.published_at ?? record.published)
      ? { publishedAt: asText(record.publishedAt ?? record.published_at ?? record.published) }
      : {}),
    ...(asNumber(record.likeCount ?? record.like_count ?? record.likes) === undefined
      ? {}
      : { likeCount: asNumber(record.likeCount ?? record.like_count ?? record.likes) }),
    ...(asText(record.authorName ?? record.author_name ?? record.authorDisplayName ?? record.author_display_name)
      ? { authorName: asText(record.authorName ?? record.author_name ?? record.authorDisplayName ?? record.author_display_name) }
      : {}),
  };
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const controller = new AbortController();
  const timeout = window.setTimeout(() => controller.abort(), configuredTimeout());

  try {
    const response = await fetch(`${configuredBaseUrl()}${path}`, {
      ...init,
      headers: {
        Accept: "application/json",
        "Content-Type": "application/json",
        ...init?.headers,
      },
      signal: controller.signal,
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
  } finally {
    window.clearTimeout(timeout);
  }
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

export async function getJob(jobId: string) {
  return request<JobStatus>(`/v1/jobs/${encodeURIComponent(jobId)}`, { method: "GET" });
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

export async function getSourceResults(sourceId: string): Promise<SourceResults> {
  const response = await request<unknown>(`/v1/sources/${encodeURIComponent(sourceId)}/results`, {
    method: "GET",
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
  const analysisTopWords = normalizeTopWords(analysis?.topWords ?? analysis?.top_words);
  const analysisSummary = analysis
    ? {
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
        topWords: analysisTopWords,
        ...(asText(analysis.generatedAt ?? analysis.generated_at)
          ? { generatedAt: asText(analysis.generatedAt ?? analysis.generated_at) }
          : {}),
      }
    : undefined;
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
  };
}

export async function getVideoComments(videoId: string, cursor?: string): Promise<PagedComments> {
  const query = cursor ? `?cursor=${encodeURIComponent(cursor)}` : "";
  const response = await request<unknown>(`/v1/videos/${encodeURIComponent(videoId)}/comments${query}`, {
    method: "GET",
  });
  const record = asRecord(response);
  const commentValues = Array.isArray(response)
    ? response
    : firstArray(record ?? {}, ["comments", "items", "results", "data"]);
  const nextCursor = asText(record?.nextCursor ?? record?.next_cursor ?? record?.nextPageToken ?? record?.next_page_token);

  return {
    comments: commentValues.flatMap((comment) => {
      const normalized = normalizeComment(comment);
      return normalized ? [normalized] : [];
    }),
    ...(nextCursor ? { nextCursor } : {}),
  };
}

export { ApiError };
