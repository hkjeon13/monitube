"use client";

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
import { useCallback, useEffect, useMemo, useState } from "react";

import {
  apiBaseUrl,
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
  search_queries: "Search Queries",
  core: "공용 YouTube API",
};

const stageLabels: Record<string, string> = {
  resolving_source: "수집 대상을 확인하는 중",
  listing_channel_videos: "채널 업로드 목록을 불러오는 중",
  searching_keywords: "키워드 검색 결과를 불러오는 중",
  fetching_video_details: "동영상 상세 정보를 불러오는 중",
  fetching_comments: "공개 댓글을 불러오는 중",
  persisting: "결과를 저장하는 중",
  analyzing: "분석을 준비하는 중",
};

function clampPositive(value: number, fallback: number) {
  return Number.isFinite(value) && value > 0 ? Math.floor(value) : fallback;
}

function formatReset(value?: string) {
  if (!value) return "다음 quota window 확인 후";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  return new Intl.DateTimeFormat("ko-KR", {
    dateStyle: "medium",
    timeStyle: "short",
    timeZone: "Asia/Seoul",
  }).format(date);
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
      ? [
          {
            bucket: "search_queries" as const,
            estimatedCalls: (requestBody.config as KeywordSourceConfig).maxPagesPerRun,
            estimatedUnits: (requestBody.config as KeywordSourceConfig).maxPagesPerRun,
            limit: 100,
            resetAt,
          },
        ]
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

function demoWaitingJob(sourceType: CollectionSourceType): JobStatus {
  const resumeAt = new Date(Date.now() + 60 * 60 * 1000).toISOString();
  return {
    id: "demo-waiting-quota",
    state: "waiting_quota",
    currentStage: sourceType === "keyword" ? "searching_keywords" : sourceType === "video" ? "fetching_video_details" : "fetching_comments",
    progress: { completed: sourceType === "keyword" ? 4 : 1, total: sourceType === "keyword" ? 10 : sourceType === "video" ? 1 : 50, unit: sourceType === "keyword" ? "pages" : "videos" },
    pauseReason: "백엔드가 관리하는 운영 API quota가 소진되었습니다.",
    quotaBucket: sourceType === "keyword" ? "search_queries" : "core",
    resumeAt,
    resumeIsAutomatic: true,
    partialErrors: [],
  };
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

function formatCount(value?: number) {
  return value === undefined ? "—" : new Intl.NumberFormat("ko-KR").format(value);
}

function isTerminalJob(job: JobStatus | null) {
  return job ? ["completed", "completed_with_warnings", "failed", "cancelled"].includes(job.state) : false;
}

function mergeComments(current: CollectedComment[], incoming: CollectedComment[]) {
  const seen = new Set(current.map((comment) => comment.id));
  return [...current, ...incoming.filter((comment) => !seen.has(comment.id))];
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

  const requestBody = useMemo(() => sourceRequest(form), [form]);
  const validationError = useMemo(() => validate(requestBody), [requestBody]);
  const sourceName = form.sourceType === "channel" ? "채널" : form.sourceType === "keyword" ? "키워드 검색" : "동영상";

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
    setIsResultsLoading(true);
    setResultsError(null);
    try {
      setSourceResults(await getSourceResults(sourceId));
    } catch (caught) {
      setResultsError(caught instanceof Error ? caught.message : "수집 결과를 불러오지 못했습니다.");
    } finally {
      setIsResultsLoading(false);
    }
  }, []);

  const loadComments = useCallback(async (video: CollectedVideo, cursor?: string) => {
    setSelectedVideo(video);
    setIsCommentsLoading(true);
    setCommentsError(null);
    try {
      const response = await getVideoComments(video.id, cursor);
      setComments((current) => cursor ? mergeComments(current, response.comments) : response.comments);
      setNextCommentsCursor(response.nextCursor);
    } catch (caught) {
      setCommentsError(caught instanceof Error ? caught.message : "댓글을 불러오지 못했습니다.");
      if (!cursor) {
        setComments([]);
        setNextCommentsCursor(undefined);
      }
    } finally {
      setIsCommentsLoading(false);
    }
  }, []);

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
      "운영 credential과 quota ledger는 백엔드가 관리합니다. 작업 등록 시 서버가 실제 quota를 예약·검증합니다.",
    ]);
    setEstimateMode("local");
    setNotice("브라우저의 보수적 예상치를 표시합니다. 실제 quota와 reset 시각은 서버 작업 상태에서 확인됩니다.");
    setIsEstimating(false);
  }, [requestBody, validationError]);

  const launchJob = useCallback(async () => {
    if (validationError) {
      setError(validationError);
      return;
    }
    if (!estimateMode) {
      setError("먼저 수집 범위의 quota 예상치를 계산하세요.");
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
      setNotice("수집 작업을 시작했습니다. 상태를 자동으로 갱신합니다.");
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "수집 작업을 시작하지 못했습니다.");
    } finally {
      setIsStarting(false);
    }
  }, [estimateMode, requestBody, validationError]);

  useEffect(() => {
    if (!job || job.id === "demo-waiting-quota" || ["completed", "completed_with_warnings", "failed", "cancelled"].includes(job.state)) {
      return;
    }

    const timer = window.setInterval(async () => {
      try {
        setJob(await getJob(job.id));
      } catch {
        // The existing status remains visible; a transient poll failure should not erase it.
      }
    }, 5_000);

    return () => window.clearInterval(timer);
  }, [job]);

  useEffect(() => {
    if (jobSourceId && isTerminalJob(job)) {
      void refreshResults(jobSourceId);
    }
  }, [job, jobSourceId, refreshResults]);

  const progressPercent = job?.progress.total
    ? Math.min(100, Math.round((job.progress.completed / job.progress.total) * 100))
    : 0;

  return (
    <main className="shell">
      <section className="hero" aria-labelledby="page-title">
        <div>
          <p className="eyebrow">MONITUBE / COLLECTION CONSOLE</p>
          <h1 id="page-title">YouTube 수집을<br />예측 가능하게.</h1>
          <p className="lede">
            채널, 키워드 검색 또는 동영상 URL/ID를 선택하고, 실제 실행 전 API quota 예상치와 수집 범위를 확인하세요.
          </p>
        </div>
        <div className="policy-card">
          <span className="policy-dot" aria-hidden="true" />
          <div>
            <strong>Quota-safe routing</strong>
            <p>운영 credential은 백엔드에서만 관리하며, quota 대기 뒤 저장된 checkpoint에서 자동 재개합니다.</p>
          </div>
        </div>
      </section>

      <section className="workspace" aria-label="수집 작업 설정">
        <form className="source-card" onSubmit={(event) => { event.preventDefault(); void loadEstimate(); }}>
          <div className="section-heading">
            <div>
              <p className="eyebrow">01 / SOURCE</p>
              <h2>무엇을 수집할까요?</h2>
            </div>
            <span className="environment-chip">API: {apiBaseUrl()}</span>
          </div>

          <div className="source-switch" role="radiogroup" aria-label="수집 source 선택">
            <button
              type="button"
              role="radio"
              aria-checked={form.sourceType === "channel"}
              className={form.sourceType === "channel" ? "source-choice selected" : "source-choice"}
              onClick={() => update("sourceType", "channel")}
            >
              <span className="choice-symbol">◉</span>
              <span><strong>채널</strong><small>업로드 동영상 기준</small></span>
            </button>
            <button
              type="button"
              role="radio"
              aria-checked={form.sourceType === "keyword"}
              className={form.sourceType === "keyword" ? "source-choice selected" : "source-choice"}
              onClick={() => update("sourceType", "keyword")}
            >
              <span className="choice-symbol">⌕</span>
              <span><strong>키워드 검색</strong><small>검색 run별 발견 결과</small></span>
            </button>
            <button
              type="button"
              role="radio"
              aria-checked={form.sourceType === "video"}
              className={form.sourceType === "video" ? "source-choice selected" : "source-choice"}
              onClick={() => update("sourceType", "video")}
            >
              <span className="choice-symbol">▶</span>
              <span><strong>동영상</strong><small>URL 또는 ID 직접 선택</small></span>
            </button>
          </div>

          {form.sourceType === "channel" ? (
            <label className="field field-wide">
              <span>채널 URL, @handle 또는 채널 ID</span>
              <input
                value={form.channelInput}
                onChange={(event) => update("channelInput", event.target.value)}
                placeholder="예: @GoogleDevelopers 또는 UC_x5XG1OV2P6uZZ5FSM9Ttw"
                autoComplete="off"
              />
              <small>채널 전체 업로드는 검색 API 대신 업로드 재생목록을 기준으로 수집합니다.</small>
            </label>
          ) : form.sourceType === "video" ? (
            <label className="field field-wide">
              <span>YouTube 동영상 URL 또는 동영상 ID</span>
              <input
                value={form.videoInput}
                onChange={(event) => update("videoInput", event.target.value)}
                placeholder="예: https://www.youtube.com/watch?v=dQw4w9WgXcQ 또는 dQw4w9WgXcQ"
                autoComplete="off"
              />
              <small>youtube.com/watch URL, youtu.be URL 또는 11자리 동영상 ID를 사용할 수 있습니다.</small>
            </label>
          ) : (
            <div className="field-grid keyword-fields">
              <label className="field field-wide">
                <span>검색 키워드</span>
                <input
                  value={form.keyword}
                  onChange={(event) => update("keyword", event.target.value)}
                  placeholder="예: 생성형 AI 교육"
                  autoComplete="off"
                />
              </label>
              <label className="field">
                <span>시작일</span>
                <input type="date" value={form.publishedAfter} onChange={(event) => update("publishedAfter", event.target.value)} />
              </label>
              <label className="field">
                <span>종료일</span>
                <input type="date" value={form.publishedBefore} onChange={(event) => update("publishedBefore", event.target.value)} />
              </label>
              <label className="field">
                <span>정렬</span>
                <select value={form.order} onChange={(event) => update("order", event.target.value as KeywordSourceConfig["order"])}>
                  <option value="date">최신순</option>
                  <option value="relevance">관련성순</option>
                  <option value="viewCount">조회수순</option>
                </select>
              </label>
              <label className="field">
                <span>최대 검색 페이지</span>
                <input type="number" min="1" max="100" value={form.maxPagesPerRun} onChange={(event) => update("maxPagesPerRun", Number(event.target.value))} />
              </label>
              <label className="field">
                <span>관련 언어</span>
                <input value={form.relevanceLanguage} maxLength={8} onChange={(event) => update("relevanceLanguage", event.target.value)} placeholder="ko" />
              </label>
              <label className="field">
                <span>지역</span>
                <input value={form.regionCode} maxLength={2} onChange={(event) => update("regionCode", event.target.value)} placeholder="KR" />
              </label>
            </div>
          )}

          <div className="scope-panel">
            <div className="scope-copy">
              <p className="eyebrow">02 / SCOPE</p>
              <h3>수집 범위를 정하세요</h3>
              <p>모든 수치는 서버에서 quota 예약 전에 다시 검증됩니다.</p>
            </div>
            <div className="scope-controls">
              {form.sourceType === "channel" && (
                <label className="field compact-field">
                  <span>최대 동영상 수</span>
                  <input type="number" min="1" max="5000" value={form.maxVideos} onChange={(event) => update("maxVideos", Number(event.target.value))} />
                </label>
              )}
              <label className="toggle-field">
                <input type="checkbox" checked={form.includeComments} onChange={(event) => update("includeComments", event.target.checked)} />
                <span className="toggle" aria-hidden="true" />
                <span><strong>공개 댓글 수집</strong><small>댓글 비활성화 영상은 부분 경고로 남습니다.</small></span>
              </label>
              {form.includeComments && (
                <label className="field compact-field">
                  <span>동영상당 댓글 페이지</span>
                  <input type="number" min="1" max="100" value={form.maxCommentPagesPerVideo} onChange={(event) => update("maxCommentPagesPerVideo", Number(event.target.value))} />
                </label>
              )}
            </div>
          </div>

          <div className="form-actions">
            <button className="button button-primary" type="submit" disabled={isEstimating}>
              {isEstimating ? "예상치 계산 중…" : `${sourceName} quota 예상 보기`}
            </button>
            <button className="button button-quiet" type="button" onClick={() => setJob(demoWaitingJob(form.sourceType))}>
              quota 대기 UI 미리보기
            </button>
          </div>
        </form>

        <aside className="status-column" aria-label="quota 예상 및 작업 상태">
          <section className="quota-card">
            <div className="section-heading">
              <div>
                <p className="eyebrow">03 / PREFLIGHT</p>
                <h2>예상 quota</h2>
              </div>
              {estimateMode && <span className="fallback-badge">보수적 추정</span>}
            </div>

            {estimates.length === 0 ? (
              <div className="empty-state">
                <span className="empty-orbit" aria-hidden="true" />
                <p>수집 범위를 입력하면<br />bucket별 비용을 계산합니다.</p>
              </div>
            ) : (
              <>
                <div className="quota-list">
                  {estimates.map((estimate) => (
                    <div className="quota-row" key={estimate.bucket}>
                      <div>
                        <strong>{bucketLabels[estimate.bucket]}</strong>
                        <span>{estimate.bucket === "search_queries" ? `${estimate.estimatedCalls} calls 예상` : `${estimate.estimatedUnits} units 예상`}</span>
                      </div>
                      <div className="quota-metric">
                        <strong>{estimate.limit.toLocaleString()}</strong>
                        <span>일일 한도</span>
                      </div>
                    </div>
                  ))}
                </div>
                <p className="quota-note">예상치는 예약 전 추정값입니다. 실제 한도와 reset 시각은 백엔드가 관리하는 credential과 quota ledger를 기준으로 합니다.</p>
                <button className="button button-dark" type="button" disabled={isStarting || !estimateMode} onClick={() => void launchJob()}>
                  {isStarting ? "작업을 등록하는 중…" : "수집 작업 시작"}
                </button>
              </>
            )}

            {estimateWarnings.length > 0 && (
              <ul className="message-list warning-list">
                {estimateWarnings.map((warning) => <li key={warning}>{warning}</li>)}
              </ul>
            )}
          </section>

          <section className="job-card" aria-live="polite">
            <div className="section-heading">
              <div>
                <p className="eyebrow">04 / JOB STATUS</p>
                <h2>수집 상태</h2>
              </div>
              {job && <span className={`state-pill state-${job.state}`}>{statusCopy(job)}</span>}
            </div>

            {!job ? (
              <div className="job-empty">
                <p>작업을 시작하면 진행률, 중단 지점, quota bucket과 재개 계획이 여기에 표시됩니다.</p>
                <small>대기 중인 작업도 DB checkpoint를 유지하며, 다음 가능한 quota window에 백엔드가 동일한 운영 credential으로 재개합니다.</small>
              </div>
            ) : (
              <div className="job-details">
                <div className="job-progress-copy">
                  <div>
                    <strong>{stageLabels[job.currentStage] ?? job.currentStage}</strong>
                    <span>{job.progress.completed}{job.progress.total ? ` / ${job.progress.total}` : ""} {job.progress.unit} 처리</span>
                  </div>
                  {job.progress.total && <strong>{progressPercent}%</strong>}
                </div>
                {job.progress.total && <div className="progress-track"><span style={{ width: `${progressPercent}%` }} /></div>}

                {job.state === "waiting_quota" && (
                  <div className="waiting-panel">
                    <span className="waiting-icon" aria-hidden="true">↻</span>
                    <div>
                      <strong>{job.quotaBucket ? bucketLabels[job.quotaBucket] : "Quota"} bucket 대기</strong>
                      <p>{job.pauseReason ?? "일일 quota가 소진되었습니다."}</p>
                      <p className="resume-line">{job.resumeIsAutomatic ? "자동 재개" : "수동 확인 필요"} · {formatReset(job.resumeAt)}{job.resumeIsAutomatic ? " 이후 재시도" : ""}</p>
                    </div>
                  </div>
                )}

                {job.state !== "waiting_quota" && job.pauseReason && <p className="job-reason">{job.pauseReason}</p>}
                {job.partialErrors.length > 0 && <p className="job-warning">부분 경고 {job.partialErrors.length}건이 기록되었습니다.</p>}
                <p className="job-id">Job {job.id}</p>
              </div>
            )}
          </section>
        </aside>
      </section>

      <section className="results-card" aria-labelledby="results-title">
        <div className="section-heading results-heading">
          <div>
            <p className="eyebrow">05 / RESULTS</p>
            <h2 id="results-title">수집 결과</h2>
          </div>
          {sourceResults?.latestJob && (
            <span className={`state-pill state-${sourceResults.latestJob.state}`}>
              {statusCopy(sourceResults.latestJob)}
            </span>
          )}
        </div>

        <div className="results-toolbar">
          <label className="source-picker">
            <span>수집 source</span>
            <select
              value={activeSourceId}
              disabled={isSourcesLoading || sources.length === 0}
              onChange={(event) => setActiveSourceId(event.target.value)}
            >
              {sources.length === 0 && <option value="">등록된 source 없음</option>}
              {sources.map((source) => (
                <option key={source.id} value={source.id}>{sourceLabel(source)}</option>
              ))}
            </select>
          </label>
          <div className="results-actions">
            <button className="button button-quiet button-small" type="button" disabled={isSourcesLoading} onClick={() => void refreshSources()}>
              {isSourcesLoading ? "목록 갱신 중…" : "source 목록 갱신"}
            </button>
            <button className="button button-secondary button-small" type="button" disabled={!activeSourceId || isResultsLoading} onClick={() => void refreshResults(activeSourceId)}>
              {isResultsLoading ? "결과 갱신 중…" : "결과 새로고침"}
            </button>
          </div>
        </div>

        {resultsError && <p className="inline-error" role="status">{resultsError}</p>}

        {!activeSourceId && !isSourcesLoading && (
          <div className="results-empty">
            <span aria-hidden="true">◎</span>
            <p>수집 작업을 시작하면 이곳에서 동영상 메타데이터와 공개 댓글 요약을 확인할 수 있습니다.</p>
          </div>
        )}

        {activeSourceId && isResultsLoading && !sourceResults && (
          <div className="results-empty"><p>저장된 수집 결과를 불러오는 중입니다…</p></div>
        )}

        {sourceResults && sourceResults.source.id === activeSourceId && (
          <div className="results-body">
            <div className="result-overview">
              <div className="source-result-title">
                <p className="eyebrow">SELECTED SOURCE</p>
                <h3>{sourceLabel(sourceResults.source)}</h3>
                <span>{sourceResults.source.enabled ? "수집 활성" : "수집 일시 중지"} · {sourceResults.source.id}</span>
              </div>
              <div className="result-stat">
                <span>동영상</span>
                <strong>{formatCount(sourceResults.videos.length)}</strong>
              </div>
              <div className="result-stat">
                <span>공개 댓글</span>
                <strong>{formatCount(sourceResults.commentSummary?.total)}</strong>
              </div>
              <div className="result-stat">
                <span>최근 댓글</span>
                <strong className="stat-date">{formatDate(sourceResults.commentSummary?.latestPublishedAt)}</strong>
              </div>
            </div>

            {sourceResults.commentSummary?.topWords.length ? (
              <div className="word-summary">
                <span>댓글 주요 단어</span>
                <div>
                  {sourceResults.commentSummary.topWords.map((word) => (
                    <span className="word-chip" key={`${word.label}-${word.count ?? ""}`}>
                      {word.label}{word.count === undefined ? "" : ` ${formatCount(word.count)}`}
                    </span>
                  ))}
                </div>
              </div>
            ) : null}

            <section className="videos-section" aria-labelledby="videos-title">
              <div className="results-section-heading">
                <div>
                  <p className="eyebrow">VIDEO METADATA</p>
                  <h3 id="videos-title">동영상 목록</h3>
                </div>
                <span>{sourceResults.videos.length}개</span>
              </div>

              {sourceResults.videos.length === 0 ? (
                <div className="results-empty compact-empty">
                  <p>아직 표시할 저장 동영상이 없습니다. 작업이 완료된 뒤 결과를 새로고침하세요.</p>
                </div>
              ) : (
                <div className="video-table-wrap">
                  <table className="video-table">
                    <thead>
                      <tr>
                        <th scope="col">동영상</th>
                        <th scope="col">게시 시각</th>
                        <th scope="col">조회</th>
                        <th scope="col">좋아요</th>
                        <th scope="col">댓글</th>
                        <th scope="col"><span className="visually-hidden">작업</span></th>
                      </tr>
                    </thead>
                    <tbody>
                      {sourceResults.videos.map((video) => (
                        <tr key={video.id}>
                          <td className="video-title-cell">
                            <strong>{video.title}</strong>
                            <span>{video.youtubeVideoId}</span>
                          </td>
                          <td>{formatDate(video.publishedAt)}</td>
                          <td>{formatCount(video.viewCount)}</td>
                          <td>{formatCount(video.likeCount)}</td>
                          <td>{formatCount(video.commentCount)}</td>
                          <td>
                            <button className="button button-secondary button-small" type="button" onClick={() => void loadComments(video)}>
                              댓글 보기
                            </button>
                          </td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              )}
            </section>

            {selectedVideo && (
              <section className="comments-panel" aria-labelledby="comments-title">
                <div className="results-section-heading">
                  <div>
                    <p className="eyebrow">PUBLIC COMMENTS</p>
                    <h3 id="comments-title">{selectedVideo.title}</h3>
                  </div>
                  <button className="button button-quiet button-small" type="button" disabled={isCommentsLoading} onClick={() => void loadComments(selectedVideo)}>
                    댓글 새로고침
                  </button>
                </div>

                {commentsError && <p className="inline-error" role="status">{commentsError}</p>}
                {isCommentsLoading && comments.length === 0 ? <p className="comments-loading">공개 댓글을 불러오는 중입니다…</p> : null}
                {!isCommentsLoading && !commentsError && comments.length === 0 ? <p className="comments-loading">저장된 공개 댓글이 없거나 댓글 수집이 선택되지 않았습니다.</p> : null}

                {comments.length > 0 && (
                  <div className="comment-list">
                    {comments.map((comment) => (
                      <article className="comment-item" key={comment.id}>
                        <p>{comment.text}</p>
                        <footer>
                          <span>{formatDate(comment.publishedAt)}</span>
                          {comment.likeCount !== undefined && <span>좋아요 {formatCount(comment.likeCount)}</span>}
                        </footer>
                      </article>
                    ))}
                  </div>
                )}

                {nextCommentsCursor && (
                  <button className="button button-secondary button-small load-more" type="button" disabled={isCommentsLoading} onClick={() => void loadComments(selectedVideo, nextCommentsCursor)}>
                    {isCommentsLoading ? "댓글을 불러오는 중…" : "댓글 더 보기"}
                  </button>
                )}
              </section>
            )}
          </div>
        )}
      </section>

      {(notice || error) && (
        <div className={error ? "toast toast-error" : "toast"} role="status">
          <span>{error ? "!" : "i"}</span>
          <p>{error ?? notice}</p>
        </div>
      )}

      <section className="footnote" aria-label="서비스 제약 안내">
        <span>01</span>
        <p>MVP는 채널·키워드·동영상 URL/ID를 기준으로 동영상 메타데이터와 공개 댓글을 수집·분석합니다.</p>
        <span>02</span>
        <p>운영 YouTube API credential은 백엔드 전용으로 관리되며 브라우저에 전달되지 않습니다.</p>
      </section>
    </main>
  );
}
