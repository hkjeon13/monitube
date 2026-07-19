"use client";

import {
  ArrowLeftIcon,
  ArrowPathIcon,
  CheckCircleIcon,
  ChevronRightIcon,
  ClockIcon,
  DocumentChartBarIcon,
  EllipsisHorizontalIcon,
  ExclamationTriangleIcon,
  FolderIcon,
  HomeIcon,
  InformationCircleIcon,
  MagnifyingGlassIcon,
  PlayIcon,
  PlusIcon,
  QueueListIcon,
  Cog6ToothIcon,
  SparklesIcon,
  Squares2X2Icon,
  XMarkIcon,
} from "@heroicons/react/24/outline";
import Link from "next/link";
import { useRouter } from "next/navigation";
import type {
  ChannelSourceConfig,
  CollectionSourceType,
  CreateCollectionSourceRequest,
  JobStatus,
  KeywordSourceConfig,
  QuotaBucket,
  VideoSourceConfig,
} from "@monitube/contracts";
import type { FormEvent, MouseEvent, ReactNode } from "react";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";

import {
  ApiError,
  createCollectionRequest,
  deleteSource,
  getChannelSubscriberHistory,
  getCurrentUser,
  getJob,
  listSourceJobs,
  getExplore,
  getCommentDetail,
  searchCollected,
  getSourceVideos,
  getSourceWorkspace,
  getVideoCommentThreads,
  listActiveJobs,
  listRecentJobFailures,
  listSources,
  login,
  register,
  updateSource,
  type CommentThreadItem,
  type CommentThreadSort,
  type CollectedVideo,
  type CollectionRequestDisposition,
  type CollectedSearchData,
  type CollectedSearchScope,
  type ChannelSubscriberSnapshot,
  type CommentDetailData,
  type ExploreData,
  type ActiveSourceJob,
  type RecentJobFailure,
  type SourceResults,
  type SourceSummary,
} from "../lib/api";
import { CommentRow, CommentThread } from "./comment-thread";

