import type {
  ChannelSourceConfig,
  CollectionSourceType,
  CreateCollectionSourceRequest,
  JobStatus,
  KeywordSourceConfig,
  QuotaBucket,
  VideoSourceConfig,
} from "@monitube/contracts";
import type {
  ActiveSourceJob,
  CollectedVideo,
  CollectionRequestDisposition,
  CollectedSearchScope,
  CommentThreadItem,
  CommentThreadSort,
  SourceSummary,
} from "../../lib/api";

export type ViewMetric = "views" | "likes" | "comments";
export const searchScopeLabels: Record<CollectedSearchScope, string> = { all: "전체", videos: "영상", comments: "댓글" };
export const commentSortLabels: Record<CommentThreadSort, string> = {
  newest: "최신순",
  oldest: "오래된 순",
  recommended: "추천순",
};
export type WorkspacePage = "overview" | "explore" | "sources" | "keywords" | "jobs" | "insights";
export type FormState = {
  sourceType: CollectionSourceType;
  channelInput: string;
  videoInput: string;
  keyword: string;
  publishedAfter: string;
  publishedBefore: string;
  relevanceLanguage: string;
  regionCode: string;
  order: KeywordSourceConfig["order"];
  includeComments: boolean;
};

export type CollectionPreferences = {
  defaultSourceType: "channel" | "keyword";
  includeComments: boolean;
  order: KeywordSourceConfig["order"];
  relevanceLanguage: string;
  regionCode: string;
};

export const defaultCollectionPreferences: CollectionPreferences = {
  defaultSourceType: "channel",
  includeComments: true,
  order: "date",
  relevanceLanguage: "ko",
  regionCode: "KR",
};

export function formFromPreferences(preferences: CollectionPreferences): FormState {
  return {
    sourceType: preferences.defaultSourceType,
    channelInput: "",
    videoInput: "",
    keyword: "",
    publishedAfter: "",
    publishedBefore: "",
    relevanceLanguage: preferences.relevanceLanguage,
    regionCode: preferences.regionCode,
    order: preferences.order,
    includeComments: preferences.includeComments,
  };
}

export const initialForm = formFromPreferences(defaultCollectionPreferences);

export function preferencesStorageKey(username: string) {
  return `monitube:collection-defaults:v1:${encodeURIComponent(username)}`;
}

export function normalizePreferences(value: unknown): CollectionPreferences {
  const record = typeof value === "object" && value !== null && !Array.isArray(value)
    ? value as Record<string, unknown>
    : {};
  const sourceType = record.defaultSourceType;
  const order = record.order;
  const language = typeof record.relevanceLanguage === "string" ? record.relevanceLanguage.trim() : "";
  const region = typeof record.regionCode === "string" ? record.regionCode.trim() : "";
  const validLanguage = language.length >= 2
    && language.length <= 10
    && /^[A-Za-z]{2,3}(?:-[A-Za-z0-9]{2,6})?$/.test(language);
  const validRegion = region.length === 2 && /^[A-Za-z]{2}$/.test(region);
  return {
    defaultSourceType: sourceType === "keyword" ? "keyword" : "channel",
    includeComments: typeof record.includeComments === "boolean"
      ? record.includeComments
      : defaultCollectionPreferences.includeComments,
    order: order === "relevance" || order === "viewCount" ? order : "date",
    relevanceLanguage: validLanguage ? language.toLowerCase() : defaultCollectionPreferences.relevanceLanguage,
    regionCode: validRegion ? region.toUpperCase() : defaultCollectionPreferences.regionCode,
  };
}

export const bucketLabels: Record<QuotaBucket, string> = {
  search_queries: "검색 요청",
  core: "YouTube API",
};

export const stageLabels: Record<string, string> = {
  queued: "수집 대기열에 등록됨",
  resolving_source: "수집 대상을 확인하는 중",
  listing_channel_videos: "채널 업로드 목록을 불러오는 중",
  searching_keywords: "키워드 검색 결과를 불러오는 중",
  fetching_video_details: "동영상 상세 정보를 불러오는 중",
  backfilling_oldest_videos: "누락된 과거 동영상을 오래된 순으로 보완하는 중",
  fetching_comments: "공개 댓글을 불러오는 중",
  persisting: "결과를 저장하는 중",
  analyzing: "분석을 준비하는 중",
  collecting: "수집을 진행하는 중",
  waiting_for_quota: "YouTube API 할당량을 기다리는 중",
  waiting_to_retry: "일시 오류 후 재시도를 기다리는 중",
  completed: "수집 완료",
  failed: "수집 실패",
};

export function clampPositive(value: number, fallback: number) {
  return Number.isFinite(value) && value > 0 ? Math.floor(value) : fallback;
}

