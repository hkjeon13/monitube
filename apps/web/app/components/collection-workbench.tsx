"use client";

import {
  ArrowPathIcon,
  CheckCircleIcon,
  ChevronRightIcon,
  ClockIcon,
  DocumentChartBarIcon,
  ExclamationTriangleIcon,
  FolderIcon,
  HomeIcon,
  InformationCircleIcon,
  MagnifyingGlassIcon,
  PlayIcon,
  PlusIcon,
  QueueListIcon,
  SparklesIcon,
  XMarkIcon,
} from "@heroicons/react/24/outline";
import type {
  ChannelSourceConfig,
  CollectionSourceType,
  CreateCollectionSourceRequest,
  JobStatus,
  KeywordSourceConfig,
  QuotaBucket,
  QuotaEstimate,
  VideoSourceConfig,
} from "@monitube/contracts";
import type { MouseEvent, ReactNode } from "react";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";

import {
  createSource,
  getJob,
  getSourceResults,
  getVideoComments,
  listSources,
  startJob,
  type CollectedComment,
  type CollectedVideo,
  type SourceResults,
  type SourceSummary,
} from "../lib/api";

type EstimateMode = "local" | null;
type ViewMetric = "views" | "likes" | "comments";
type NavItem = "overview" | "sources" | "jobs" | "insights";
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
  maxVideos: number;
  maxPagesPerRun: number;
  includeComments: boolean;
  maxCommentPagesPerVideo: number;
};

const initialForm: FormState = {
  sourceType: "channel",
  channelInput: "",
  videoInput: "",
  keyword: "",
  publishedAfter: "",
  publishedBefore: "",
  relevanceLanguage: "ko",
  regionCode: "KR",
  order: "date",
  maxVideos: 50,
  maxPagesPerRun: 3,
  includeComments: true,
  maxCommentPagesPerVideo: 1,
};

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
  fetching_comments: "공개 댓글을 불러오는 중",
  persisting: "결과를 저장하는 중",
  analyzing: "분석을 준비하는 중",
};