type ViewMetric = "views" | "likes" | "comments";
const searchScopeLabels: Record<CollectedSearchScope, string> = { all: "전체", videos: "영상", comments: "댓글" };
const commentSortLabels: Record<CommentThreadSort, string> = {
  newest: "최신순",
  oldest: "오래된 순",
  recommended: "추천순",
};
export type WorkspacePage = "overview" | "explore" | "sources" | "keywords" | "jobs" | "insights";
type FormState = {
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

type CollectionPreferences = {
  defaultSourceType: "channel" | "keyword";
  includeComments: boolean;
  order: KeywordSourceConfig["order"];
  relevanceLanguage: string;
  regionCode: string;
};

const defaultCollectionPreferences: CollectionPreferences = {
  defaultSourceType: "channel",
  includeComments: true,
  order: "date",
  relevanceLanguage: "ko",
  regionCode: "KR",
};

function formFromPreferences(preferences: CollectionPreferences): FormState {
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

const initialForm = formFromPreferences(defaultCollectionPreferences);

function preferencesStorageKey(username: string) {
  return `monitube:collection-defaults:v1:${encodeURIComponent(username)}`;
}

function normalizePreferences(value: unknown): CollectionPreferences {
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

const bucketLabels: Record<QuotaBucket, string> = {
  search_queries: "검색 요청",
  core: "YouTube API",
};

const stageLabels: Record<string, string> = {
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

const sourceTypeChoices = [
  { type: "channel" as const, label: "채널", detail: "업로드 동영상 기준", Icon: FolderIcon },
  { type: "keyword" as const, label: "키워드", detail: "검색 run별 발견 결과", Icon: MagnifyingGlassIcon },
];

function clampPositive(value: number, fallback: number) {
  return Number.isFinite(value) && value > 0 ? Math.floor(value) : fallback;
}

function formatDate(value?: string) {
  if (!value) return "—";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  return new Intl.DateTimeFormat("ko-KR", {
    dateStyle: "medium",
    timeStyle: "short",
    timeZone: "Asia/Seoul",
  }).format(date);
}

function formatShortDate(value?: string) {
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

function formatKpiDate(value?: string) {
  if (!value) return "—";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  return new Intl.DateTimeFormat("ko-KR", {
    month: "short",
    day: "numeric",
    timeZone: "Asia/Seoul",
  }).format(date);
}

function formatReset(value?: string) {
  return value ? formatDate(value) : "다음 quota window 확인 후";
}

function collectionStatusValue(job?: JobStatus | null) {
  if (!job) return "대기";
  if (job.state === "waiting_quota") return "할당량 대기";
  if (job.state === "waiting_retry") return "재시도 대기";
  return job.progress.total
    ? `${Math.min(100, Math.round((job.progress.completed / job.progress.total) * 100))}%`
    : statusCopy(job);
}

function collectionStatusDetail(job?: JobStatus | null) {
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

function formatCount(value?: number) {
  return value === undefined ? "—" : new Intl.NumberFormat("ko-KR").format(value);
}

function formatDuration(value?: number) {
  if (value === undefined) return "—";
  const hours = Math.floor(value / 3600);
  const minutes = Math.floor((value % 3600) / 60);
  const seconds = value % 60;
  return hours > 0
    ? `${hours}:${String(minutes).padStart(2, "0")}:${String(seconds).padStart(2, "0")}`
    : `${minutes}:${String(seconds).padStart(2, "0")}`;
}

function youtubeThumbnail(videoId: string) {
  return `https://i.ytimg.com/vi/${encodeURIComponent(videoId)}/hqdefault.jpg`;
}

function SubscriberTrend({ samples }: { samples: ChannelSubscriberSnapshot[] }) {
  const visible = samples.filter((sample) => sample.subscriberCount !== undefined && !sample.hiddenSubscriberCount);
  if (visible.length < 2) return <p className="subscriber-trend-empty">구독자 수집 이력이 쌓이면 변동 추이를 보여드립니다.</p>;
  const values = visible.map((sample) => sample.subscriberCount ?? 0);
  const min = Math.min(...values);
  const max = Math.max(...values);
  const range = Math.max(1, max - min);
  const points = visible.map((sample, index) => `${(index / (visible.length - 1)) * 100},${40 - (((sample.subscriberCount ?? min) - min) / range) * 32}`).join(" ");
  const delta = values.at(-1)! - values[0];
  return (
    <section className="subscriber-trend" aria-label="구독자 수 변동 추이">
      <div><span>구독자 추이</span><strong className={delta > 0 ? "subscriber-trend-positive" : ""}>{delta > 0 ? "+" : ""}{formatCount(delta)}</strong></div>
      <svg viewBox="0 0 100 48" role="img" aria-label={`${formatKpiDate(visible[0].fetchedAt)}부터 ${formatKpiDate(visible.at(-1)?.fetchedAt)}까지 구독자 ${formatCount(values[0])}명에서 ${formatCount(values.at(-1))}명`} preserveAspectRatio="none"><polyline points={points} /></svg>
      <small>{formatKpiDate(visible[0].fetchedAt)} · {formatKpiDate(visible.at(-1)?.fetchedAt)}</small>
    </section>
  );
}

function sourceRequest(form: FormState): CreateCollectionSourceRequest {
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

function validate(requestBody: CreateCollectionSourceRequest) {
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

function statusCopy(job: JobStatus) {
  if (job.state === "waiting_quota") return "Quota 대기 중";
  if (job.state === "waiting_retry") return "재시도 대기 중";
  if (job.state === "running") return "수집 진행 중";
  if (job.state === "completed") return "수집 완료";
  if (job.state === "completed_with_warnings") return "경고와 함께 완료";
  if (job.state === "failed") return "수집 실패";
  if (job.state === "cancelled") return "취소됨";
  return "대기열에 추가됨";
}

function sourceTypeCopy(type: string) {
  if (type === "channel") return "채널";
  if (type === "keyword") return "키워드";
  if (type === "video") return "동영상";
  return "수집 대상";
}

function sourceLabel(source: SourceSummary) {
  const input = source.config.input;
  const query = source.config.query;
  const value = typeof query === "string" ? query : typeof input === "string" ? input : source.id;
  return `${sourceTypeCopy(source.type)} · ${value}`;
}

function searchFieldLabel(field: string) {
  return ({ title: "제목", description: "설명", channel: "채널", handle: "채널 ID", comment: "댓글", videoTitle: "영상 제목" } as Record<string, string>)[field] ?? field;
}

function sourceCoverage(source?: SourceSummary) {
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

function normalizedSourceIdentity(source: SourceSummary) {
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

function sourceCoverageScore(source: SourceSummary) {
  const config = source.config;
  const maxVideos = config.collectAllVideos === true ? Number.MAX_SAFE_INTEGER : clampPositive(Number(config.maxVideos), 1);
  const maxPages = source.type === "keyword" ? Number.MAX_SAFE_INTEGER : clampPositive(Number(config.maxPagesPerRun), 1);
  const commentPages = config.includeComments === true
    ? config.collectAllComments === true ? Number.MAX_SAFE_INTEGER : clampPositive(Number(config.maxCommentPagesPerVideo), 1)
    : 0;
  return maxVideos * 10_000 + maxPages * 1_000 + commentPages * 10;
}

function dedupeSources(sources: SourceSummary[]) {
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

function collectionNotice(disposition: CollectionRequestDisposition) {
  if (disposition === "cached") return "이미 충분히 수집된 데이터를 표시합니다. 새 YouTube API 호출은 만들지 않았습니다.";
  if (disposition === "joined") return "같은 수집 대상의 진행 중 작업에 연결했습니다.";
  if (disposition === "successor_queued") return "현재 수집이 끝난 뒤 범위를 확장하는 작업을 하나 예약했습니다.";
  return "공유 수집 작업을 대기열에 추가했습니다.";
}

function idempotencyKey() {
  if (typeof crypto !== "undefined" && typeof crypto.randomUUID === "function") return crypto.randomUUID();
  return `collection-${Date.now()}-${Math.random().toString(36).slice(2)}`;
}

function sourceScope(source?: SourceSummary) {
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

function isTerminalJob(job: JobStatus | null | undefined) {
  return job ? ["completed", "completed_with_warnings", "failed", "cancelled"].includes(job.state) : false;
}

function isAbortError(error: unknown) {
  return error instanceof DOMException && error.name === "AbortError";
}

function activeJobKey(entry: ActiveSourceJob) {
  return `${entry.sourceId}:${entry.job.id}`;
}

function activeJobPollingDelay(entries: ActiveSourceJob[], failureCount = 0) {
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

function mergeCommentThreads(current: CommentThreadItem[], incoming: CommentThreadItem[]) {
  const seen = new Set(current.map((item) => item.comment.id));
  return [...current, ...incoming.filter((item) => !seen.has(item.comment.id))];
}

function videoMetric(video: CollectedVideo, metric: ViewMetric) {
  if (metric === "likes") return video.likeCount ?? 0;
  if (metric === "comments") return video.commentCount ?? 0;
  return video.viewCount ?? 0;
}

function metricLabel(metric: ViewMetric) {
  if (metric === "likes") return "좋아요";
  if (metric === "comments") return "YouTube 댓글";
  return "조회";
}

function StatusPill({ job }: { job?: JobStatus | null }) {
  if (!job) return <span className="status-pill status-idle">수집 기록 없음</span>;
  const Icon = job.state === "completed"
    ? CheckCircleIcon
    : job.state === "failed" || job.state === "cancelled"
      ? ExclamationTriangleIcon
      : job.state === "waiting_quota" || job.state === "waiting_retry"
        ? ClockIcon
        : ArrowPathIcon;
  return (
    <span className={`status-pill status-${job.state}`}>
      <Icon aria-hidden="true" />
      {statusCopy(job)}
    </span>
  );
}

function sourceTargetValue(source: SourceSummary) {
  const input = source.config.input;
  const query = source.config.query;
  return typeof query === "string" ? query : typeof input === "string" ? input : source.canonicalKey ?? "—";
}

function sourceCollectionState(source: SourceSummary) {
  if (!source.enabled) return { label: "일시 정지", tone: "idle" };
  const job = source.latestJob;
  if (!job) return { label: "정지", tone: "idle" };
  if (job.state === "failed") return { label: "실패", tone: "failed" };
  if (job.state === "running" || job.state === "queued" || job.state === "waiting_quota" || job.state === "waiting_retry") {
    return { label: "진행 중", tone: "running" };
  }
  return { label: "완료", tone: "completed" };
}

function SourceCollectionState({ source }: { source: SourceSummary }) {
  const state = sourceCollectionState(source);
  return (
    <span className={`source-progress source-progress-state source-progress-state-${state.tone}`}>{state.label}</span>
  );
}

function jobFailureReason(job?: JobStatus | null) {
  if (!job || job.state !== "failed") return null;
  return job.pauseReason
    ?? job.partialErrors.find((error) => error.message)?.message
    ?? job.partialErrors[0]?.code
    ?? "실패 사유가 기록되지 않았습니다.";
}

function MetricCard({
  label,
  value,
  detail,
  icon,
  accent = false,
  failure = false,
  onClick,
}: {
  label: string;
  value: string;
  detail: string;
  icon: ReactNode;
  accent?: boolean;
  failure?: boolean;
  onClick?: () => void;
}) {
  const className = `${accent ? "metric-card metric-card-accent" : "metric-card"}${failure ? " metric-card-failure" : ""}`;
  const content = <><div className="metric-card-head"><span>{label}</span><span className="metric-icon" aria-hidden="true">{icon}</span></div><strong>{value}</strong><small>{detail}</small></>;
  return (
    onClick ? <button type="button" className={`${className} metric-card-button`} onClick={onClick}>{content}</button> : <article className={className}>{content}</article>
  );
}

function LoginScreen({ onAuthenticated }: { onAuthenticated: (username: string) => void }) {
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [mode, setMode] = useState<"login" | "register">("login");
  const [error, setError] = useState<string | null>(null);
  const [isSubmitting, setIsSubmitting] = useState(false);
  const submit = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    setIsSubmitting(true);
    setError(null);
    try {
      const user = mode === "login" ? await login(username, password) : await register(username, password);
      onAuthenticated(user.username);
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "로그인할 수 없습니다.");
    } finally {
      setIsSubmitting(false);
    }
  };
  return <main className="login-page"><section className="login-card"><div className="brand-lockup"><span className="brand-mark"><PlayIcon /></span><span>monitube</span></div><p className="section-kicker">PRIVATE COLLECTION WORKSPACE</p><h1>{mode === "login" ? "로그인" : "계정 만들기"}</h1><p>아이디와 비밀번호만 저장합니다. 수집 데이터는 로그인한 계정별로 분리됩니다.</p><form onSubmit={submit}><label>아이디<input value={username} onChange={(event) => setUsername(event.target.value)} minLength={3} maxLength={32} pattern="[A-Za-z0-9_-]+" autoComplete="username" required /></label><label>비밀번호<input value={password} onChange={(event) => setPassword(event.target.value)} minLength={8} maxLength={256} type="password" autoComplete={mode === "login" ? "current-password" : "new-password"} required /></label>{error && <p className="inline-error">{error}</p>}<button className="primary-action" type="submit" disabled={isSubmitting}>{isSubmitting ? "확인 중…" : mode === "login" ? "로그인" : "계정 생성"}</button></form><button className="login-mode-switch" type="button" onClick={() => { setMode((current) => current === "login" ? "register" : "login"); setError(null); }}>{mode === "login" ? "새 계정 만들기" : "이미 계정이 있습니다"}</button></section></main>;
}

export function CollectionWorkbench({ page = "overview" }: { page?: WorkspacePage }) {
  const router = useRouter();
  const [requestedSourceId, setRequestedSourceId] = useState<string | null>(null);
  const [authUser, setAuthUser] = useState<string | null | undefined>(undefined);
  const [form, setForm] = useState<FormState>(initialForm);
  const [preferences, setPreferences] = useState<CollectionPreferences>(defaultCollectionPreferences);
  const [settingsDraft, setSettingsDraft] = useState<CollectionPreferences>(defaultCollectionPreferences);
  const [isSettingsOpen, setIsSettingsOpen] = useState(false);
  const [notice, setNotice] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [isStarting, setIsStarting] = useState(false);
  const [job, setJob] = useState<JobStatus | null>(null);
  const [jobSourceId, setJobSourceId] = useState<string | null>(null);
  const [sources, setSources] = useState<SourceSummary[]>([]);
  const [activeSourceId, setActiveSourceId] = useState("");
  const [isSourcesLoading, setIsSourcesLoading] = useState(false);
  const [sourceResults, setSourceResults] = useState<SourceResults | null>(null);
  const [isResultsLoading, setIsResultsLoading] = useState(false);
  const [resultsError, setResultsError] = useState<string | null>(null);
  const [isSourceVideosLoadingMore, setIsSourceVideosLoadingMore] = useState(false);
  const [sourceVideosError, setSourceVideosError] = useState<string | null>(null);
  const [selectedVideo, setSelectedVideo] = useState<CollectedVideo | null>(null);
  const [commentThreads, setCommentThreads] = useState<CommentThreadItem[]>([]);
  const [commentSort, setCommentSort] = useState<CommentThreadSort>("newest");
  const [nextCommentsCursor, setNextCommentsCursor] = useState<string | undefined>();
  const [isCommentsLoading, setIsCommentsLoading] = useState(false);
  const [commentsError, setCommentsError] = useState<string | null>(null);
  const [selectedCommentId, setSelectedCommentId] = useState<string | null>(null);
  const [selectedCommentDetail, setSelectedCommentDetail] = useState<CommentDetailData | null>(null);
  const [isCommentDetailLoading, setIsCommentDetailLoading] = useState(false);
  const [commentDetailError, setCommentDetailError] = useState<string | null>(null);
  const [isCollectionOpen, setIsCollectionOpen] = useState(false);
  const [viewMetric, setViewMetric] = useState<ViewMetric>("views");
  const [explore, setExplore] = useState<ExploreData>({ channels: [], videos: [] });
  const [isExploreLoading, setIsExploreLoading] = useState(false);
  const [isExploreLoadingMore, setIsExploreLoadingMore] = useState(false);
  const [exploreError, setExploreError] = useState<string | null>(null);
  const [updatingSourceId, setUpdatingSourceId] = useState<string | null>(null);
  const [deletingSourceId, setDeletingSourceId] = useState<string | null>(null);
  const [openSourceMenuId, setOpenSourceMenuId] = useState<string | null>(null);
  const [exploreChannelId, setExploreChannelId] = useState<string | null>(null);
  const [subscriberHistory, setSubscriberHistory] = useState<ChannelSubscriberSnapshot[]>([]);
  const [exploreVisibleCount, setExploreVisibleCount] = useState(12);
  const [searchQuery, setSearchQuery] = useState("");
  const [searchScope, setSearchScope] = useState<CollectedSearchScope>("all");
  const [submittedSearchQuery, setSubmittedSearchQuery] = useState("");
  const [submittedSearchScope, setSubmittedSearchScope] = useState<CollectedSearchScope>("all");
  const [searchRun, setSearchRun] = useState(0);
  const [searchResults, setSearchResults] = useState<CollectedSearchData | null>(null);
  const [isSearchLoading, setIsSearchLoading] = useState(false);
  const [searchError, setSearchError] = useState<string | null>(null);
  const [recentFailures, setRecentFailures] = useState<RecentJobFailure[]>([]);
  const [isRecentFailuresLoading, setIsRecentFailuresLoading] = useState(false);
  const [recentFailuresError, setRecentFailuresError] = useState<string | null>(null);
  const resultsRequest = useRef(0);
  const resultsAbortController = useRef<AbortController | null>(null);
  const sourceVideosAbortController = useRef<AbortController | null>(null);
  const sourceVideosInFlightRef = useRef(false);
  const commentsRequest = useRef(0);
  const collectionTriggerRef = useRef<HTMLElement | null>(null);
  const settingsTriggerRef = useRef<HTMLElement | null>(null);
  const videoTriggerRef = useRef<HTMLElement | null>(null);
  const commentTriggerRef = useRef<HTMLElement | null>(null);
  const collectionDrawerRef = useRef<HTMLElement | null>(null);
  const settingsDrawerRef = useRef<HTMLElement | null>(null);
  const videoModalRef = useRef<HTMLElement | null>(null);
  const commentThreadsLoadMoreRef = useRef<HTMLDivElement | null>(null);
  const sourceVideosLoadMoreRef = useRef<HTMLDivElement | null>(null);
  const exploreLoadMoreRef = useRef<HTMLDivElement | null>(null);
  const appliedSourceQueryRef = useRef<string | null>(null);
  const searchRequest = useRef(0);
  const sourcesRef = useRef<SourceSummary[]>([]);
  const activeSourceIdRef = useRef("");
  const localJobRef = useRef<{ sourceId: string; job: JobStatus } | null>(null);
  const activeJobsRef = useRef<Map<string, ActiveSourceJob>>(new Map());
  const activeJobsEndpointSupportedRef = useRef<boolean | null>(null);
  const handledTerminalJobsRef = useRef<Set<string>>(new Set());
  const wakeActiveJobsPollRef = useRef<(() => void) | null>(null);
  const authUserRef = useRef<string | null | undefined>(undefined);
  const recentFailuresAbortControllerRef = useRef<AbortController | null>(null);
  const recentFailuresGenerationRef = useRef(0);

  const changeAuthUser = useCallback((nextUser: string | null) => {
    authUserRef.current = nextUser;
    recentFailuresAbortControllerRef.current?.abort();
    recentFailuresAbortControllerRef.current = null;
    recentFailuresGenerationRef.current += 1;
    setRecentFailures([]);
    setRecentFailuresError(null);
    setIsRecentFailuresLoading(false);
    setIsSettingsOpen(false);
    setAuthUser(nextUser);
  }, []);

  useEffect(() => {
    void getCurrentUser().then((user) => changeAuthUser(user?.username ?? null)).catch(() => changeAuthUser(null));
  }, [changeAuthUser]);

  useEffect(() => {
    if (authUser === undefined) return;
    let next = defaultCollectionPreferences;
    if (authUser) {
      try {
        const stored = window.localStorage.getItem(preferencesStorageKey(authUser));
        if (stored) next = normalizePreferences(JSON.parse(stored));
      } catch {
        next = defaultCollectionPreferences;
      }
    }
    setPreferences(next);
    setSettingsDraft(next);
    setForm(formFromPreferences(next));
  }, [authUser]);

  const requestBody = useMemo(() => sourceRequest(form), [form]);
  const validationError = useMemo(() => validate(requestBody), [requestBody]);
  const activeSource = sourceResults?.source ?? sources.find((source) => source.id === activeSourceId);
  const activeJob = job && jobSourceId === activeSourceId ? job : sourceResults?.latestJob;
  const videos = sourceResults?.videos ?? [];
  const rankedVideos = useMemo(
    () => sourceResults?.topVideos?.[viewMetric]
      ?? [...videos].sort((left, right) => videoMetric(right, viewMetric) - videoMetric(left, viewMetric)).slice(0, 6),
    [sourceResults?.topVideos, videos, viewMetric],
  );
  const rankedMax = Math.max(1, ...rankedVideos.map((video) => videoMetric(video, viewMetric)));
  const topWords = sourceResults?.analysis?.topWords.length
    ? sourceResults.analysis.topWords
    : sourceResults?.commentSummary?.topWords ?? [];
  const wordMax = Math.max(1, ...topWords.map((word) => word.count ?? 0));
  const analysisVideoCount = sourceResults?.analysis?.videoCount ?? sourceResults?.videosTotal ?? videos.length;
  const analysisCommentCount = sourceResults?.analysis?.commentCount ?? sourceResults?.commentSummary?.total;
  const latestVideoDate = sourceResults?.analysis?.latestVideoPublishedAt
    ?? [...videos].sort((left, right) => new Date(right.publishedAt ?? 0).getTime() - new Date(left.publishedAt ?? 0).getTime())[0]?.publishedAt;
  const progressPercent = activeJob?.progress.total
    ? Math.min(100, Math.round((activeJob.progress.completed / activeJob.progress.total) * 100))
    : 0;
  const failureReason = jobFailureReason(activeJob);
  const openFailureHistory = useCallback(async () => {
    if (!activeSourceId) return;
    try {
      const failed = (await listSourceJobs(activeSourceId)).filter((item) => item.state === "failed");
      const history = failed.length
        ? failed.map((item, index) => `${index + 1}. ${jobFailureReason(item) ?? "실패 사유가 기록되지 않았습니다."}\n작업 ID: ${item.id}`).join("\n\n")
        : "기록된 실패 이력이 없습니다.";
      window.alert(`수집 실패 이력\n\n${history}`);
    } catch (caught) {
      window.alert(caught instanceof Error ? caught.message : "실패 이력을 불러오지 못했습니다.");
    }
  }, [activeSourceId]);
  const exploreVideos = useMemo(() => {
    const scoped = exploreChannelId ? explore.videos.filter((video) => video.channelId === exploreChannelId) : explore.videos;
    return [...scoped].sort((left, right) => {
      const rightPublished = new Date(right.publishedAt ?? 0).getTime();
      const leftPublished = new Date(left.publishedAt ?? 0).getTime();
      if (rightPublished !== leftPublished) return rightPublished - leftPublished;
      return new Date(right.fetchedAt ?? 0).getTime() - new Date(left.fetchedAt ?? 0).getTime();
    });
  }, [explore.videos, exploreChannelId]);
  const visibleExploreVideos = exploreVideos.slice(0, exploreVisibleCount);
  const selectedExploreChannel = useMemo(
    () => explore.channels.find((channel) => channel.youtubeChannelId === exploreChannelId) ?? null,
    [explore.channels, exploreChannelId],
  );
  const sourceRegisteredChannels = useMemo(() => {
    const targetIds = new Set(
      sources
        .filter((source) => source.type === "channel" && source.targetId)
        .map((source) => source.targetId),
    );
    return explore.channels.filter((channel) => channel.targetId && targetIds.has(channel.targetId));
  }, [explore.channels, sources]);
  const displayedSources = page === "keywords" ? sources.filter((source) => source.type === "keyword") : sources;
  const hasSearchQuery = submittedSearchQuery.length >= 2;
  const update = <K extends keyof FormState>(key: K, value: FormState[K]) => {
    setForm((current) => ({ ...current, [key]: value }));
    setError(null);
  };

  const refreshRecentFailures = useCallback(async () => {
    const requestedUser = authUser;
    if (!requestedUser || page !== "jobs") return;
    recentFailuresAbortControllerRef.current?.abort();
    const controller = new AbortController();
    recentFailuresAbortControllerRef.current = controller;
    const generation = ++recentFailuresGenerationRef.current;
    setIsRecentFailuresLoading(true);
    setRecentFailuresError(null);
    try {
      const failures = await listRecentJobFailures(10, controller.signal);
      if (!controller.signal.aborted
        && generation === recentFailuresGenerationRef.current
        && authUserRef.current === requestedUser) {
        setRecentFailures(failures);
      }
    } catch (caught) {
      if (!isAbortError(caught)
        && !controller.signal.aborted
        && generation === recentFailuresGenerationRef.current
        && authUserRef.current === requestedUser) {
        setRecentFailuresError(caught instanceof Error ? caught.message : "최근 수집 실패 현황을 불러오지 못했습니다.");
      }
    } finally {
      if (recentFailuresAbortControllerRef.current === controller) {
        recentFailuresAbortControllerRef.current = null;
      }
      if (!controller.signal.aborted
        && generation === recentFailuresGenerationRef.current
        && authUserRef.current === requestedUser) {
        setIsRecentFailuresLoading(false);
      }
    }
  }, [authUser, page]);

  const openSourceWorkspace = useCallback((sourceId: string) => {
    router.push(`/channels?source=${encodeURIComponent(sourceId)}`);
  }, [router]);

  const refreshSources = useCallback(async () => {
    setIsSourcesLoading(true);
    try {
      const nextSources = dedupeSources(await listSources());
      for (const source of nextSources) {
        if (source.latestJob && !isTerminalJob(source.latestJob)) {
          const entry: ActiveSourceJob = {
            sourceId: source.id,
            ...(source.targetId ? { targetId: source.targetId } : {}),
            job: source.latestJob,
          };
          activeJobsRef.current.set(activeJobKey(entry), entry);
        }
      }
      if (nextSources.some((source) => source.latestJob && !isTerminalJob(source.latestJob))) {
        wakeActiveJobsPollRef.current?.();
      }
      setSources(nextSources);
      setActiveSourceId((current) => {
        if (current && nextSources.some((source) => source.id === current)) return current;
        return nextSources[0]?.id ?? "";
      });
    } catch {
      setResultsError("수집 대상 목록을 불러오지 못했습니다. API 연결 상태를 확인하세요.");
    } finally {
      setIsSourcesLoading(false);
    }
  }, []);

  const refreshResults = useCallback(async (sourceId: string) => {
    if (!sourceId) return;
    resultsAbortController.current?.abort();
    sourceVideosAbortController.current?.abort();
    const controller = new AbortController();
    resultsAbortController.current = controller;
    const requestId = ++resultsRequest.current;
    setIsResultsLoading(true);
    setResultsError(null);
    setSourceVideosError(null);
    setIsSourceVideosLoadingMore(false);
    sourceVideosInFlightRef.current = false;
    try {
      const nextResults = await getSourceWorkspace(sourceId, controller.signal);
      if (requestId === resultsRequest.current) setSourceResults(nextResults);
    } catch (caught) {
      if (requestId === resultsRequest.current && !isAbortError(caught)) {
        setResultsError(caught instanceof Error ? caught.message : "수집 결과를 불러오지 못했습니다.");
      }
    } finally {
      if (resultsAbortController.current === controller) resultsAbortController.current = null;
      if (requestId === resultsRequest.current && !controller.signal.aborted) setIsResultsLoading(false);
    }
  }, []);

  const loadMoreSourceVideos = useCallback(async () => {
    const sourceId = activeSourceIdRef.current;
    const cursor = sourceResults?.videosNextCursor;
    if (!sourceId || !cursor || sourceVideosInFlightRef.current) return;
    sourceVideosInFlightRef.current = true;
    sourceVideosAbortController.current?.abort();
    const controller = new AbortController();
    sourceVideosAbortController.current = controller;
    setIsSourceVideosLoadingMore(true);
    setSourceVideosError(null);
    try {
      const page = await getSourceVideos(sourceId, { cursor, signal: controller.signal });
      if (controller.signal.aborted || activeSourceIdRef.current !== sourceId) return;
      setSourceResults((current) => {
        if (!current || current.source.id !== sourceId) return current;
        const existingIds = new Set(current.videos.map((video) => video.id));
        return {
          ...current,
          videos: [...current.videos, ...page.videos.filter((video) => !existingIds.has(video.id))],
          videosNextCursor: page.nextCursor,
          videosSnapshotAt: page.snapshotAt ?? current.videosSnapshotAt,
          videosTotal: page.total,
        };
      });
    } catch (caught) {
      if (!isAbortError(caught)) {
        setSourceVideosError(caught instanceof Error ? caught.message : "추가 동영상을 불러오지 못했습니다.");
      }
    } finally {
      if (sourceVideosAbortController.current === controller) sourceVideosAbortController.current = null;
      if (sourceVideosAbortController.current === null || sourceVideosAbortController.current === controller) {
        sourceVideosInFlightRef.current = false;
      }
      if (!controller.signal.aborted) setIsSourceVideosLoadingMore(false);
    }
  }, [sourceResults?.videosNextCursor]);

  const refreshExplore = useCallback(async (channelId?: string | null) => {
    setIsExploreLoading(true);
    setIsExploreLoadingMore(false);
    setExploreError(null);
    try {
      setExplore(await getExplore(channelId ?? undefined));
      setExploreVisibleCount(12);
    } catch (caught) {
      setExploreError(caught instanceof Error ? caught.message : "Explore 라이브러리를 불러오지 못했습니다.");
    } finally {
      setIsExploreLoading(false);
    }
  }, []);

  const loadMoreExplore = useCallback(async () => {
    if (isExploreLoadingMore || explore.nextOffset === undefined) return;
    setIsExploreLoadingMore(true);
    setExploreError(null);
    try {
      const nextPage = await getExplore(exploreChannelId ?? undefined, explore.nextOffset);
      setExplore((current) => ({
        channels: current.channels.length ? current.channels : nextPage.channels,
        videos: [...current.videos, ...nextPage.videos.filter((video) => !current.videos.some((item) => item.id === video.id))],
        ...(nextPage.nextOffset !== undefined ? { nextOffset: nextPage.nextOffset } : {}),
      }));
      setExploreVisibleCount((current) => current + nextPage.videos.length);
    } catch (caught) {
      setExploreError(caught instanceof Error ? caught.message : "추가 동영상을 불러오지 못했습니다.");
    } finally {
      setIsExploreLoadingMore(false);
    }
  }, [explore.nextOffset, exploreChannelId, isExploreLoadingMore]);

  const submitCollectedSearch = (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    const query = searchQuery.trim();
    if (query.length < 2) {
      setSearchError("검색어를 두 글자 이상 입력하세요.");
      return;
    }
    searchRequest.current += 1;
    setSubmittedSearchQuery(query);
    setSubmittedSearchScope(searchScope);
    setSearchResults(null);
    setSearchError(null);
    setIsSearchLoading(true);
    setSearchRun((current) => current + 1);
  };

  useEffect(() => {
    const query = submittedSearchQuery;
    if (page !== "explore" || query.length < 2) {
      setSearchResults(null);
      setSearchError(null);
      setIsSearchLoading(false);
      return;
    }
    const requestId = ++searchRequest.current;
    void searchCollected(query, submittedSearchScope)
      .then((result) => {
        if (requestId === searchRequest.current) setSearchResults(result);
      })
      .catch((caught) => {
        if (requestId === searchRequest.current) {
          setSearchError(caught instanceof Error ? caught.message : "통합 검색 결과를 불러오지 못했습니다.");
        }
      })
      .finally(() => {
        if (requestId === searchRequest.current) setIsSearchLoading(false);
      });
  }, [page, searchRun, submittedSearchQuery, submittedSearchScope]);

  const toggleSubscriptionRefresh = useCallback(async (source: SourceSummary) => {
    setUpdatingSourceId(source.id);
    try {
      await updateSource(source.id, { enabled: !source.enabled });
      await Promise.all([refreshSources(), refreshExplore()]);
      setNotice(source.enabled
        ? "이 수집 대상의 자동 갱신을 멈췄습니다. 다른 사용자의 수집에는 영향을 주지 않습니다."
        : "이 수집 대상의 자동 갱신을 다시 시작했습니다.");
    } catch (caught) {
      setExploreError(caught instanceof Error ? caught.message : "수집 상태를 변경하지 못했습니다.");
    } finally {
      setUpdatingSourceId(null);
    }
  }, [refreshExplore, refreshSources]);

  const removeSource = useCallback(async (source: SourceSummary) => {
    const confirmed = window.confirm(`“${sourceLabel(source)}” 수집 대상을 삭제할까요? 자동 수집은 중지되지만 이미 저장된 채널·영상·댓글 데이터는 Explore에서 유지됩니다.`);
    if (!confirmed) return;
    setOpenSourceMenuId(null);
    setDeletingSourceId(source.id);
    try {
      await deleteSource(source.id);
      if (activeSourceId === source.id) {
        setActiveSourceId("");
        setSourceResults(null);
      }
      await Promise.all([refreshSources(), refreshExplore()]);
      setNotice("수집 대상과 자동 갱신을 삭제했습니다. 저장된 공개 데이터는 유지됩니다.");
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "수집 대상을 삭제하지 못했습니다.");
    } finally {
      setDeletingSourceId(null);
    }
  }, [activeSourceId, exploreChannelId, refreshExplore, refreshSources]);

  const loadComments = useCallback(async (
    video: CollectedVideo,
    cursor?: string,
    sort: CommentThreadSort = commentSort,
  ) => {
    const requestId = ++commentsRequest.current;
    setSelectedVideo(video);
    setIsCommentsLoading(true);
    setCommentsError(null);
    try {
      const response = await getVideoCommentThreads(video.id, sort, cursor);
      if (requestId !== commentsRequest.current) return;
      setCommentThreads((current) => cursor ? mergeCommentThreads(current, response.items) : response.items);
      setNextCommentsCursor(response.nextCursor);
    } catch (caught) {
      if (requestId !== commentsRequest.current) return;
      setCommentsError(caught instanceof Error ? caught.message : "댓글을 불러오지 못했습니다.");
      if (!cursor) {
        setCommentThreads([]);
        setNextCommentsCursor(undefined);
      }
    } finally {
      if (requestId === commentsRequest.current) setIsCommentsLoading(false);
    }
  }, [commentSort]);

  const restoreFocus = useCallback((trigger: HTMLElement | null) => {
    window.requestAnimationFrame(() => trigger?.focus());
  }, []);

  const closeCollectionDrawer = useCallback(() => {
    setIsCollectionOpen(false);
    restoreFocus(collectionTriggerRef.current);
  }, [restoreFocus]);

  const closeSettingsDrawer = useCallback(() => {
    setIsSettingsOpen(false);
    restoreFocus(settingsTriggerRef.current);
  }, [restoreFocus]);

  const closeVideoDrawer = useCallback(() => {
    setSelectedVideo(null);
    setSelectedCommentId(null);
    setSelectedCommentDetail(null);
    setCommentThreads([]);
    setCommentSort("newest");
    setNextCommentsCursor(undefined);
    setCommentsError(null);
    restoreFocus(videoTriggerRef.current);
  }, [restoreFocus]);

  const closeCommentDetail = useCallback(() => {
    setSelectedCommentId(null);
    setSelectedCommentDetail(null);
    setCommentDetailError(null);
    restoreFocus(commentTriggerRef.current);
  }, [restoreFocus]);

  const openCollectionDrawer = (event: MouseEvent<HTMLButtonElement>, sourceType?: CollectionSourceType) => {
    collectionTriggerRef.current = event.currentTarget;
    const nextForm = formFromPreferences(preferences);
    if (sourceType === "channel" || sourceType === "keyword") nextForm.sourceType = sourceType;
    setForm(nextForm);
    setError(null);
    setIsCollectionOpen(true);
  };

  const openSettingsDrawer = (event: MouseEvent<HTMLButtonElement>) => {
    settingsTriggerRef.current = event.currentTarget;
    setSettingsDraft(preferences);
    setError(null);
    setIsSettingsOpen(true);
  };

  const saveSettings = (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    if (!authUser) return;
    const next = normalizePreferences(settingsDraft);
    try {
      window.localStorage.setItem(preferencesStorageKey(authUser), JSON.stringify(next));
    } catch {
      setError("이 브라우저에 기본 설정을 저장하지 못했습니다. 저장 공간 또는 개인정보 보호 설정을 확인하세요.");
      return;
    }
    setPreferences(next);
    setSettingsDraft(next);
    setForm(formFromPreferences(next));
    setError(null);
    setNotice("기본 수집 설정을 저장했습니다. 다음 수집 대상 추가부터 적용됩니다.");
    closeSettingsDrawer();
  };

  const resetSettings = () => {
    if (!authUser) return;
    try {
      window.localStorage.removeItem(preferencesStorageKey(authUser));
    } catch {
      setError("이 브라우저에서 기본 설정을 초기화하지 못했습니다.");
      return;
    }
    const next = { ...defaultCollectionPreferences };
    setPreferences(next);
    setSettingsDraft(next);
    setForm(formFromPreferences(next));
    setError(null);
    setNotice("기본 수집 설정을 초기화했습니다.");
  };

  const openVideoDrawer = useCallback((video: CollectedVideo, trigger: HTMLElement) => {
    videoTriggerRef.current = trigger;
    setSelectedCommentId(null);
    setSelectedCommentDetail(null);
    setCommentDetailError(null);
    setCommentSort("newest");
    setCommentThreads([]);
    setNextCommentsCursor(undefined);
    void loadComments(video, undefined, "newest");
  }, [loadComments]);

  const changeCommentSort = useCallback((sort: CommentThreadSort) => {
    if (!selectedVideo || sort === commentSort) return;
    setCommentSort(sort);
    setCommentThreads([]);
    setNextCommentsCursor(undefined);
    setCommentsError(null);
    void loadComments(selectedVideo, undefined, sort);
  }, [commentSort, loadComments, selectedVideo]);

  const openCommentDetail = useCallback(async (commentId: string, trigger: HTMLElement) => {
    commentTriggerRef.current = trigger;
    setSelectedCommentId(commentId);
    setSelectedCommentDetail(null);
    setCommentDetailError(null);
    setIsCommentDetailLoading(true);
    try {
      setSelectedCommentDetail(await getCommentDetail(commentId));
    } catch (caught) {
      setCommentDetailError(caught instanceof Error ? caught.message : "댓글 상세 정보를 불러오지 못했습니다.");
    } finally {
      setIsCommentDetailLoading(false);
    }
  }, []);

  useEffect(() => {
    setRequestedSourceId(new URLSearchParams(window.location.search).get("source"));
  }, []);

  useEffect(() => {
    sourcesRef.current = sources;
  }, [sources]);

  useEffect(() => {
    activeSourceIdRef.current = activeSourceId;
  }, [activeSourceId]);

  useEffect(() => {
    localJobRef.current = job && jobSourceId ? { sourceId: jobSourceId, job } : null;
  }, [job, jobSourceId]);

  useEffect(() => () => {
    resultsAbortController.current?.abort();
    sourceVideosAbortController.current?.abort();
  }, []);

  useEffect(() => {
    authUserRef.current = authUser;
    recentFailuresAbortControllerRef.current?.abort();
    recentFailuresAbortControllerRef.current = null;
    recentFailuresGenerationRef.current += 1;
    setRecentFailures([]);
    setRecentFailuresError(null);
    setIsRecentFailuresLoading(false);
    setIsSettingsOpen(false);
    if (authUser !== null) return;
    resultsAbortController.current?.abort();
    sourceVideosAbortController.current?.abort();
    activeJobsRef.current.clear();
    handledTerminalJobsRef.current.clear();
    activeJobsEndpointSupportedRef.current = null;
    sourcesRef.current = [];
    activeSourceIdRef.current = "";
    localJobRef.current = null;
    setSources([]);
    setActiveSourceId("");
    setSourceResults(null);
    setExplore({ channels: [], videos: [] });
    setJob(null);
    setJobSourceId(null);
  }, [authUser]);

  useEffect(() => {
    if (authUser) void refreshSources();
  }, [authUser, refreshSources]);

  useEffect(() => {
    if (!authUser || page !== "jobs") return;
    void refreshRecentFailures();
    return () => {
      recentFailuresAbortControllerRef.current?.abort();
      recentFailuresAbortControllerRef.current = null;
      recentFailuresGenerationRef.current += 1;
    };
  }, [authUser, page, refreshRecentFailures]);

  useEffect(() => {
    if (!requestedSourceId || appliedSourceQueryRef.current === requestedSourceId) return;
    if (!sources.some((source) => source.id === requestedSourceId)) return;
    appliedSourceQueryRef.current = requestedSourceId;
    setActiveSourceId(requestedSourceId);
  }, [requestedSourceId, sources]);

  useEffect(() => {
    if (authUser) void refreshExplore();
  }, [authUser, refreshExplore]);

  useEffect(() => {
    if (page !== "overview" || !activeSource?.targetId) return;
    const activeChannel = explore.channels.find((channel) => channel.targetId === activeSource.targetId);
    if (activeChannel && activeChannel.youtubeChannelId !== exploreChannelId) {
      setExploreChannelId(activeChannel.youtubeChannelId);
    }
  }, [activeSource?.targetId, explore.channels, exploreChannelId, page]);

  useEffect(() => {
    if (!exploreChannelId) {
      setSubscriberHistory([]);
      return;
    }
    let cancelled = false;
    void getChannelSubscriberHistory(exploreChannelId).then((history) => {
      if (!cancelled) setSubscriberHistory(history);
    }).catch(() => {
      if (!cancelled) setSubscriberHistory([]);
    });
    return () => { cancelled = true; };
  }, [exploreChannelId]);

  useEffect(() => {
    const sentinel = exploreLoadMoreRef.current;
    if (page !== "explore" || hasSearchQuery || !sentinel || isExploreLoadingMore) return;
    const observer = new IntersectionObserver(([entry]) => {
      if (!entry.isIntersecting) return;
      if (visibleExploreVideos.length < exploreVideos.length) {
        setExploreVisibleCount((count) => Math.min(count + 12, exploreVideos.length));
      } else {
        void loadMoreExplore();
      }
    }, { rootMargin: "288px 0px" });
    observer.observe(sentinel);
    return () => observer.disconnect();
  }, [hasSearchQuery, isExploreLoadingMore, loadMoreExplore, page, exploreVideos.length, visibleExploreVideos.length]);

  useEffect(() => {
    const sentinel = commentThreadsLoadMoreRef.current;
    if (!selectedVideo || selectedCommentId || !nextCommentsCursor || !sentinel || isCommentsLoading) return;
    const scrollRoot = videoModalRef.current?.querySelector<HTMLElement>(".video-modal-scroll") ?? null;
    const observer = new IntersectionObserver(([entry]) => {
      if (entry.isIntersecting) void loadComments(selectedVideo, nextCommentsCursor);
    }, { root: scrollRoot, rootMargin: "240px 0px" });
    observer.observe(sentinel);
    return () => observer.disconnect();
  }, [isCommentsLoading, loadComments, nextCommentsCursor, selectedCommentId, selectedVideo]);

  useEffect(() => {
    const sentinel = sourceVideosLoadMoreRef.current;
    if (!sourceResults?.videosNextCursor || !sentinel || isSourceVideosLoadingMore || sourceVideosError) return;
    const observer = new IntersectionObserver(([entry]) => {
      if (entry.isIntersecting) void loadMoreSourceVideos();
    }, { rootMargin: "320px 0px" });
    observer.observe(sentinel);
    return () => observer.disconnect();
  }, [isSourceVideosLoadingMore, loadMoreSourceVideos, sourceResults?.videosNextCursor, sourceVideosError]);

  useEffect(() => {
    if (!activeSourceId) {
      resultsAbortController.current?.abort();
      sourceVideosAbortController.current?.abort();
      setSourceResults(null);
      setIsResultsLoading(false);
      setIsSourceVideosLoadingMore(false);
      sourceVideosInFlightRef.current = false;
      return;
    }
    setSelectedVideo(null);
    setSelectedCommentId(null);
    setSelectedCommentDetail(null);
    setCommentThreads([]);
    setCommentSort("newest");
    setNextCommentsCursor(undefined);
    setCommentsError(null);
    setSourceVideosError(null);
    void refreshResults(activeSourceId);
  }, [activeSourceId, refreshResults]);

  useEffect(() => {
    if (!authUser) return;
    let stopped = false;
    let inFlight = false;
    let pollAgain = false;
    let timer: number | undefined;
    let controller: AbortController | null = null;
    let failureCount = 0;

    const readLegacyActiveJobs = async (signal: AbortSignal) => {
      const candidates: ActiveSourceJob[] = sourcesRef.current.flatMap((source) => (
        source.latestJob && !isTerminalJob(source.latestJob)
          ? [{ sourceId: source.id, ...(source.targetId ? { targetId: source.targetId } : {}), job: source.latestJob }]
          : []
      ));
      const local = localJobRef.current;
      if (local && !isTerminalJob(local.job) && !candidates.some((entry) => activeJobKey(entry) === `${local.sourceId}:${local.job.id}`)) {
        candidates.push({ sourceId: local.sourceId, job: local.job });
      }
      const resolved = await Promise.all(candidates.map(async (entry) => {
        try {
          return { ...entry, job: await getJob(entry.job.id, signal) };
        } catch (caught) {
          if (caught instanceof ApiError && caught.status === 404) return null;
          throw caught;
        }
      }));
      return resolved.flatMap((entry) => entry ? [entry] : []);
    };

    const readActiveJobs = async (signal: AbortSignal) => {
      if (activeJobsEndpointSupportedRef.current !== false) {
        try {
          const entries = await listActiveJobs(signal);
          activeJobsEndpointSupportedRef.current = true;
          return entries;
        } catch (caught) {
          if (!(caught instanceof ApiError) || ![404, 405, 501].includes(caught.status)) throw caught;
          activeJobsEndpointSupportedRef.current = false;
        }
      }
      return readLegacyActiveJobs(signal);
    };

    const schedule = (delay: number) => {
      if (stopped) return;
      if (timer !== undefined) window.clearTimeout(timer);
      timer = window.setTimeout(() => { void poll(); }, delay);
    };

    const poll = async () => {
      if (stopped || inFlight) {
        pollAgain = true;
        return;
      }
      inFlight = true;
      controller = new AbortController();
      let activeEntries: ActiveSourceJob[] = [];
      try {
        const entries = await readActiveJobs(controller.signal);
        if (stopped || controller.signal.aborted) return;
        failureCount = 0;

        const previous = activeJobsRef.current;
        const explicitTerminal = entries.filter(({ job: currentJob }) => isTerminalJob(currentJob));
        activeEntries = entries.filter(({ job: currentJob }) => !isTerminalJob(currentJob));
        const current = new Map(activeEntries.map((entry) => [activeJobKey(entry), entry]));
        const disappeared = [...previous.entries()].flatMap(([key, entry]) => current.has(key) ? [] : [entry]);
        activeJobsRef.current = current;

        const latestBySource = new Map<string, ActiveSourceJob>();
        for (const entry of [...activeEntries, ...explicitTerminal]) latestBySource.set(entry.sourceId, entry);
        setSources((currentSources) => currentSources.map((source) => {
          const entry = latestBySource.get(source.id);
          return entry ? { ...source, latestJob: entry.job } : source;
        }));

        const selectedSourceId = activeSourceIdRef.current;
        const selectedEntry = latestBySource.get(selectedSourceId);
        if (selectedEntry) {
          setJob(selectedEntry.job);
          setJobSourceId(selectedSourceId);
        } else if (disappeared.some((entry) => entry.sourceId === selectedSourceId)) {
          setJob(null);
          setJobSourceId(null);
        }

        const newlyTerminal = [...explicitTerminal, ...disappeared].filter((entry) => {
          const key = activeJobKey(entry);
          if (handledTerminalJobsRef.current.has(key)) return false;
          handledTerminalJobsRef.current.add(key);
          return true;
        });
        if (newlyTerminal.length > 0) {
          const affectedSourceIds = new Set(newlyTerminal.map((entry) => entry.sourceId));
          await Promise.all([
            refreshSources(),
            affectedSourceIds.has(selectedSourceId) && selectedSourceId
              ? refreshResults(selectedSourceId)
              : Promise.resolve(),
            refreshExplore(),
            // The active-jobs endpoint intentionally returns non-terminal jobs
            // only. A disappeared running job may have failed, so Status must
            // re-read the authoritative failure list for every terminal exit.
            page === "jobs"
              ? refreshRecentFailures()
              : Promise.resolve(),
          ]);
        }
      } catch (caught) {
        if (isAbortError(caught) || stopped) return;
        if (caught instanceof ApiError && caught.status === 401) {
          stopped = true;
          changeAuthUser(null);
          return;
        }
        failureCount += 1;
      } finally {
        inFlight = false;
        controller = null;
        if (!stopped) {
          const delay = pollAgain ? 0 : activeJobPollingDelay(activeEntries, failureCount);
          pollAgain = false;
          schedule(delay);
        }
      }
    };

    const handleVisibilityChange = () => {
      if (document.visibilityState !== "visible") return;
      if (inFlight) pollAgain = true;
      else schedule(0);
    };
    wakeActiveJobsPollRef.current = handleVisibilityChange;
    document.addEventListener("visibilitychange", handleVisibilityChange);
    schedule(0);
    return () => {
      stopped = true;
      if (timer !== undefined) window.clearTimeout(timer);
      controller?.abort();
      if (wakeActiveJobsPollRef.current === handleVisibilityChange) wakeActiveJobsPollRef.current = null;
      document.removeEventListener("visibilitychange", handleVisibilityChange);
    };
  }, [authUser, changeAuthUser, page, refreshExplore, refreshRecentFailures, refreshResults, refreshSources]);

  useEffect(() => {
    const drawer = isSettingsOpen
      ? settingsDrawerRef.current
      : isCollectionOpen
        ? collectionDrawerRef.current
        : (selectedCommentId || selectedVideo) ? videoModalRef.current : null;
    if (!drawer) return;

    const previousOverflow = document.body.style.overflow;
    const previousOverscrollBehavior = document.body.style.overscrollBehavior;
    document.body.style.overflow = "hidden";
    document.body.style.overscrollBehavior = "contain";

    const focusInitialControl = window.requestAnimationFrame(() => {
      const initialFocusTarget = drawer.querySelector<HTMLElement>("[data-drawer-initial-focus]") ?? drawer;
      initialFocusTarget.focus();
    });

    const keepFocusInDrawer = (event: KeyboardEvent) => {
      if (event.key === "Escape") {
        event.preventDefault();
        if (isSettingsOpen) closeSettingsDrawer();
        else if (isCollectionOpen) closeCollectionDrawer();
        else if (selectedCommentId) closeCommentDetail();
        else closeVideoDrawer();
        return;
      }
      if (event.key !== "Tab") return;

      const focusable = Array.from(drawer.querySelectorAll<HTMLElement>(
        'a[href], button:not([disabled]), input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])',
      )).filter((element) => element.getAttribute("aria-hidden") !== "true");
      if (focusable.length === 0) {
        event.preventDefault();
        drawer.focus();
        return;
      }

      const first = focusable[0];
      const last = focusable[focusable.length - 1];
      if (event.shiftKey && document.activeElement === first) {
        event.preventDefault();
        last.focus();
      } else if (!event.shiftKey && document.activeElement === last) {
        event.preventDefault();
        first.focus();
      }
    };

    document.addEventListener("keydown", keepFocusInDrawer);
    return () => {
      window.cancelAnimationFrame(focusInitialControl);
      document.removeEventListener("keydown", keepFocusInDrawer);
      document.body.style.overflow = previousOverflow;
      document.body.style.overscrollBehavior = previousOverscrollBehavior;
    };
  }, [closeCollectionDrawer, closeCommentDetail, closeSettingsDrawer, closeVideoDrawer, isCollectionOpen, isSettingsOpen, selectedCommentId, selectedVideo]);

  const launchJob = useCallback(async () => {
    if (validationError) {
      setError(validationError);
      return;
    }
    setIsStarting(true);
    setError(null);
    setNotice(null);
    try {
      const response = await createCollectionRequest(requestBody, {
        idempotencyKey: idempotencyKey(),
      });
      setSources((current) => dedupeSources([response.source, ...current.filter((item) => item.id !== response.source.id)]));
      setActiveSourceId(response.source.id);
      if (response.job) {
        setJobSourceId(response.source.id);
        setJob(response.job);
        if (!isTerminalJob(response.job)) {
          const entry: ActiveSourceJob = {
            sourceId: response.source.id,
            ...(response.source.targetId ? { targetId: response.source.targetId } : {}),
            job: response.job,
          };
          activeJobsRef.current.set(activeJobKey(entry), entry);
          handledTerminalJobsRef.current.delete(activeJobKey(entry));
          wakeActiveJobsPollRef.current?.();
        }
      } else {
        setJobSourceId(null);
        setJob(null);
      }
      closeCollectionDrawer();
      setNotice(collectionNotice(response.disposition));
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "수집 작업을 시작하지 못했습니다.");
    } finally {
      setIsStarting(false);
    }
  }, [closeCollectionDrawer, requestBody, validationError]);

  const navigation = [
    { id: "explore" as const, label: "Explore", href: "/", Icon: Squares2X2Icon },
    { id: "overview" as const, label: "Channels", href: "/channels", Icon: HomeIcon },
    { id: "sources" as const, label: "Sources", href: "/sources", Icon: FolderIcon },
    { id: "jobs" as const, label: "Status", href: "/jobs", Icon: QueueListIcon },
  ];
  const breadcrumbPage = page === "overview" ? "Channels" : page === "explore" ? "Explore" : page === "sources" ? "Sources" : page === "keywords" ? "Keywords" : page === "jobs" ? "Status" : "Insights";
  const breadcrumbDetail = page === "overview" && selectedExploreChannel
    ? selectedExploreChannel.title ?? selectedExploreChannel.handle ?? selectedExploreChannel.youtubeChannelId
    : null;
  const activeModalVideo = selectedVideo ?? selectedCommentDetail?.video ?? null;
  const selectedDetailThread = selectedCommentDetail
    ? {
        comment: selectedCommentDetail.parentComment ?? selectedCommentDetail.comment,
        repliesPreview: selectedCommentDetail.replies,
        storedReplyCount: selectedCommentDetail.storedReplyCount,
      }
    : null;

  if (authUser === undefined) return <main className="login-page"><p className="explore-loading">세션을 확인하는 중입니다…</p></main>;
  if (!authUser) return <LoginScreen onAuthenticated={changeAuthUser} />;

  return (
    <div className={`app-shell page-${page}`}>
      <aside className="sidebar" aria-label="Monitube 탐색">
        <div className="brand-lockup">
          <span className="brand-mark" aria-hidden="true"><PlayIcon /></span>
          <span>monitube</span>
        </div>

        <nav className="sidebar-nav" aria-label="주요 메뉴">
          {navigation.map(({ id, label, href, Icon }) => (
            <Link
              key={id}
              className={page === id ? "nav-item nav-item-active" : "nav-item"}
              aria-label={label}
              aria-current={page === id ? "page" : undefined}
              href={href}
            >
              <Icon aria-hidden="true" />
              <span>{label}</span>
            </Link>
          ))}
        </nav>

      </aside>

      <main className="dashboard-main">
        <div className="dashboard-utilitybar">
          <nav className="dashboard-breadcrumb" aria-label="현재 위치">
            <Link href="/" aria-label="Monitube 홈">Monitube</Link>
            <ChevronRightIcon aria-hidden="true" />
            {breadcrumbDetail ? <Link href="/channels">{breadcrumbPage}</Link> : <span aria-current="page">{breadcrumbPage}</span>}
            {breadcrumbDetail && <>
              <ChevronRightIcon aria-hidden="true" />
              <span aria-current="page" title={breadcrumbDetail}>{breadcrumbDetail}</span>
            </>}
          </nav>
          <button className="settings-button" type="button" onClick={openSettingsDrawer} aria-label="기본 설정 열기" aria-haspopup="dialog" aria-expanded={isSettingsOpen}>
            <Cog6ToothIcon aria-hidden="true" />
          </button>
        </div>
        {page !== "explore" && page !== "sources" && page !== "keywords" && <header className="dashboard-topbar" id="source-selector" tabIndex={-1}>
          <label className="source-select">
            <span className="visually-hidden">수집 대상 선택</span>
            <FolderIcon aria-hidden="true" />
            <select
              value={activeSourceId}
              disabled={isSourcesLoading || sources.length === 0}
              onChange={(event) => setActiveSourceId(event.target.value)}
              aria-describedby="active-source-coverage"
            >
              {sources.length === 0 && <option value="">등록된 수집 대상 없음</option>}
              {sources.map((source) => (
                <option key={source.id} value={source.id}>
                  {sourceLabel(source)} — {sourceCoverage(source)}
                </option>
              ))}
            </select>
            <span className="source-select-detail" id="active-source-coverage">
              {activeSource
                ? `${sourceCoverage(activeSource)} · ${activeSource.lastCompletedAt ? `마지막 완료 ${formatShortDate(activeSource.lastCompletedAt)}` : "수집 이력 확인 중"}`
                : "채널·키워드·동영상을 하나의 공유 수집 대상으로 관리합니다."}
            </span>
          </label>
          <div className="topbar-actions">
            <span className="refresh-copy">
              {sourceResults?.analysis?.generatedAt ? `분석 갱신 ${formatDate(sourceResults.analysis.generatedAt)}` : "수집 대상을 선택하세요"}
            </span>
            <button
              className="icon-button"
              type="button"
              onClick={() => {
                void refreshSources();
                if (activeSourceId) void refreshResults(activeSourceId);
              }}
              disabled={isSourcesLoading || isResultsLoading}
              aria-label="수집 대상과 분석 결과 새로고침"
            >
              <ArrowPathIcon aria-hidden="true" />
            </button>
            <button className="primary-action" type="button" onClick={openCollectionDrawer}>
              <PlusIcon aria-hidden="true" />
              수집 대상 추가
            </button>
          </div>
        </header>}

        {page === "jobs" && (
          <section className="status-page" aria-labelledby="status-page-title">
            <div className="workspace-page-heading status-page-heading">
              <p className="section-kicker">COLLECTION OPERATIONS</p>
              <h1 id="status-page-title">Status</h1>
              <p>현재 계정이 구독 중인 공유 수집 대상의 상태와 최근 실패 원인, 재시도 가능 여부를 확인합니다.</p>
            </div>
            <section className="panel recent-failures-panel" aria-labelledby="recent-failures-title" aria-busy={isRecentFailuresLoading}>
              <div className="panel-heading">
                <div>
                  <p className="section-kicker">RECENT FAILURES</p>
                  <h2 id="recent-failures-title">최근 공유 수집 대상 실패</h2>
                </div>
                <button className="icon-button" type="button" onClick={() => void refreshRecentFailures()} disabled={isRecentFailuresLoading} aria-label="최근 수집 실패 새로고침">
                  <ArrowPathIcon className={isRecentFailuresLoading ? "icon-spinning" : undefined} aria-hidden="true" />
                </button>
              </div>

              {recentFailuresError && (
                <div className="recent-failures-state recent-failures-error" role="status">
                  <ExclamationTriangleIcon aria-hidden="true" />
                  <div><strong>실패 현황을 불러오지 못했습니다.</strong><p>{recentFailuresError}</p></div>
                  <button type="button" onClick={() => void refreshRecentFailures()}>다시 시도</button>
                </div>
              )}
              {!recentFailuresError && isRecentFailuresLoading && recentFailures.length === 0 && (
                <div className="recent-failures-state" role="status"><span className="loading-spinner" aria-hidden="true" /><p>최근 실패 기록을 불러오는 중입니다.</p></div>
              )}
              {!recentFailuresError && !isRecentFailuresLoading && recentFailures.length === 0 && (
                <div className="recent-failures-state recent-failures-empty" role="status">
                  <CheckCircleIcon aria-hidden="true" />
                  <div><strong>구독 중인 공유 수집 대상의 최근 실패가 없습니다.</strong><p>새 실패가 기록되면 대상과 원인을 이곳에 표시합니다.</p></div>
                </div>
              )}
              {recentFailures.length > 0 && (
                <ol className="recent-failure-list" aria-live="polite">
                  {recentFailures.map((failure) => {
                    return (
                      <li key={`${failure.sourceId}:${failure.job.id}`}>
                        <div className="recent-failure-target">
                          <span className="source-type-chip">{sourceTypeCopy(failure.sourceType)}</span>
                          <strong>{failure.sourceLabel}</strong>
                          <small title={failure.targetId ?? failure.sourceId}>{failure.targetId ?? failure.sourceId}</small>
                        </div>
                        <time dateTime={failure.failedAt}>{formatDate(failure.failedAt)}</time>
                        <div className="recent-failure-reason"><strong>{failure.reason}</strong><small>Job {failure.job.id}</small></div>
                        <div className="recent-failure-tags">
                          <span className={failure.retryable === true ? "failure-tag failure-tag-retryable" : "failure-tag"}>
                            {failure.retryable === true ? "재시도 가능" : failure.retryable === false ? "재시도 불가" : "재시도 정보 없음"}
                          </span>
                          <span className="failure-tag failure-code">{failure.errorCode ?? "코드 없음"}</span>
                          {failure.failedChildCount > 0 && <span className="failure-tag failure-child-count">하위 작업 실패 {formatCount(failure.failedChildCount)}건</span>}
                        </div>
                      </li>
                    );
                  })}
                </ol>
              )}
            </section>
          </section>
        )}

        <section className="sources-page" aria-labelledby="sources-page-title">
          <div className="workspace-page-heading"><p className="section-kicker">{page === "keywords" ? "KEYWORD COLLECTION" : "COLLECTION TARGETS"}</p><h1 id="sources-page-title">{page === "keywords" ? "Keywords" : "Sources"}</h1><p>{page === "keywords" ? "등록한 키워드의 수집 범위와 최신 상태를 관리합니다." : "중복 없이 정규화된 수집 대상을 관리하고, 선택한 대상의 수집 범위를 확인합니다."}</p></div>
          <div className="sources-page-actions">
            <button className="source-add-button" type="button" onClick={(event) => openCollectionDrawer(event, page === "keywords" ? "keyword" : undefined)} aria-label={page === "keywords" ? "키워드 등록" : "수집 대상 추가"}>
              <PlusIcon aria-hidden="true" />
            </button>
          </div>
          <div className="sources-table-wrap">
          <div className="sources-page-list" aria-label="수집 대상 목록">
            <div className="source-table-header">
              <span>구분</span>
              <span>수집 대상</span>
              <span>수집 상태</span>
              <span>영상 수집률</span>
              <span>댓글 수집률</span>
              <span className="source-table-actions-header">관리</span>
            </div>
            {displayedSources.map((source) => {
              const canToggleRefresh = source.type === "channel" && Boolean(source.targetId);
              const menuOpen = openSourceMenuId === source.id;
              const channel = source.targetId ? explore.channels.find((item) => item.targetId === source.targetId) : undefined;
              const targetValue = sourceTargetValue(source);
              const channelName = channel?.title ?? channel?.handle ?? (source.type === "channel" ? "채널 정보 확인 중" : targetValue);
              const channelId = channel?.youtubeChannelId ?? targetValue;
              const videoCollectionRate = channel?.videoCollectionRate;
              const commentCollectionRate = channel?.commentCollectionRate;
              return (
                <article key={source.id} className={`source-page-card${source.id === activeSourceId ? " source-page-card-active" : ""}${menuOpen ? " source-page-card-menu-open" : ""}`}>
                  <button type="button" className="source-page-select" onClick={() => { setOpenSourceMenuId(null); openSourceWorkspace(source.id); }} aria-label={`${sourceLabel(source)} 작업 공간 열기`}>
                    <span className="source-type-chip">{sourceTypeCopy(source.type)}</span>
                    <strong title={channelId}>{channelName}</strong>
                  </button>
                  <SourceCollectionState source={source} />
                  <span className="source-collection-rate source-video-collection-rate">{videoCollectionRate === undefined ? "—" : `${videoCollectionRate}%`}</span>
                  <span className="source-collection-rate source-comment-collection-rate">{commentCollectionRate === undefined ? "—" : `${commentCollectionRate}%`}</span>
                  <div className="source-card-actions">
                    <button className="source-more-button" type="button" disabled={deletingSourceId === source.id} onClick={() => setOpenSourceMenuId((current) => current === source.id ? null : source.id)} aria-label={`${sourceLabel(source)} 관리 메뉴`} aria-expanded={menuOpen} aria-haspopup="menu"><EllipsisHorizontalIcon aria-hidden="true" /></button>
                    {menuOpen && <div className="source-action-menu" role="menu" aria-label={`${sourceLabel(source)} 관리`}>
                      {canToggleRefresh && <button type="button" role="menuitem" disabled={updatingSourceId === source.id} onClick={() => { setOpenSourceMenuId(null); void toggleSubscriptionRefresh(source); }}>{source.enabled ? "수집 일시정지" : "수집 재개"}</button>}
                      <button className="source-action-menu-delete" type="button" role="menuitem" onClick={() => void removeSource(source)}>삭제</button>
                    </div>}
                  </div>
                </article>
              );
            })}
            {displayedSources.length === 0 && <div className="explore-empty">{page === "keywords" ? "아직 등록된 키워드가 없습니다." : "아직 등록된 수집 대상이 없습니다."}</div>}
          </div>
          </div>
        </section>

        <section className="channel-details-page" aria-label="채널 상세">
          <div className="explore-channel-strip" aria-label="수집된 채널">
            {sourceRegisteredChannels.map((channel) => {
              const coverVideo = explore.videos.find((video) => video.channelId === channel.youtubeChannelId);
              const selected = channel.youtubeChannelId === exploreChannelId;
              const avatarUrl = channel.thumbnailUrl ?? (coverVideo ? youtubeThumbnail(coverVideo.youtubeVideoId) : undefined);
              return <button className={selected ? "explore-channel-avatar-button explore-channel-avatar-button-selected" : "explore-channel-avatar-button"} type="button" key={channel.youtubeChannelId} onClick={() => { const nextChannelId = selected ? null : channel.youtubeChannelId; setExploreChannelId(nextChannelId); setExploreVisibleCount(12); if (nextChannelId && channel.targetId) { const source = sources.find((item) => item.targetId === channel.targetId); if (source) setActiveSourceId(source.id); } }} aria-pressed={selected} aria-label={`${channel.title ?? channel.handle ?? channel.youtubeChannelId} 채널 상세 보기`} title={channel.title ?? channel.handle ?? channel.youtubeChannelId}>{avatarUrl ? <img src={avatarUrl} alt="" /> : <span className="explore-avatar">{(channel.title ?? channel.handle ?? "Y").slice(0, 1).toUpperCase()}</span>}</button>;
            })}
          </div>
          {selectedExploreChannel && <section className="explore-channel-overview" aria-labelledby="channel-overview-title">
            <div className="channel-overview-avatar">{selectedExploreChannel.thumbnailUrl ? <img src={selectedExploreChannel.thumbnailUrl} alt="" /> : <span>{(selectedExploreChannel.title ?? selectedExploreChannel.handle ?? "Y").slice(0, 1).toUpperCase()}</span>}</div>
            <div className="channel-overview-copy"><p className="section-kicker">CHANNEL OVERVIEW</p><h3 id="channel-overview-title">{selectedExploreChannel.title ?? selectedExploreChannel.handle ?? selectedExploreChannel.youtubeChannelId}</h3><p className="channel-overview-id">{selectedExploreChannel.handle ? `@${selectedExploreChannel.handle.replace(/^@/, "")} · ` : ""}{selectedExploreChannel.youtubeChannelId}</p>{selectedExploreChannel.description && <p className="channel-overview-description">{selectedExploreChannel.description}</p>}</div>
            <dl className="channel-overview-stats"><div><dt>구독자</dt><dd>{selectedExploreChannel.hiddenSubscriberCount ? "비공개" : formatCount(selectedExploreChannel.subscriberCount)}</dd></div><div><dt>채널 영상</dt><dd>{formatCount(selectedExploreChannel.youtubeVideoCount ?? selectedExploreChannel.videoCount)}</dd></div><div><dt>저장 영상</dt><dd>{formatCount(selectedExploreChannel.videoCount)}</dd></div><div><dt>수집 댓글</dt><dd>{formatCount(selectedExploreChannel.commentCount)}</dd></div></dl>
            {!selectedExploreChannel.hiddenSubscriberCount && <SubscriberTrend samples={subscriberHistory} />}
          </section>}
        </section>

        <section className="overview-intro" id="overview" aria-labelledby="overview-title" tabIndex={-1}>
          <div>
            <p className="section-kicker">MONITUBE / ANALYSIS WORKSPACE</p>
            <h1 id="overview-title">Channels</h1>
            <p>{sourceScope(activeSource)}</p>
          </div>
          {activeSource && (
            <div className="source-meta">
              <span className="source-type-chip">{sourceTypeCopy(activeSource.type)}</span>
              <span>{activeSource.enabled ? "수집 활성" : "수집 일시 중지"}</span>
            </div>
          )}
        </section>

        <section className="explore-section" id="explore" aria-label="Explore" tabIndex={-1}>
          {exploreError && <p className="inline-error" role="status">{exploreError}</p>}
          <form className="explore-search" role="search" onSubmit={submitCollectedSearch}>
            <label className="visually-hidden" htmlFor="collected-search-scope">검색 대상</label>
            <select
              id="collected-search-scope"
              className="explore-search-scope"
              value={searchScope}
              onChange={(event) => {
                const nextScope = event.target.value as CollectedSearchScope;
                setSearchScope(nextScope);
                if (searchQuery.trim().length >= 2) {
                  searchRequest.current += 1;
                  setSubmittedSearchQuery(searchQuery.trim());
                  setSubmittedSearchScope(nextScope);
                  setSearchResults(null);
                  setSearchError(null);
                  setIsSearchLoading(true);
                  setSearchRun((current) => current + 1);
                }
              }}
            >
              {Object.entries(searchScopeLabels).map(([value, label]) => <option key={value} value={value}>{label}</option>)}
            </select>
            <label className="visually-hidden" htmlFor="collected-search">수집 데이터 통합 검색</label>
            <input
              id="collected-search"
              value={searchQuery}
              onChange={(event) => setSearchQuery(event.target.value)}
              placeholder={searchScope === "videos" ? "영상 제목, 채널 검색" : searchScope === "comments" ? "댓글 검색" : "영상 제목, 채널, 댓글 검색"}
              type="search"
              autoComplete="off"
            />
            <button className="explore-search-submit" type="submit" aria-label="검색" disabled={searchQuery.trim().length < 2 || isSearchLoading}><MagnifyingGlassIcon aria-hidden="true" /></button>
          </form>
          {searchError && <p className="inline-error" role="status">{searchError}</p>}
          {isExploreLoading && explore.channels.length === 0 ? <p className="explore-loading">수집 라이브러리를 불러오는 중입니다…</p> : (
            hasSearchQuery ? (
              <section className="collected-search-results" aria-live="polite" aria-label="통합 검색 결과">
                {isSearchLoading && !searchResults ? (
                  <div className="search-results-skeleton" aria-busy="true" aria-label="검색 결과를 불러오는 중">
                    <div className="search-result-heading"><div><p className="section-kicker">UNIFIED RESULTS</p><h3>“{submittedSearchQuery}” 검색 중</h3></div></div>
                    <div className="search-result-columns">
                      {(submittedSearchScope === "all" ? ["동영상", "댓글"] : [searchScopeLabels[submittedSearchScope]]).map((title) => <section key={title}><h4>{title}</h4>{Array.from({ length: 5 }, (_, index) => <div className="search-skeleton-item" key={index}><span className="search-skeleton-line search-skeleton-title" /><span className="search-skeleton-line search-skeleton-copy" /><span className="search-skeleton-line search-skeleton-meta" /></div>)}</section>)}
                    </div>
                  </div>
                ) : (
                  <>
                    <div className="search-result-heading"><div><p className="section-kicker">UNIFIED RESULTS</p><h3>“{submittedSearchQuery}” 검색 결과</h3></div><span>{formatCount((searchResults?.videos.length ?? 0) + (searchResults?.comments.length ?? 0))}개</span></div>
                    <div className="search-result-columns">
                      {submittedSearchScope !== "comments" && <section><h4>동영상</h4>{searchResults?.videos.map((result) => <button className="search-video-result" type="button" key={result.video.id} onClick={(event) => openVideoDrawer(result.video, event.currentTarget)}><img src={youtubeThumbnail(result.video.youtubeVideoId)} alt="" /><span><strong>{result.video.title}</strong><small>{result.matchedFields.map(searchFieldLabel).join(" · ")} · 유사도 {Math.round(result.score * 100)}%</small></span><ChevronRightIcon aria-hidden="true" /></button>)}{!isSearchLoading && (searchResults?.videos.length ?? 0) === 0 && <p className="search-empty">일치하는 동영상이 없습니다.</p>}</section>}
                      {submittedSearchScope !== "videos" && <section><h4>댓글</h4>{searchResults?.comments.map((result) => <div className="search-comment-result" key={result.comment.id}><CommentRow compact comment={result.comment} context={<><strong>{result.video.title}</strong><small>{result.channelTitle ?? "수집 채널"} · {result.matchedFields.map(searchFieldLabel).join(" · ")} · 유사도 {Math.round(result.score * 100)}%</small></>} onOpenDetail={openCommentDetail} /></div>)}{!isSearchLoading && (searchResults?.comments.length ?? 0) === 0 && <p className="search-empty">일치하는 댓글이 없습니다.</p>}</section>}
                    </div>
                    {isSearchLoading && <p className="explore-loading">검색 결과를 갱신하는 중입니다…</p>}
                  </>
                )}
              </section>
            ) : (
            <>
              <div className="explore-channel-strip" aria-label="수집된 채널">
                {explore.channels.map((channel) => {
                  const coverVideo = explore.videos.find((video) => video.channelId === channel.youtubeChannelId);
                  const selected = channel.youtubeChannelId === exploreChannelId;
                  const avatarUrl = channel.thumbnailUrl ?? (coverVideo ? youtubeThumbnail(coverVideo.youtubeVideoId) : undefined);
                  return (
                    <button className={selected ? "explore-channel-avatar-button explore-channel-avatar-button-selected" : "explore-channel-avatar-button"} type="button" key={channel.youtubeChannelId} onClick={() => { const nextChannelId = selected ? null : channel.youtubeChannelId; setExploreChannelId(nextChannelId); setExploreVisibleCount(12); void refreshExplore(nextChannelId); }} aria-pressed={selected} aria-label={`${channel.title ?? channel.handle ?? channel.youtubeChannelId} 채널 개요 보기`} title={channel.title ?? channel.handle ?? channel.youtubeChannelId}>
                      {avatarUrl ? <img src={avatarUrl} alt="" /> : <span className="explore-avatar">{(channel.title ?? channel.handle ?? "Y").slice(0, 1).toUpperCase()}</span>}
                    </button>
                  );
                })}
                {explore.channels.length === 0 && <div className="explore-empty">아직 수집된 채널이 없습니다. 첫 수집을 시작하면 이곳에 자동으로 모입니다.</div>}
              </div>
              {selectedExploreChannel && (
                <section className="explore-channel-overview" aria-labelledby="channel-overview-title">
                  <div className="channel-overview-avatar">
                    {selectedExploreChannel.thumbnailUrl ? <img src={selectedExploreChannel.thumbnailUrl} alt="" /> : <span>{(selectedExploreChannel.title ?? selectedExploreChannel.handle ?? "Y").slice(0, 1).toUpperCase()}</span>}
                  </div>
                  <div className="channel-overview-copy">
                    <p className="section-kicker">CHANNEL OVERVIEW</p>
                    <h3 id="channel-overview-title">{selectedExploreChannel.title ?? selectedExploreChannel.handle ?? selectedExploreChannel.youtubeChannelId}</h3>
                    <p className="channel-overview-id">{selectedExploreChannel.handle ? `@${selectedExploreChannel.handle.replace(/^@/, "")} · ` : ""}{selectedExploreChannel.youtubeChannelId}</p>
                    {selectedExploreChannel.description && <p className="channel-overview-description">{selectedExploreChannel.description}</p>}
                  </div>
                  <dl className="channel-overview-stats">
                    <div><dt>구독자</dt><dd>{selectedExploreChannel.hiddenSubscriberCount ? "비공개" : formatCount(selectedExploreChannel.subscriberCount)}</dd></div>
                    <div><dt>채널 영상</dt><dd>{formatCount(selectedExploreChannel.youtubeVideoCount ?? selectedExploreChannel.videoCount)}</dd></div>
                    <div><dt>저장 영상</dt><dd>{formatCount(selectedExploreChannel.videoCount)}</dd></div>
                    <div><dt>수집 댓글</dt><dd>{formatCount(selectedExploreChannel.commentCount)}</dd></div>
                  </dl>
                  {!selectedExploreChannel.hiddenSubscriberCount && <SubscriberTrend samples={subscriberHistory} />}
                </section>
              )}
              <div className="explore-video-grid" aria-label="수집된 동영상">
                {visibleExploreVideos.map((video, index) => <button className={index === 0 ? "explore-video-card explore-video-card-featured" : "explore-video-card"} type="button" key={video.id} onClick={(event) => openVideoDrawer(video, event.currentTarget)}><img src={youtubeThumbnail(video.youtubeVideoId)} alt="" loading={index < 6 ? "eager" : "lazy"} /><span className="explore-video-shade" aria-hidden="true" /><span className="explore-video-date">{formatShortDate(video.publishedAt)}</span><strong>{video.title ?? video.youtubeVideoId}</strong><footer><span>조회 {formatCount(video.viewCount)}</span><span>댓글 {formatCount(video.commentCount)}</span></footer></button>)}
                {exploreVideos.length === 0 && <div className="explore-empty">조건에 맞는 저장 동영상이 없습니다.</div>}
              </div>
              {(exploreVideos.length > visibleExploreVideos.length || explore.nextOffset !== undefined || isExploreLoadingMore) && <div className="explore-load-more" ref={exploreLoadMoreRef} aria-live="polite">
                {isExploreLoadingMore && <span className="explore-load-more-spinner" aria-label="추가 동영상을 불러오는 중" />}
              </div>}
            </>
            )
          )}
        </section>

        {resultsError && <p className="inline-error" role="status">{resultsError}</p>}

        {!activeSourceId && !isSourcesLoading && (
          <section className="empty-overview" aria-labelledby="empty-overview-title">
            <DocumentChartBarIcon aria-hidden="true" />
            <div>
              <p className="section-kicker">READY WHEN YOU ARE</p>
              <h2 id="empty-overview-title">첫 수집 대상을 추가해 보세요.</h2>
              <p>채널, 키워드 또는 단일 동영상을 선택하면 공유 데이터와 분석 상태를 이곳에 정리해 드립니다.</p>
            </div>
            <button className="primary-action" type="button" onClick={openCollectionDrawer}>
              <PlusIcon aria-hidden="true" />
              수집 대상 추가
            </button>
          </section>
        )}

        {activeSourceId && isResultsLoading && !sourceResults && (
          <section className="empty-overview loading-overview" aria-live="polite">
            <ArrowPathIcon aria-hidden="true" />
            <div><h2>저장된 분석을 불러오는 중입니다.</h2><p>선택한 수집 대상의 최신 결과를 준비하고 있습니다.</p></div>
          </section>
        )}

        {sourceResults && sourceResults.source.id === activeSourceId && (
          <div className="dashboard-content">
            <section className="kpi-grid" aria-label="선택 수집 대상의 핵심 지표">
              <MetricCard
                label="수집 동영상"
                value={formatCount(analysisVideoCount)}
                detail="공유 수집 대상에 저장된 영상"
                icon={<DocumentChartBarIcon />}
              />
              <MetricCard
                label="수집 공개 댓글"
                value={formatCount(analysisCommentCount)}
                detail="페이지 상한 내 수집 결과"
                icon={<SparklesIcon />}
              />
              <MetricCard
                label="최근 업로드"
                value={formatKpiDate(latestVideoDate)}
                detail="저장된 동영상 게시일 기준"
                icon={<ClockIcon />}
              />
              <MetricCard
                label="수집 상태"
                value={failureReason ? "실패" : collectionStatusValue(activeJob)}
                detail={failureReason ? `실패 사유: ${failureReason}` : collectionStatusDetail(activeJob)}
                icon={<QueueListIcon />}
                accent={Boolean(activeJob && !isTerminalJob(activeJob))}
                failure={Boolean(failureReason)}
                onClick={failureReason ? () => { void openFailureHistory(); } : undefined}
              />
            </section>

            <div className="analysis-grid">
              <section className="panel top-videos-panel" aria-labelledby="top-videos-title">
                <div className="panel-heading">
                  <div>
                    <p className="section-kicker">LATEST SNAPSHOT</p>
                    <h2 id="top-videos-title">상위 영상 성과</h2>
                  </div>
                  <div className="metric-switch" role="group" aria-label="상위 영상 정렬 기준">
                    {(["views", "likes", "comments"] as ViewMetric[]).map((metric) => (
                      <button
                        key={metric}
                        className={viewMetric === metric ? "metric-switch-active" : undefined}
                        type="button"
                        aria-pressed={viewMetric === metric}
                        onClick={() => setViewMetric(metric)}
                      >
                        {metricLabel(metric)}
                      </button>
                    ))}
                  </div>
                </div>

                {rankedVideos.length === 0 ? (
                  <div className="panel-empty"><p>저장된 동영상이 생기면 최신 수치 기준의 상위 영상을 비교할 수 있습니다.</p></div>
                ) : (
                  <ol className="video-ranking">
                    {rankedVideos.map((video, index) => {
                      const amount = videoMetric(video, viewMetric);
                      const percent = Math.max(3, Math.round((amount / rankedMax) * 100));
                      return (
                        <li key={video.id}>
                          <button type="button" onClick={(event) => openVideoDrawer(video, event.currentTarget)}>
                            <span className="ranking-index">{String(index + 1).padStart(2, "0")}</span>
                            <span className="ranking-copy"><strong>{video.title}</strong><small>{formatShortDate(video.publishedAt)}</small></span>
                            <span className="ranking-bar" aria-hidden="true"><span className={index === 0 ? "ranking-bar-highlight" : undefined} style={{ width: `${percent}%` }} /></span>
                            <span className="ranking-value">{formatCount(amount)}</span>
                            <ChevronRightIcon aria-hidden="true" />
                          </button>
                        </li>
                      );
                    })}
                  </ol>
                )}
                <p className="panel-note">최신 수집 snapshot 기준 · 그래프는 현재 저장된 영상 통계만 사용합니다.</p>
              </section>

              <section className="panel collection-health-panel" id="collection-jobs" aria-labelledby="collection-health-title" tabIndex={-1}>
                <p className="visually-hidden" role="status" aria-live="polite">
                  {activeJob
                    ? `${statusCopy(activeJob)}. ${stageLabels[activeJob.currentStage] ?? activeJob.currentStage}`
                    : "수집 기록 없음"}
                </p>
                <div className="panel-heading">
                  <div>
                    <p className="section-kicker">COLLECTION HEALTH</p>
                    <h2 id="collection-health-title">수집 상태</h2>
                  </div>
                  <StatusPill job={activeJob} />
                </div>

                {!activeJob ? (
                  <div className="health-empty">
                    <ClockIcon aria-hidden="true" />
                    <p>아직 실행 기록이 없습니다. 수집 요청을 보내면 진행률과 재개 계획이 이곳에 표시됩니다.</p>
                  </div>
                ) : (
                  <div className="health-content">
                    <div className="health-stage">
                      <div>
                        <strong>{stageLabels[activeJob.currentStage] ?? activeJob.currentStage}</strong>
                        <span>{activeJob.progress.completed}{activeJob.progress.total ? ` / ${activeJob.progress.total}` : ""} {activeJob.progress.unit} 처리</span>
                      </div>
                      {activeJob.progress.total ? <strong>{progressPercent}%</strong> : null}
                    </div>
                    {activeJob.progress.total ? (
                      <div className="progress-bar" role="progressbar" aria-label="수집 진행률" aria-valuenow={progressPercent} aria-valuemin={0} aria-valuemax={100}>
                        <span style={{ width: `${progressPercent}%` }} />
                      </div>
                    ) : null}

                    {activeJob.state === "waiting_quota" && (
                      <div className="health-callout health-callout-waiting">
                        <ClockIcon aria-hidden="true" />
                        <div>
                          <strong>{activeJob.quotaBucket ? bucketLabels[activeJob.quotaBucket] : "Quota"} 대기</strong>
                          <p>{activeJob.pauseReason ?? "일일 quota가 소진되었습니다."}</p>
                          <small>{activeJob.resumeIsAutomatic ? "자동 재개 예정" : "수동 확인 필요"} · {formatReset(activeJob.resumeAt)}</small>
                        </div>
                      </div>
                    )}
                    {activeJob.state !== "waiting_quota" && activeJob.pauseReason && <p className="health-reason">{activeJob.pauseReason}</p>}
                    {activeJob.partialErrors.length > 0 && <p className="health-warning">부분 경고 {activeJob.partialErrors.length}건이 기록되었습니다.</p>}
                    <p className="job-reference">Job {activeJob.id}</p>
                  </div>
                )}
              </section>

              <section className="panel word-panel" id="insights" aria-labelledby="word-panel-title" tabIndex={-1}>
                <div className="panel-heading">
                  <div>
                    <p className="section-kicker">PUBLIC COMMENTS</p>
                    <h2 id="word-panel-title">댓글 주요 단어</h2>
                  </div>
                  <InformationCircleIcon className="panel-info-icon" aria-label="수집된 공개 댓글 기준" />
                </div>
                {topWords.length === 0 ? (
                  <div className="panel-empty"><p>수집된 공개 댓글이 생기면 자주 언급된 단어를 보여 드립니다.</p></div>
                ) : (
                  <ol className="word-ranking">
                    {topWords.slice(0, 10).map((word, index) => {
                      const count = word.count ?? 0;
                      const percent = Math.max(3, Math.round((count / wordMax) * 100));
                      return (
                        <li key={`${word.label}-${index}`}>
                          <span>{String(index + 1).padStart(2, "0")}</span>
                          <strong>{word.label}</strong>
                          <span className="word-bar" aria-hidden="true"><span style={{ width: `${percent}%` }} /></span>
                          <small>{formatCount(word.count)}</small>
                        </li>
                      );
                    })}
                  </ol>
                )}
                <p className="panel-note">수집된 공개 댓글 기준</p>
              </section>

              <section className="panel recent-videos-panel" aria-labelledby="recent-videos-title">
                <div className="panel-heading">
                  <div>
                    <p className="section-kicker">VIDEO LIBRARY</p>
                    <h2 id="recent-videos-title">최근 동영상</h2>
                  </div>
                  <span className="panel-count">
                    {sourceResults.videosTotal !== undefined && sourceResults.videosTotal !== videos.length
                      ? `${formatCount(videos.length)} / ${formatCount(sourceResults.videosTotal)}개`
                      : `${formatCount(videos.length)}개`}
                  </span>
                </div>

                {videos.length === 0 ? (
                  <div className="panel-empty"><p>아직 표시할 저장 동영상이 없습니다. 작업 완료 뒤 결과를 새로고침하세요.</p></div>
                ) : (
                  <>
                    <ul className="mobile-video-list" aria-label="최근 동영상 목록">
                      {videos.map((video) => (
                        <li key={video.id}>
                          <button type="button" onClick={(event) => openVideoDrawer(video, event.currentTarget)}>
                            <span className="mobile-video-copy">
                              <strong>{video.title}</strong>
                              <small>{formatShortDate(video.publishedAt)}</small>
                            </span>
                            <span className="mobile-video-metrics">
                              <span>조회 <strong>{formatCount(video.viewCount)}</strong></span>
                              <span>댓글 <strong>{formatCount(video.commentCount)}</strong></span>
                            </span>
                            <ChevronRightIcon aria-hidden="true" />
                          </button>
                        </li>
                      ))}
                    </ul>
                    <div className="video-table-wrap">
                    <table className="video-table">
                      <thead>
                        <tr><th scope="col">동영상</th><th scope="col">게시일</th><th scope="col">조회</th><th scope="col">좋아요</th><th scope="col">YouTube 댓글</th><th scope="col"><span className="visually-hidden">상세</span></th></tr>
                      </thead>
                      <tbody>
                        {videos.map((video) => (
                          <tr key={video.id}>
                            <td className="video-title-cell"><strong>{video.title}</strong><span>{video.youtubeVideoId}</span></td>
                            <td>{formatShortDate(video.publishedAt)}</td>
                            <td>{formatCount(video.viewCount)}</td>
                            <td>{formatCount(video.likeCount)}</td>
                            <td>{formatCount(video.commentCount)}</td>
                            <td><button className="table-open" type="button" aria-label={`${video.title} 댓글과 상세 보기`} onClick={(event) => openVideoDrawer(video, event.currentTarget)}><ChevronRightIcon aria-hidden="true" /></button></td>
                          </tr>
                        ))}
                      </tbody>
                    </table>
                  </div>
                  </>
                )}
                {sourceVideosError && (
                  <div className="source-videos-load-error" role="status">
                    <span>{sourceVideosError}</span>
                    <button type="button" onClick={() => void loadMoreSourceVideos()}>다시 시도</button>
                  </div>
                )}
                {!sourceVideosError && (sourceResults.videosNextCursor || isSourceVideosLoadingMore) && (
                  <div className="source-videos-load-more" ref={sourceVideosLoadMoreRef} aria-live="polite">
                    {isSourceVideosLoadingMore
                      ? <span><span className="loading-spinner" aria-hidden="true" />동영상을 더 불러오는 중</span>
                      : <button type="button" onClick={() => void loadMoreSourceVideos()}>동영상 더 보기</button>}
                  </div>
                )}
              </section>
            </div>
          </div>
        )}

        <footer className="dashboard-footer">
          <p>공개 YouTube 메타데이터와 공개 댓글을 수집 대상별로 공유·분석합니다.</p>
          <p>운영 credential은 브라우저에 노출되지 않습니다.</p>
        </footer>
      </main>

      {isSettingsOpen && (
        <div className="drawer-layer">
          <div className="drawer-backdrop" aria-hidden="true" onClick={closeSettingsDrawer} />
          <aside ref={settingsDrawerRef} className="settings-drawer" role="dialog" aria-modal="true" aria-labelledby="settings-drawer-title" aria-describedby="settings-drawer-description" tabIndex={-1}>
            <div className="drawer-heading">
              <div>
                <p className="section-kicker">PERSONAL DEFAULTS</p>
                <h2 id="settings-drawer-title">기본 설정</h2>
                <p id="settings-drawer-description">{authUser} 계정에서 새 수집 대상을 추가할 때 사용할 기본값입니다.</p>
              </div>
              <button className="icon-button" type="button" aria-label="기본 설정 창 닫기" data-drawer-initial-focus onClick={closeSettingsDrawer}><XMarkIcon aria-hidden="true" /></button>
            </div>
            <form className="settings-form" onSubmit={saveSettings}>
              <section className="settings-section" aria-labelledby="settings-collection-title">
                <div><p className="section-kicker">NEW COLLECTION</p><h3 id="settings-collection-title">새 수집 기본값</h3></div>
                <div className="drawer-field-grid">
                  <label className="drawer-field drawer-field-wide">
                    <span>기본 수집 대상</span>
                    <select value={settingsDraft.defaultSourceType} onChange={(event) => setSettingsDraft((current) => ({ ...current, defaultSourceType: event.target.value as CollectionPreferences["defaultSourceType"] }))}>
                      <option value="channel">채널</option>
                      <option value="keyword">키워드</option>
                    </select>
                    <small>수집 대상 추가 창을 열 때 먼저 선택할 유형입니다.</small>
                  </label>
                  <label className="toggle-field drawer-field-wide">
                    <input type="checkbox" checked={settingsDraft.includeComments} onChange={(event) => setSettingsDraft((current) => ({ ...current, includeComments: event.target.checked }))} />
                    <span className="toggle-visual" aria-hidden="true" />
                    <span><strong>공개 댓글 포함</strong><small>새 채널·키워드 수집에서 공개 댓글을 기본으로 함께 수집합니다.</small></span>
                  </label>
                </div>
              </section>
              <section className="settings-section" aria-labelledby="settings-keyword-title">
                <div><p className="section-kicker">KEYWORD SEARCH</p><h3 id="settings-keyword-title">키워드 검색 기본값</h3></div>
                <div className="drawer-field-grid">
                  <label className="drawer-field drawer-field-wide">
                    <span>정렬</span>
                    <select value={settingsDraft.order} onChange={(event) => setSettingsDraft((current) => ({ ...current, order: event.target.value as CollectionPreferences["order"] }))}>
                      <option value="date">최신순</option>
                      <option value="relevance">관련도순</option>
                      <option value="viewCount">조회수순</option>
                    </select>
                  </label>
                  <label className="drawer-field">
                    <span>검색 언어</span>
                    <input value={settingsDraft.relevanceLanguage} onChange={(event) => setSettingsDraft((current) => ({ ...current, relevanceLanguage: event.target.value }))} placeholder="ko" minLength={2} maxLength={10} pattern="[A-Za-z]{2,3}(?:-[A-Za-z0-9]{2,6})?" autoComplete="off" />
                    <small>예: ko, en</small>
                  </label>
                  <label className="drawer-field">
                    <span>지역 코드</span>
                    <input value={settingsDraft.regionCode} onChange={(event) => setSettingsDraft((current) => ({ ...current, regionCode: event.target.value }))} placeholder="KR" minLength={2} maxLength={2} pattern="[A-Za-z]{2}" autoComplete="off" />
                    <small>ISO 국가 코드, 예: KR, US</small>
                  </label>
                </div>
              </section>
              <p className="settings-storage-note"><InformationCircleIcon aria-hidden="true" />이 설정은 현재 브라우저에 계정별로 저장되며 API 키 같은 운영 비밀값은 포함하지 않습니다.</p>
              <div className="drawer-footer-action settings-footer-action">
                <button className="secondary-action settings-reset-action" type="button" onClick={resetSettings}>설정 초기화</button>
                <button className="primary-action" type="submit">설정 저장</button>
              </div>
            </form>
          </aside>
        </div>
      )}

      {isCollectionOpen && (
        <div className="drawer-layer">
          <div className="drawer-backdrop" aria-hidden="true" onClick={closeCollectionDrawer} />
          <aside ref={collectionDrawerRef} className="collection-drawer" role="dialog" aria-modal="true" aria-labelledby="collection-drawer-title" tabIndex={-1}>
            <div className="drawer-heading">
              <div><p className="section-kicker">COLLECTION TARGET</p><h2 id="collection-drawer-title">수집 대상 추가</h2><p>같은 대상은 하나로 관리하고, 수집 결과를 함께 최신화합니다.</p></div>
              <button className="icon-button" type="button" aria-label="수집 대상 추가 창 닫기" data-drawer-initial-focus onClick={closeCollectionDrawer}><XMarkIcon aria-hidden="true" /></button>
            </div>

            <form className="collection-form" onSubmit={(event) => { event.preventDefault(); void launchJob(); }}>
              <fieldset className="source-type-group">
                <legend>수집 대상</legend>
                <div className="source-type-tabs" role="tablist" aria-label="수집 대상 유형">
                  {sourceTypeChoices.map(({ type, label, detail, Icon }) => (
                    <button
                      key={type}
                      type="button"
                      className={form.sourceType === type ? "source-type-choice source-type-choice-active" : "source-type-choice"}
                      onClick={() => update("sourceType", type)}
                      role="tab"
                      aria-selected={form.sourceType === type}
                    >
                      <Icon aria-hidden="true" />
                      <span><strong>{label}</strong><small>{detail}</small></span>
                    </button>
                  ))}
                </div>
              </fieldset>

              {form.sourceType === "channel" ? (
                <label className="drawer-field drawer-field-wide">
                  <span>채널 URL, @handle 또는 채널 ID</span>
                  <input value={form.channelInput} onChange={(event) => update("channelInput", event.target.value)} placeholder="예: @GoogleDevelopers 또는 @우정잉" autoComplete="off" />
                  <small>한글·유니코드 핸들도 지원합니다. 채널 전체 업로드는 업로드 재생목록을 기준으로 수집합니다.</small>
                </label>
              ) : (
                <div className="drawer-field-grid">
                  <label className="drawer-field drawer-field-wide"><span>검색 키워드</span><input value={form.keyword} onChange={(event) => update("keyword", event.target.value)} placeholder="예: 생성형 AI 교육" autoComplete="off" /></label>
                  <label className="drawer-field"><span>시작일</span><input type="date" value={form.publishedAfter} onChange={(event) => update("publishedAfter", event.target.value)} /></label>
                  <label className="drawer-field"><span>종료일</span><input type="date" value={form.publishedBefore} onChange={(event) => update("publishedBefore", event.target.value)} /></label>
                  <label className="drawer-field"><span>정렬</span><select value={form.order} onChange={(event) => update("order", event.target.value as KeywordSourceConfig["order"])}><option value="date">최신순</option><option value="relevance">관련성순</option><option value="viewCount">조회수순</option></select></label>
                  <label className="drawer-field"><span>관련 언어</span><input value={form.relevanceLanguage} maxLength={8} onChange={(event) => update("relevanceLanguage", event.target.value)} placeholder="ko" /></label>
                  <label className="drawer-field"><span>지역</span><input value={form.regionCode} maxLength={2} onChange={(event) => update("regionCode", event.target.value)} placeholder="KR" /></label>
                </div>
              )}

              <section className="drawer-scope" aria-labelledby="scope-title">
                <div><p className="section-kicker">SCOPE</p><h3 id="scope-title">수집 범위</h3><p>{form.sourceType === "channel" ? "채널은 현재 공개된 전체 업로드를 수집합니다." : "선택한 대상의 공개 메타데이터를 수집합니다."} quota가 소진되면 1~3시간 간격으로 자동 재시도합니다.</p></div>
                <div className="scope-controls">
                  <label className="toggle-field"><input type="checkbox" checked={form.includeComments} onChange={(event) => update("includeComments", event.target.checked)} /><span className="toggle-visual" aria-hidden="true" /><span><strong>공개 댓글 전체 수집</strong><small>댓글이 비활성화된 영상은 부분 경고로 남습니다.</small></span></label>
                </div>
              </section>

              <div className="drawer-footer-action">
                <button className="secondary-action" type="button" onClick={closeCollectionDrawer}>취소</button>
                <button className="primary-action drawer-start-action" type="submit" disabled={isStarting}>{isStarting ? "요청을 연결하는 중…" : "수집 요청 보내기"}<ChevronRightIcon aria-hidden="true" /></button>
              </div>
            </form>
          </aside>
        </div>
      )}

      {(selectedVideo || selectedCommentId) && (
        <div className="video-modal-layer">
          <div className="video-modal-backdrop" aria-hidden="true" onClick={selectedVideo ? closeVideoDrawer : closeCommentDetail} />
          <section ref={videoModalRef} className="video-modal" role="dialog" aria-modal="true" aria-labelledby={selectedCommentId ? "comment-detail-title" : "video-modal-title"} tabIndex={-1}>
            <div className="video-modal-toolbar">
              {selectedCommentId && selectedVideo ? (
                <button className="icon-button" type="button" aria-label="영상 댓글로 돌아가기" data-drawer-initial-focus onClick={closeCommentDetail}><ArrowLeftIcon aria-hidden="true" /></button>
              ) : <span />}
              <button className="icon-button" type="button" aria-label="상세 팝업 닫기" data-drawer-initial-focus={!selectedCommentId || !selectedVideo ? true : undefined} onClick={selectedVideo ? closeVideoDrawer : closeCommentDetail}><XMarkIcon aria-hidden="true" /></button>
            </div>
            <div className="video-modal-scroll">
              {selectedCommentId ? (
                <div className="comment-detail-view">
                  <header className="comment-detail-header">
                    <p className="section-kicker">COMMENT DETAIL</p>
                    <h2 id="comment-detail-title">댓글 스레드</h2>
                    {activeModalVideo && <p>{activeModalVideo.title}</p>}
                  </header>
                  {isCommentDetailLoading && <div className="comments-loading" role="status"><span className="loading-spinner" aria-hidden="true" />댓글 상세 정보를 불러오는 중입니다.</div>}
                  {commentDetailError && <div className="inline-error" role="status">{commentDetailError}</div>}
                  {selectedCommentDetail && selectedDetailThread && <>
                    <section className="comment-detail-thread" aria-label="선택한 댓글 스레드">
                      <CommentThread item={selectedDetailThread} onOpenDetail={openCommentDetail} selectedCommentId={selectedCommentId} />
                    </section>
                    <section className="comment-detail-video-section">
                      <p className="section-kicker">ON VIDEO</p>
                      <button className="comment-detail-video" type="button" onClick={(event) => { setSelectedCommentId(null); setSelectedCommentDetail(null); openVideoDrawer(selectedCommentDetail.video, event.currentTarget); }}>
                        <img src={youtubeThumbnail(selectedCommentDetail.video.youtubeVideoId)} alt="" />
                        <span><strong>{selectedCommentDetail.video.title}</strong><small>{selectedCommentDetail.video.youtubeVideoId}</small></span>
                        <ChevronRightIcon aria-hidden="true" />
                      </button>
                    </section>
                    <section className="comment-author-comments" aria-labelledby="author-comments-title">
                      <div><p className="section-kicker">SAME AUTHOR</p><h3 id="author-comments-title">이 작성자의 다른 댓글</h3></div>
                      {!selectedCommentDetail.comment.authorChannelId && <p className="comments-loading">저장된 작성자 식별 정보가 없어 다른 댓글을 연결할 수 없습니다.</p>}
                      {selectedCommentDetail.comment.authorChannelId && selectedCommentDetail.authorComments.length === 0 && <p className="comments-loading">같은 작성자의 다른 저장 댓글이 아직 없습니다.</p>}
                      {selectedCommentDetail.authorComments.length > 0 && <div className="comment-author-list">{selectedCommentDetail.authorComments.map((item) => <div className="comment-author-result" key={item.comment.id}><CommentRow compact comment={item.comment} context={<strong>{item.video.title}</strong>} onOpenDetail={openCommentDetail} /></div>)}</div>}
                    </section>
                  </>}
                </div>
              ) : selectedVideo ? (
                <>
                  <header className="video-modal-hero">
                    <img className="video-modal-thumbnail" src={youtubeThumbnail(selectedVideo.youtubeVideoId)} alt={`${selectedVideo.title} 썸네일`} />
                    <div className="video-modal-summary">
                      <p className="section-kicker">VIDEO DETAIL</p>
                      <h2 id="video-modal-title">{selectedVideo.title}</h2>
                      <p className="video-modal-id">{selectedVideo.youtubeVideoId}</p>
                      <p className="video-modal-description">{selectedVideo.description ?? "저장된 영상 설명이 없습니다."}</p>
                      <dl className="video-meta-grid"><div><dt>게시일</dt><dd>{formatDate(selectedVideo.publishedAt)}</dd></div><div><dt>길이</dt><dd>{formatDuration(selectedVideo.durationSeconds)}</dd></div><div><dt>조회</dt><dd>{formatCount(selectedVideo.viewCount)}</dd></div><div><dt>좋아요</dt><dd>{formatCount(selectedVideo.likeCount)}</dd></div><div><dt>YouTube 댓글</dt><dd>{formatCount(selectedVideo.commentCount)}</dd></div><div><dt>공개 상태</dt><dd>{selectedVideo.privacyStatus ?? "—"}</dd></div></dl>
                    </div>
                  </header>
                  <section className="video-comments" aria-labelledby="comments-title" aria-busy={isCommentsLoading}>
                    <div className="video-comments-heading">
                      <div><p className="section-kicker">PUBLIC COMMENTS</p><h3 id="comments-title">수집된 공개 댓글</h3></div>
                      <div className="video-comments-actions">
                        <label className="comment-sort-select">
                          <span>댓글 정렬</span>
                          <select
                            aria-label="댓글 정렬"
                            value={commentSort}
                            onChange={(event) => changeCommentSort(event.target.value as CommentThreadSort)}
                          >
                            {(Object.entries(commentSortLabels) as Array<[CommentThreadSort, string]>).map(([value, label]) => <option value={value} key={value}>{label}</option>)}
                          </select>
                        </label>
                        <button className="icon-button" type="button" aria-label="댓글 새로고침" disabled={isCommentsLoading} onClick={() => void loadComments(selectedVideo)}><ArrowPathIcon aria-hidden="true" /></button>
                      </div>
                    </div>
                    {commentsError && <div className="comment-load-error" role="status"><span>{commentsError}</span><button type="button" onClick={() => void loadComments(selectedVideo)}>다시 시도</button></div>}
                    {isCommentsLoading && commentThreads.length === 0 && <div className="comments-loading" role="status"><span className="loading-spinner" aria-hidden="true" />공개 댓글을 불러오는 중입니다.</div>}
                    {!isCommentsLoading && !commentsError && commentThreads.length === 0 && <p className="comments-loading">저장된 공개 댓글이 없거나 댓글 수집이 선택되지 않았습니다.</p>}
                    {commentThreads.length > 0 && <div className="yt-comment-list">{commentThreads.map((item) => <CommentThread item={item} onOpenDetail={openCommentDetail} key={item.comment.id} />)}</div>}
                    <div className="comment-load-sentinel" ref={commentThreadsLoadMoreRef} aria-hidden="true" />
                    {isCommentsLoading && commentThreads.length > 0 && <div className="comment-list-loading" role="status"><span className="loading-spinner" aria-hidden="true" />댓글을 더 불러오는 중</div>}
                    {nextCommentsCursor && !isCommentsLoading && <button className="comments-load-more" type="button" onClick={() => void loadComments(selectedVideo, nextCommentsCursor)}>댓글 더 보기</button>}
                  </section>
                </>
              ) : null}
            </div>
          </section>
        </div>
      )}

      {(notice || error) && <div className={error ? "toast toast-error" : "toast"} role="status"><span aria-hidden="true">{error ? <ExclamationTriangleIcon /> : <CheckCircleIcon />}</span><p>{error ?? notice}</p></div>}
    </div>
  );
}