export function formatDate(value?: string) {
  if (!value) return "—";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  return new Intl.DateTimeFormat("ko-KR", {
    dateStyle: "medium",
    timeStyle: "short",
    timeZone: "Asia/Seoul",
  }).format(date);
}

export function formatShortDate(value?: string) {
  if (!value) return "—";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  return new Intl.DateTimeFormat("ko-KR", {
    year: "numeric",
    month: "short",
    day: "numeric",
    timeZone: "Asia/Seoul",
  }).format(date);
}

export function formatKpiDate(value?: string) {
  if (!value) return "—";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  return new Intl.DateTimeFormat("ko-KR", {
    month: "short",
    day: "numeric",
    timeZone: "Asia/Seoul",
  }).format(date);
}

export function formatReset(value?: string) {
  return value ? formatDate(value) : "다음 quota window 확인 후";
}

export function collectionStatusValue(job?: JobStatus | null) {
  if (!job) return "대기";
  if (job.state === "waiting_quota") return "할당량 대기";
  if (job.state === "waiting_retry") return "재시도 대기";
  return job.progress.total
    ? `${Math.min(100, Math.round((job.progress.completed / job.progress.total) * 100))}%`
    : statusCopy(job);
}

export function collectionStatusDetail(job?: JobStatus | null) {
  if (!job) return "아직 실행 기록 없음";
  if (job.state === "waiting_quota") {
    return job.resumeIsAutomatic
      ? `${formatReset(job.resumeAt)} 자동 재시도`
      : "관리자 확인 후 재개";
  }
  if (job.state === "waiting_retry") {
    return job.resumeIsAutomatic
      ? `${formatReset(job.resumeAt)} 자동 재시도`
      : "수동 재시도 필요";
  }
  return stageLabels[job.currentStage] ?? job.currentStage;
}

export function formatCount(value?: number) {
  return value === undefined ? "—" : new Intl.NumberFormat("ko-KR").format(value);
}

export function formatDuration(value?: number) {
  if (value === undefined) return "—";
  const hours = Math.floor(value / 3600);
  const minutes = Math.floor((value % 3600) / 60);
  const seconds = value % 60;
  return hours > 0
    ? `${hours}:${String(minutes).padStart(2, "0")}:${String(seconds).padStart(2, "0")}`
    : `${minutes}:${String(seconds).padStart(2, "0")}`;
}

export function youtubeThumbnail(videoId: string) {
  return `https://i.ytimg.com/vi/${encodeURIComponent(videoId)}/hqdefault.jpg`;
}

export function sourceRequest(form: FormState): CreateCollectionSourceRequest {
  if (form.sourceType === "channel") {
    const config: ChannelSourceConfig = {
      input: form.channelInput.trim(),
      includeComments: form.includeComments,
      collectAllVideos: true,
      collectAllComments: form.includeComments,
    };
    return { type: "channel", config };
  }

  if (form.sourceType === "video") {
    const config: VideoSourceConfig = {
      input: form.videoInput.trim(),
      includeComments: form.includeComments,
      collectAllComments: form.includeComments,
    };
    return { type: "video", config };
  }

  const config: KeywordSourceConfig = {
    query: form.keyword.trim(),
    ...(form.publishedAfter ? { publishedAfter: new Date(`${form.publishedAfter}T00:00:00Z`).toISOString() } : {}),
    ...(form.publishedBefore ? { publishedBefore: new Date(`${form.publishedBefore}T23:59:59Z`).toISOString() } : {}),
    ...(form.regionCode.trim() ? { regionCode: form.regionCode.trim().toUpperCase() } : {}),
    ...(form.relevanceLanguage.trim()
      ? { relevanceLanguage: form.relevanceLanguage.trim().toLowerCase() }
      : {}),
    order: form.order,
    includeComments: form.includeComments,
    collectAllComments: form.includeComments,
  };
  return { type: "keyword", config };
}

export function validate(requestBody: CreateCollectionSourceRequest) {
  if (requestBody.type === "channel" && !(requestBody.config as ChannelSourceConfig).input) {
    return "채널 URL, @handle 또는 UC로 시작하는 채널 ID를 입력하세요.";
  }
  if (requestBody.type === "keyword" && !(requestBody.config as KeywordSourceConfig).query) {
    return "수집할 키워드를 입력하세요.";
  }
  if (requestBody.type === "video" && !(requestBody.config as VideoSourceConfig).input) {
    return "YouTube 동영상 URL 또는 동영상 ID를 입력하세요.";
  }
  return null;
}