const sourceTypeChoices = [
  { type: "channel" as const, label: "채널", detail: "업로드 동영상 기준", Icon: FolderIcon },
  { type: "keyword" as const, label: "키워드", detail: "검색 run별 발견 결과", Icon: MagnifyingGlassIcon },
  { type: "video" as const, label: "동영상", detail: "URL 또는 ID 직접 선택", Icon: PlayIcon },
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

function sourceRequest(form: FormState): CreateCollectionSourceRequest {
  if (form.sourceType === "channel") {
    const config: ChannelSourceConfig = {
      input: form.channelInput.trim(),
      includeComments: form.includeComments,
      maxVideos: clampPositive(form.maxVideos, 50),
      maxCommentPagesPerVideo: clampPositive(form.maxCommentPagesPerVideo, 1),
    };
    return { type: "channel", config };
  }

  if (form.sourceType === "video") {
    const config: VideoSourceConfig = {
      input: form.videoInput.trim(),
      includeComments: form.includeComments,
      maxCommentPagesPerVideo: clampPositive(form.maxCommentPagesPerVideo, 1),
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
    maxPagesPerRun: clampPositive(form.maxPagesPerRun, 1),
    includeComments: form.includeComments,
    maxCommentPagesPerVideo: clampPositive(form.maxCommentPagesPerVideo, 1),
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

function localEstimate(requestBody: CreateCollectionSourceRequest): QuotaEstimate[] {
  const upperBoundVideos =
    requestBody.type === "channel"
      ? (requestBody.config as ChannelSourceConfig).maxVideos
      : requestBody.type === "keyword"
        ? (requestBody.config as KeywordSourceConfig).maxPagesPerRun * 50
        : 1;
  const comments = requestBody.config.includeComments
    ? upperBoundVideos * requestBody.config.maxCommentPagesPerVideo
    : 0;
  const core =
    requestBody.type === "channel"
      ? 1 + Math.ceil(upperBoundVideos / 50) * 2 + comments
      : requestBody.type === "keyword"
        ? (requestBody.config as KeywordSourceConfig).maxPagesPerRun + comments
        : 1 + comments;
  const resetAt = "서버 quota 상태를 확인하면 reset 시각이 표시됩니다.";

  return [
    ...(requestBody.type === "keyword"
      ? [{
          bucket: "search_queries" as const,
          estimatedCalls: (requestBody.config as KeywordSourceConfig).maxPagesPerRun,
          estimatedUnits: (requestBody.config as KeywordSourceConfig).maxPagesPerRun,
          limit: 100,
          resetAt,
        }]
      : []),
    {
      bucket: "core" as const,
      estimatedCalls: core,
      estimatedUnits: core,
      limit: 10_000,
      resetAt,
    },
  ];
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
  return "수집 source";
}

function sourceLabel(source: SourceSummary) {
  const input = source.config.input;
  const query = source.config.query;
  const value = typeof query === "string" ? query : typeof input === "string" ? input : source.id;
  return `${sourceTypeCopy(source.type)} · ${value}`;
}

function sourceScope(source?: SourceSummary) {
  if (!source) return "source를 선택하면 범위가 표시됩니다.";
  const config = source.config;
  if (source.type === "keyword") {
    const terms = [
      typeof config.query === "string" ? `“${config.query}”` : "키워드 검색",
      typeof config.relevanceLanguage === "string" ? config.relevanceLanguage.toUpperCase() : undefined,
      typeof config.regionCode === "string" ? config.regionCode.toUpperCase() : undefined,
    ].filter(Boolean);
    return terms.join(" · ");
  }
  const input = typeof config.input === "string" ? config.input : source.id;
  return `${sourceTypeCopy(source.type)} · ${input}`;
}

function isTerminalJob(job: JobStatus | null | undefined) {
  return job ? ["completed", "completed_with_warnings", "failed", "cancelled"].includes(job.state) : false;
}

function mergeComments(current: CollectedComment[], incoming: CollectedComment[]) {
  const seen = new Set(current.map((comment) => comment.id));
  return [...current, ...incoming.filter((comment) => !seen.has(comment.id))];
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

function MetricCard({
  label,
  value,
  detail,
  icon,
  accent = false,
}: {
  label: string;
  value: string;
  detail: string;
  icon: ReactNode;
  accent?: boolean;
}) {
  return (
    <article className={accent ? "metric-card metric-card-accent" : "metric-card"}>
      <div className="metric-card-head">
        <span>{label}</span>
        <span className="metric-icon" aria-hidden="true">{icon}</span>
      </div>
      <strong>{value}</strong>
      <small>{detail}</small>
    </article>
  );
}

export function CollectionWorkbench() {
  const [form, setForm] = useState<FormState>(initialForm);
  const [estimates, setEstimates] = useState<QuotaEstimate[]>([]);
  const [estimateWarnings, setEstimateWarnings] = useState<string[]>([]);
  const [estimateMode, setEstimateMode] = useState<EstimateMode>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [isEstimating, setIsEstimating] = useState(false);
  const [isStarting, setIsStarting] = useState(false);
  const [job, setJob] = useState<JobStatus | null>(null);
  const [jobSourceId, setJobSourceId] = useState<string | null>(null);
  const [sources, setSources] = useState<SourceSummary[]>([]);
  const [activeSourceId, setActiveSourceId] = useState("");
  const [isSourcesLoading, setIsSourcesLoading] = useState(false);
  const [sourceResults, setSourceResults] = useState<SourceResults | null>(null);
  const [isResultsLoading, setIsResultsLoading] = useState(false);
  const [resultsError, setResultsError] = useState<string | null>(null);
  const [selectedVideo, setSelectedVideo] = useState<CollectedVideo | null>(null);
  const [comments, setComments] = useState<CollectedComment[]>([]);
  const [nextCommentsCursor, setNextCommentsCursor] = useState<string | undefined>();
  const [isCommentsLoading, setIsCommentsLoading] = useState(false);
  const [commentsError, setCommentsError] = useState<string | null>(null);
  const [isCollectionOpen, setIsCollectionOpen] = useState(false);
  const [viewMetric, setViewMetric] = useState<ViewMetric>("views");
  const [activeNav, setActiveNav] = useState<NavItem>("overview");
  const resultsRequest = useRef(0);
  const commentsRequest = useRef(0);
  const collectionTriggerRef = useRef<HTMLElement | null>(null);
  const videoTriggerRef = useRef<HTMLElement | null>(null);
  const collectionDrawerRef = useRef<HTMLElement | null>(null);
  const videoDrawerRef = useRef<HTMLElement | null>(null);

  const requestBody = useMemo(() => sourceRequest(form), [form]);
  const validationError = useMemo(() => validate(requestBody), [requestBody]);
  const sourceName = form.sourceType === "channel" ? "채널" : form.sourceType === "keyword" ? "키워드 검색" : "동영상";
  const activeSource = sourceResults?.source ?? sources.find((source) => source.id === activeSourceId);
  const activeJob = job && jobSourceId === activeSourceId ? job : sourceResults?.latestJob;
  const videos = sourceResults?.videos ?? [];
  const rankedVideos = useMemo(
    () => [...videos].sort((left, right) => videoMetric(right, viewMetric) - videoMetric(left, viewMetric)).slice(0, 6),
    [videos, viewMetric],
  );
  const rankedMax = Math.max(1, ...rankedVideos.map((video) => videoMetric(video, viewMetric)));
  const topWords = sourceResults?.analysis?.topWords.length
    ? sourceResults.analysis.topWords
    : sourceResults?.commentSummary?.topWords ?? [];
  const wordMax = Math.max(1, ...topWords.map((word) => word.count ?? 0));
  const analysisVideoCount = sourceResults?.analysis?.videoCount ?? videos.length;
  const analysisCommentCount = sourceResults?.analysis?.commentCount ?? sourceResults?.commentSummary?.total;
  const latestVideoDate = sourceResults?.analysis?.latestVideoPublishedAt
    ?? [...videos].sort((left, right) => new Date(right.publishedAt ?? 0).getTime() - new Date(left.publishedAt ?? 0).getTime())[0]?.publishedAt;
  const progressPercent = activeJob?.progress.total
    ? Math.min(100, Math.round((activeJob.progress.completed / activeJob.progress.total) * 100))
    : 0;

  const update = <K extends keyof FormState>(key: K, value: FormState[K]) => {
    setForm((current) => ({ ...current, [key]: value }));
    setEstimates([]);
    setEstimateMode(null);
    setError(null);
  };

  const refreshSources = useCallback(async () => {
    setIsSourcesLoading(true);
    try {
      const nextSources = await listSources();
      setSources(nextSources);
      setActiveSourceId((current) => {
        if (current && nextSources.some((source) => source.id === current)) return current;
        return nextSources[0]?.id ?? "";
      });
    } catch {
      setResultsError("수집 source 목록을 불러오지 못했습니다. API 연결 상태를 확인하세요.");
    } finally {
      setIsSourcesLoading(false);
    }
  }, []);

  const refreshResults = useCallback(async (sourceId: string) => {
    if (!sourceId) return;
    const requestId = ++resultsRequest.current;
    setIsResultsLoading(true);
    setResultsError(null);
    try {
      const nextResults = await getSourceResults(sourceId);
      if (requestId === resultsRequest.current) setSourceResults(nextResults);
    } catch (caught) {
      if (requestId === resultsRequest.current) {
        setResultsError(caught instanceof Error ? caught.message : "수집 결과를 불러오지 못했습니다.");
      }
    } finally {
      if (requestId === resultsRequest.current) setIsResultsLoading(false);
    }
  }, []);

  const loadComments = useCallback(async (video: CollectedVideo, cursor?: string) => {
    const requestId = ++commentsRequest.current;
    setSelectedVideo(video);
    setIsCommentsLoading(true);
    setCommentsError(null);
    try {
      const response = await getVideoComments(video.id, cursor);
      if (requestId !== commentsRequest.current) return;
      setComments((current) => cursor ? mergeComments(current, response.comments) : response.comments);
      setNextCommentsCursor(response.nextCursor);
    } catch (caught) {
      if (requestId !== commentsRequest.current) return;
      setCommentsError(caught instanceof Error ? caught.message : "댓글을 불러오지 못했습니다.");
      if (!cursor) {
        setComments([]);
        setNextCommentsCursor(undefined);
      }
    } finally {
      if (requestId === commentsRequest.current) setIsCommentsLoading(false);
    }
  }, []);

  const restoreFocus = useCallback((trigger: HTMLElement | null) => {
    window.requestAnimationFrame(() => trigger?.focus());
  }, []);

  const closeCollectionDrawer = useCallback(() => {
    setIsCollectionOpen(false);
    restoreFocus(collectionTriggerRef.current);
  }, [restoreFocus]);

  const closeVideoDrawer = useCallback(() => {
    setSelectedVideo(null);
    restoreFocus(videoTriggerRef.current);
  }, [restoreFocus]);

  const openCollectionDrawer = (event: MouseEvent<HTMLButtonElement>) => {
    collectionTriggerRef.current = event.currentTarget;
    setIsCollectionOpen(true);
  };

  const openVideoDrawer = useCallback((video: CollectedVideo, trigger: HTMLElement) => {
    videoTriggerRef.current = trigger;
    void loadComments(video);
  }, [loadComments]);

  useEffect(() => {
    void refreshSources();
  }, [refreshSources]);

  useEffect(() => {
    if (!activeSourceId) {
      setSourceResults(null);
      return;
    }
    setSelectedVideo(null);
    setComments([]);
    setNextCommentsCursor(undefined);
    setCommentsError(null);
    void refreshResults(activeSourceId);
  }, [activeSourceId, refreshResults]);

  useEffect(() => {
    if (!activeJob || isTerminalJob(activeJob)) return;
    const jobId = activeJob.id;
    const sourceId = activeSourceId;
    const poll = async () => {
      try {
        const nextJob = await getJob(jobId);
        setJob(nextJob);
        setJobSourceId(sourceId);
      } catch {
        // Keep the last valid state visible through a transient polling failure.
      }
    };
    const timer = window.setInterval(() => { void poll(); }, 5_000);
    return () => window.clearInterval(timer);
  }, [activeJob, activeSourceId]);

  useEffect(() => {
    if (jobSourceId && isTerminalJob(job)) void refreshResults(jobSourceId);
  }, [job, jobSourceId, refreshResults]);

  useEffect(() => {
    const drawer = isCollectionOpen ? collectionDrawerRef.current : selectedVideo ? videoDrawerRef.current : null;
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
        if (isCollectionOpen) closeCollectionDrawer();
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
  }, [closeCollectionDrawer, closeVideoDrawer, isCollectionOpen, selectedVideo]);

  const loadEstimate = useCallback(async () => {
    if (validationError) {
      setError(validationError);
      return;
    }
    setIsEstimating(true);
    setError(null);
    setNotice(null);
    setEstimates(localEstimate(requestBody));
    setEstimateWarnings([
      "예상치는 보수적 계산입니다. 실제 예약과 재개 시각은 서버가 관리합니다.",
    ]);
    setEstimateMode("local");
    setIsEstimating(false);
  }, [requestBody, validationError]);

  const launchJob = useCallback(async () => {
    if (validationError) {
      setError(validationError);
      return;
    }
    if (!estimateMode) {
      setError("먼저 수집 범위의 quota 예상치를 확인하세요.");
      return;
    }

    setIsStarting(true);
    setError(null);
    setNotice(null);
    try {
      const source = await createSource(requestBody);
      const response = await startJob(source.id, {
        include_comments: requestBody.config.includeComments,
        ...(requestBody.type === "channel"
          ? { max_videos: (requestBody.config as ChannelSourceConfig).maxVideos }
          : {}),
        max_comments_per_video: requestBody.config.maxCommentPagesPerVideo,
      });
      setSources((current) => [source, ...current.filter((item) => item.id !== source.id)]);
      setActiveSourceId(source.id);
      setJobSourceId(source.id);
      setJob(response);
      closeCollectionDrawer();
      setNotice("수집 작업을 시작했습니다. Overview에서 상태를 자동으로 갱신합니다.");
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "수집 작업을 시작하지 못했습니다.");
    } finally {
      setIsStarting(false);
    }
  }, [closeCollectionDrawer, estimateMode, requestBody, validationError]);

  const navigateTo = (item: NavItem, target: string) => {
    setActiveNav(item);
    const targetElement = document.getElementById(target);
    targetElement?.scrollIntoView({ behavior: "smooth", block: "start" });
    targetElement?.focus({ preventScroll: true });
  };

  const navigation = [
    { id: "overview" as const, label: "Overview", target: "overview", Icon: HomeIcon },
    { id: "sources" as const, label: "Sources", target: "source-selector", Icon: FolderIcon },
    { id: "jobs" as const, label: "Collection jobs", target: "collection-jobs", Icon: QueueListIcon },
    { id: "insights" as const, label: "Insights", target: "insights", Icon: SparklesIcon },
  ];

  return (
    <div className="app-shell">
      <aside className="sidebar" aria-label="Monitube 탐색">
        <div className="brand-lockup">
          <span className="brand-mark" aria-hidden="true"><PlayIcon /></span>
          <span>monitube</span>
        </div>

        <nav className="sidebar-nav" aria-label="주요 메뉴">
          {navigation.map(({ id, label, target, Icon }) => (
            <button
              key={id}
              className={activeNav === id ? "nav-item nav-item-active" : "nav-item"}
              type="button"
              aria-label={label}
              aria-current={activeNav === id ? "page" : undefined}
              onClick={() => navigateTo(id, target)}
            >
              <Icon aria-hidden="true" />
              <span>{label}</span>
            </button>
          ))}
        </nav>

        <div className="sidebar-status">
          <span className="sidebar-status-dot" aria-hidden="true" />
          <div>
            <strong>Server managed</strong>
            <small>수집 credential은 안전하게 관리됩니다.</small>
          </div>
        </div>
      </aside>

      <main className="dashboard-main">
        <header className="dashboard-topbar" id="source-selector" tabIndex={-1}>
          <label className="source-select">
            <span className="visually-hidden">수집 source 선택</span>
            <FolderIcon aria-hidden="true" />
            <select
              value={activeSourceId}
              disabled={isSourcesLoading || sources.length === 0}
              onChange={(event) => setActiveSourceId(event.target.value)}
            >
              {sources.length === 0 && <option value="">등록된 source 없음</option>}
              {sources.map((source) => <option key={source.id} value={source.id}>{sourceLabel(source)}</option>)}
            </select>
          </label>
          <div className="topbar-actions">
            <span className="refresh-copy">
              {sourceResults?.analysis?.generatedAt ? `분석 갱신 ${formatDate(sourceResults.analysis.generatedAt)}` : "수집 결과를 선택하세요"}
            </span>
            <button
              className="icon-button"
              type="button"
              onClick={() => {
                void refreshSources();
                if (activeSourceId) void refreshResults(activeSourceId);
              }}
              disabled={isSourcesLoading || isResultsLoading}
              aria-label="source와 결과 새로고침"
            >
              <ArrowPathIcon aria-hidden="true" />
            </button>
            <button className="primary-action" type="button" onClick={openCollectionDrawer}>
              <PlusIcon aria-hidden="true" />
              새 수집
            </button>
          </div>
        </header>

        <section className="overview-intro" id="overview" aria-labelledby="overview-title" tabIndex={-1}>
          <div>
            <p className="section-kicker">MONITUBE / ANALYSIS WORKSPACE</p>
            <h1 id="overview-title">Overview</h1>
            <p>{sourceScope(activeSource)}</p>
          </div>
          {activeSource && (
            <div className="source-meta">
              <span className="source-type-chip">{sourceTypeCopy(activeSource.type)}</span>
              <span>{activeSource.enabled ? "수집 활성" : "수집 일시 중지"}</span>
            </div>
          )}
        </section>

        {resultsError && <p className="inline-error" role="status">{resultsError}</p>}

        {!activeSourceId && !isSourcesLoading && (
          <section className="empty-overview" aria-labelledby="empty-overview-title">
            <DocumentChartBarIcon aria-hidden="true" />
            <div>
              <p className="section-kicker">READY WHEN YOU ARE</p>
              <h2 id="empty-overview-title">첫 source를 수집해 보세요.</h2>
              <p>채널, 키워드 또는 단일 동영상을 선택하면 이곳에 동영상·공개 댓글·수집 상태를 정리해 드립니다.</p>
            </div>
            <button className="primary-action" type="button" onClick={openCollectionDrawer}>
              <PlusIcon aria-hidden="true" />
              첫 수집 시작
            </button>
          </section>
        )}

        {activeSourceId && isResultsLoading && !sourceResults && (
          <section className="empty-overview loading-overview" aria-live="polite">
            <ArrowPathIcon aria-hidden="true" />
            <div><h2>저장된 분석을 불러오는 중입니다.</h2><p>선택한 source의 최신 수집 결과를 준비하고 있습니다.</p></div>
          </section>
        )}

        {sourceResults && sourceResults.source.id === activeSourceId && (
          <div className="dashboard-content">
            <section className="kpi-grid" aria-label="선택 source의 핵심 지표">
              <MetricCard
                label="수집 동영상"
                value={formatCount(analysisVideoCount)}
                detail="현재 source에 저장된 영상"
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
                value={activeJob?.progress.total ? `${progressPercent}%` : activeJob ? statusCopy(activeJob) : "대기"}
                detail={activeJob ? stageLabels[activeJob.currentStage] ?? activeJob.currentStage : "아직 실행 기록 없음"}
                icon={<QueueListIcon />}
                accent={Boolean(activeJob && !isTerminalJob(activeJob))}
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
                    <p>아직 실행 기록이 없습니다. 새 수집을 시작하면 진행률과 재개 계획이 이곳에 표시됩니다.</p>
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
                          <small>{activeJob.resumeIsAutomatic ? "자동 재개" : "수동 확인 필요"} · {formatReset(activeJob.resumeAt)}</small>
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
                  <span className="panel-count">{formatCount(videos.length)}개</span>
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
              </section>
            </div>
          </div>
        )}

        <footer className="dashboard-footer">
          <p>공개 YouTube 메타데이터와 공개 댓글을 source별로 수집·분석합니다.</p>
          <p>운영 credential은 브라우저에 노출되지 않습니다.</p>
        </footer>
      </main>

      {isCollectionOpen && (
        <div className="drawer-layer">
          <div className="drawer-backdrop" aria-hidden="true" onClick={closeCollectionDrawer} />
          <aside ref={collectionDrawerRef} className="collection-drawer" role="dialog" aria-modal="true" aria-labelledby="collection-drawer-title" tabIndex={-1}>
            <div className="drawer-heading">
              <div><p className="section-kicker">NEW COLLECTION</p><h2 id="collection-drawer-title">새 수집</h2><p>수집할 대상을 고르고 필요한 범위를 정하세요.</p></div>
              <button className="icon-button" type="button" aria-label="새 수집 창 닫기" data-drawer-initial-focus onClick={closeCollectionDrawer}><XMarkIcon aria-hidden="true" /></button>
            </div>

            <form className="collection-form" onSubmit={(event) => { event.preventDefault(); void loadEstimate(); }}>
              <fieldset className="source-type-group">
                <legend>수집 대상</legend>
                <div>
                  {sourceTypeChoices.map(({ type, label, detail, Icon }) => (
                    <button
                      key={type}
                      type="button"
                      className={form.sourceType === type ? "source-type-choice source-type-choice-active" : "source-type-choice"}
                      onClick={() => update("sourceType", type)}
                      aria-pressed={form.sourceType === type}
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
                  <input value={form.channelInput} onChange={(event) => update("channelInput", event.target.value)} placeholder="예: @GoogleDevelopers" autoComplete="off" />
                  <small>채널 전체 업로드는 업로드 재생목록을 기준으로 수집합니다.</small>
                </label>
              ) : form.sourceType === "video" ? (
                <label className="drawer-field drawer-field-wide">
                  <span>YouTube 동영상 URL 또는 ID</span>
                  <input value={form.videoInput} onChange={(event) => update("videoInput", event.target.value)} placeholder="예: https://www.youtube.com/watch?v=..." autoComplete="off" />
                  <small>youtube.com/watch, youtu.be URL 또는 11자리 동영상 ID를 사용할 수 있습니다.</small>
                </label>
              ) : (
                <div className="drawer-field-grid">
                  <label className="drawer-field drawer-field-wide"><span>검색 키워드</span><input value={form.keyword} onChange={(event) => update("keyword", event.target.value)} placeholder="예: 생성형 AI 교육" autoComplete="off" /></label>
                  <label className="drawer-field"><span>시작일</span><input type="date" value={form.publishedAfter} onChange={(event) => update("publishedAfter", event.target.value)} /></label>
                  <label className="drawer-field"><span>종료일</span><input type="date" value={form.publishedBefore} onChange={(event) => update("publishedBefore", event.target.value)} /></label>
                  <label className="drawer-field"><span>정렬</span><select value={form.order} onChange={(event) => update("order", event.target.value as KeywordSourceConfig["order"])}><option value="date">최신순</option><option value="relevance">관련성순</option><option value="viewCount">조회수순</option></select></label>
                  <label className="drawer-field"><span>최대 검색 페이지</span><input type="number" min="1" max="100" value={form.maxPagesPerRun} onChange={(event) => update("maxPagesPerRun", Number(event.target.value))} /></label>
                  <label className="drawer-field"><span>관련 언어</span><input value={form.relevanceLanguage} maxLength={8} onChange={(event) => update("relevanceLanguage", event.target.value)} placeholder="ko" /></label>
                  <label className="drawer-field"><span>지역</span><input value={form.regionCode} maxLength={2} onChange={(event) => update("regionCode", event.target.value)} placeholder="KR" /></label>
                </div>
              )}

              <section className="drawer-scope" aria-labelledby="scope-title">
                <div><p className="section-kicker">SCOPE</p><h3 id="scope-title">수집 범위</h3><p>실행 전 예상치를 확인하고, 서버가 실제 한도를 다시 검증합니다.</p></div>
                <div className="scope-controls">
                  {form.sourceType === "channel" && <label className="drawer-field compact-drawer-field"><span>최대 동영상 수</span><input type="number" min="1" max="5000" value={form.maxVideos} onChange={(event) => update("maxVideos", Number(event.target.value))} /></label>}
                  <label className="toggle-field"><input type="checkbox" checked={form.includeComments} onChange={(event) => update("includeComments", event.target.checked)} /><span className="toggle-visual" aria-hidden="true" /><span><strong>공개 댓글 수집</strong><small>댓글이 비활성화된 영상은 부분 경고로 남습니다.</small></span></label>
                  {form.includeComments && <label className="drawer-field compact-drawer-field"><span>동영상당 댓글 페이지</span><input type="number" min="1" max="100" value={form.maxCommentPagesPerVideo} onChange={(event) => update("maxCommentPagesPerVideo", Number(event.target.value))} /></label>}
                </div>
              </section>

              {estimates.length > 0 && (
                <section className="estimate-panel" aria-labelledby="estimate-title">
                  <div className="estimate-heading"><div><p className="section-kicker">PREFLIGHT</p><h3 id="estimate-title">보수적 예상치</h3></div><InformationCircleIcon aria-hidden="true" /></div>
                  <div className="estimate-list">
                    {estimates.map((estimate) => <div key={estimate.bucket}><div><strong>{bucketLabels[estimate.bucket]}</strong><span>{estimate.bucket === "search_queries" ? `${estimate.estimatedCalls} calls 예상` : `${estimate.estimatedUnits} units 예상`}</span></div><strong>{formatCount(estimate.limit)}<small>일일 한도</small></strong></div>)}
                  </div>
                  {estimateWarnings.map((warning) => <p className="estimate-note" key={warning}>{warning}</p>)}
                </section>
              )}

              <div className="drawer-footer-action">
                <button className="secondary-action" type="button" onClick={closeCollectionDrawer}>취소</button>
                {estimateMode ? (
                  <button className="primary-action drawer-start-action" type="button" disabled={isStarting} onClick={() => void launchJob()}>{isStarting ? "작업을 등록하는 중…" : "수집 작업 시작"}<ChevronRightIcon aria-hidden="true" /></button>
                ) : (
                  <button className="primary-action" type="submit" disabled={isEstimating}>{isEstimating ? "예상치 계산 중…" : `${sourceName} 예상치 확인`}<ChevronRightIcon aria-hidden="true" /></button>
                )}
              </div>
            </form>
          </aside>
        </div>
      )}

      {selectedVideo && (
        <div className="drawer-layer video-drawer-layer">
          <div className="drawer-backdrop" aria-hidden="true" onClick={closeVideoDrawer} />
          <aside ref={videoDrawerRef} className="video-drawer" role="dialog" aria-modal="true" aria-labelledby="video-drawer-title" tabIndex={-1}>
            <div className="drawer-heading"><div><p className="section-kicker">VIDEO DETAIL</p><h2 id="video-drawer-title">{selectedVideo.title}</h2><p>{selectedVideo.youtubeVideoId}</p></div><button className="icon-button" type="button" aria-label="동영상 상세 창 닫기" data-drawer-initial-focus onClick={closeVideoDrawer}><XMarkIcon aria-hidden="true" /></button></div>
            <dl className="video-meta-grid"><div><dt>게시일</dt><dd>{formatDate(selectedVideo.publishedAt)}</dd></div><div><dt>길이</dt><dd>{formatDuration(selectedVideo.durationSeconds)}</dd></div><div><dt>조회</dt><dd>{formatCount(selectedVideo.viewCount)}</dd></div><div><dt>좋아요</dt><dd>{formatCount(selectedVideo.likeCount)}</dd></div><div><dt>YouTube 댓글</dt><dd>{formatCount(selectedVideo.commentCount)}</dd></div><div><dt>공개 상태</dt><dd>{selectedVideo.privacyStatus ?? "—"}</dd></div></dl>
            <section className="drawer-comments" aria-labelledby="comments-title"><div className="drawer-comments-heading"><div><p className="section-kicker">PUBLIC COMMENTS</p><h3 id="comments-title">수집된 공개 댓글</h3></div><button className="icon-button" type="button" aria-label="댓글 새로고침" disabled={isCommentsLoading} onClick={() => void loadComments(selectedVideo)}><ArrowPathIcon aria-hidden="true" /></button></div>
              {commentsError && <p className="inline-error" role="status">{commentsError}</p>}
              {isCommentsLoading && comments.length === 0 ? <p className="comments-loading">공개 댓글을 불러오는 중입니다…</p> : null}
              {!isCommentsLoading && !commentsError && comments.length === 0 ? <p className="comments-loading">저장된 공개 댓글이 없거나 댓글 수집이 선택되지 않았습니다.</p> : null}
              {comments.length > 0 && <div className="comment-list">{comments.map((comment) => <article className="comment-item" key={comment.id}><p>{comment.text}</p><footer><span>{formatDate(comment.publishedAt)}</span>{comment.likeCount !== undefined && <span>좋아요 {formatCount(comment.likeCount)}</span>}</footer></article>)}</div>}
              {nextCommentsCursor && <button className="secondary-action comments-load-more" type="button" disabled={isCommentsLoading} onClick={() => void loadComments(selectedVideo, nextCommentsCursor)}>{isCommentsLoading ? "댓글을 불러오는 중…" : "댓글 더 보기"}</button>}
            </section>
          </aside>
        </div>
      )}

      {(notice || error) && <div className={error ? "toast toast-error" : "toast"} role="status"><span aria-hidden="true">{error ? <ExclamationTriangleIcon /> : <CheckCircleIcon />}</span><p>{error ?? notice}</p></div>}
    </div>
  );
}