export function statusCopy(job: JobStatus) {
  if (job.state === "waiting_quota") return "Quota 대기 중";
  if (job.state === "waiting_retry") return "재시도 대기 중";
  if (job.state === "running") return "수집 진행 중";
  if (job.state === "completed") return "수집 완료";
  if (job.state === "completed_with_warnings") return "경고와 함께 완료";
  if (job.state === "failed") return "수집 실패";
  if (job.state === "cancelled") return "취소됨";
  return "대기열에 추가됨";
}

export function sourceTypeCopy(type: string) {
  if (type === "channel") return "채널";
  if (type === "keyword") return "키워드";
  if (type === "video") return "동영상";
  return "수집 대상";
}

export function sourceLabel(source: SourceSummary) {
  const input = source.config.input;
  const query = source.config.query;
  const value = typeof query === "string" ? query : typeof input === "string" ? input : source.id;
  return `${sourceTypeCopy(source.type)} · ${value}`;
}

export function searchFieldLabel(field: string) {
  return ({ title: "제목", description: "설명", channel: "채널", handle: "채널 ID", comment: "댓글", videoTitle: "영상 제목" } as Record<string, string>)[field] ?? field;
}

export function sourceCoverage(source?: SourceSummary) {
  if (!source) return "아직 수집 대상이 선택되지 않았습니다.";
  const config = source.config;
  const coverage = source.coverage ?? {};
  const comments = coverage.includeComments === true
    || coverage.requestedIncludeComments === true
    || config.includeComments === true;
  const commentPages = clampPositive(
    Number(coverage.maxCommentPagesPerVideo ?? coverage.requestedMaxCommentPagesPerVideo ?? config.maxCommentPagesPerVideo),
    1,
  );
  if (source.type === "channel") {
    if (coverage.collectAllVideos === true || config.collectAllVideos === true) {
      return comments
        ? "채널의 전체 공개 동영상 · 전체 공개 댓글 수집"
        : "채널의 전체 공개 동영상 수집";
    }
    const videos = clampPositive(Number(coverage.maxVideos ?? coverage.requestedMaxVideos ?? config.maxVideos), 50);
    return `영상 최대 ${formatCount(videos)}개 · ${comments ? `댓글 ${formatCount(commentPages)}페이지` : "댓글 미수집"}`;
  }
  if (source.type === "keyword") {
    return `검색 결과 전체 · ${comments ? (coverage.collectAllComments === true || config.collectAllComments === true ? "전체 공개 댓글" : `댓글 ${formatCount(commentPages)}페이지`) : "댓글 미수집"}`;
  }
  return comments
    ? (coverage.collectAllComments === true || config.collectAllComments === true ? "전체 공개 댓글" : `공개 댓글 ${formatCount(commentPages)}페이지`)
    : "동영상 메타데이터";
}

export function normalizedSourceIdentity(source: SourceSummary) {
  if (source.targetId) return `target:${source.targetId}`;
  if (source.canonicalKey) return `key:${source.canonicalKey}`;

  const raw = typeof source.config.query === "string"
    ? source.config.query
    : typeof source.config.input === "string"
      ? source.config.input
      : source.id;
  const normalized = raw
    .trim()
    .toLowerCase()
    .replace(/^https?:\/\/(?:www\.)?youtube\.com\/@/, "@")
    .replace(/^https?:\/\/(?:www\.)?youtube\.com\/channel\//, "")
    .replace(/\/+$/, "");
  return `${source.type}:${normalized}`;
}

export function sourceCoverageScore(source: SourceSummary) {
  const config = source.config;
  const maxVideos = config.collectAllVideos === true ? Number.MAX_SAFE_INTEGER : clampPositive(Number(config.maxVideos), 1);
  const maxPages = source.type === "keyword" ? Number.MAX_SAFE_INTEGER : clampPositive(Number(config.maxPagesPerRun), 1);
  const commentPages = config.includeComments === true
    ? config.collectAllComments === true ? Number.MAX_SAFE_INTEGER : clampPositive(Number(config.maxCommentPagesPerVideo), 1)
    : 0;
  return maxVideos * 10_000 + maxPages * 1_000 + commentPages * 10;
}

export function dedupeSources(sources: SourceSummary[]) {
  const grouped = new Map<string, SourceSummary>();
  for (const source of sources) {
    const key = normalizedSourceIdentity(source);
    const previous = grouped.get(key);
    if (!previous) {
      grouped.set(key, source);
      continue;
    }
    const previousScore = sourceCoverageScore(previous);
    const nextScore = sourceCoverageScore(source);
    const previousUpdated = Date.parse(previous.lastCompletedAt ?? previous.nextRunAt ?? "");
    const nextUpdated = Date.parse(source.lastCompletedAt ?? source.nextRunAt ?? "");
    if (nextScore > previousScore || (nextScore === previousScore && nextUpdated > previousUpdated)) {
      grouped.set(key, source);
    }
  }
  return [...grouped.values()];
}

export function collectionNotice(disposition: CollectionRequestDisposition) {
  if (disposition === "cached") return "이미 충분히 수집된 데이터를 표시합니다. 새 YouTube API 호출은 만들지 않았습니다.";
  if (disposition === "joined") return "같은 수집 대상의 진행 중 작업에 연결했습니다.";
  if (disposition === "successor_queued") return "현재 수집이 끝난 뒤 범위를 확장하는 작업을 하나 예약했습니다.";
  return "공유 수집 작업을 대기열에 추가했습니다.";
}

export function idempotencyKey() {
  if (typeof crypto !== "undefined" && typeof crypto.randomUUID === "function") return crypto.randomUUID();
  return `collection-${Date.now()}-${Math.random().toString(36).slice(2)}`;
}

export function sourceScope(source?: SourceSummary) {
  if (!source) return "수집 대상을 선택하면 범위가 표시됩니다.";
  const config = source.config;
  if (source.type === "keyword") {
    const terms = [
      typeof config.query === "string" ? `“${config.query}”` : "키워드 검색",
      typeof config.relevanceLanguage === "string" ? config.relevanceLanguage.toUpperCase() : undefined,
      typeof config.regionCode === "string" ? config.regionCode.toUpperCase() : undefined,
    ].filter(Boolean);
    return [...terms, sourceCoverage(source)].join(" · ");
  }
  const input = typeof config.input === "string" ? config.input : source.id;
  return `${sourceTypeCopy(source.type)} · ${input} · ${sourceCoverage(source)}`;
}

export function isTerminalJob(job: JobStatus | null | undefined) {
  return job ? ["completed", "completed_with_warnings", "failed", "cancelled"].includes(job.state) : false;
}

export function isAbortError(error: unknown) {
  return error instanceof DOMException && error.name === "AbortError";
}

export function activeJobKey(entry: ActiveSourceJob) {
  return `${entry.sourceId}:${entry.job.id}`;
}

export function activeJobPollingDelay(entries: ActiveSourceJob[], failureCount = 0) {
  if (typeof document !== "undefined" && document.visibilityState === "hidden") return 30_000;
  if (failureCount > 0) {
    const backoff = Math.min(60_000, 5_000 * (2 ** Math.min(failureCount - 1, 4)));
    return Math.round(backoff * (0.8 + Math.random() * 0.4));
  }
  if (entries.length === 0) return 15_000;
  const waitingJobs = entries.filter(({ job }) => job.state === "waiting_quota" || job.state === "waiting_retry");
  if (waitingJobs.length !== entries.length) return 5_000;
  const nextResumeIn = Math.min(...waitingJobs.map(({ job }) => {
    const resumeAt = job.resumeAt ? new Date(job.resumeAt).getTime() : Number.NaN;
    return Number.isFinite(resumeAt) ? Math.max(0, resumeAt - Date.now()) : 30_000;
  }));
  return Math.min(60_000, Math.max(15_000, nextResumeIn));
}

export function mergeCommentThreads(current: CommentThreadItem[], incoming: CommentThreadItem[]) {
  const seen = new Set(current.map((item) => item.comment.id));
  return [...current, ...incoming.filter((item) => !seen.has(item.comment.id))];
}

export function videoMetric(video: CollectedVideo, metric: ViewMetric) {
  if (metric === "likes") return video.likeCount ?? 0;
  if (metric === "comments") return video.commentCount ?? 0;
  return video.viewCount ?? 0;
}

export function metricLabel(metric: ViewMetric) {
  if (metric === "likes") return "좋아요";
  if (metric === "comments") return "YouTube 댓글";
  return "조회";
}

export function sourceTargetValue(source: SourceSummary) {
  const input = source.config.input;
  const query = source.config.query;
  return typeof query === "string" ? query : typeof input === "string" ? input : source.canonicalKey ?? "—";
}

export function sourceCollectionState(source: SourceSummary) {
  if (!source.enabled) return { label: "일시 정지", tone: "idle" };
  const job = source.latestJob;
  if (!job) return { label: "정지", tone: "idle" };
  if (job.state === "failed") return { label: "실패", tone: "failed" };
  if (job.state === "running" || job.state === "queued" || job.state === "waiting_quota" || job.state === "waiting_retry") {
    return { label: "진행 중", tone: "running" };
  }
  return { label: "완료", tone: "completed" };
}

export function jobFailureReason(job?: JobStatus | null) {
  if (!job || job.state !== "failed") return null;
  return job.pauseReason
    ?? job.partialErrors.find((error) => error.message)?.message
    ?? job.partialErrors[0]?.code
    ?? "실패 사유가 기록되지 않았습니다.";
}
