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
  PlayIcon,
  PlusIcon,
  QueueListIcon,
  Cog6ToothIcon,
  SparklesIcon,
  Squares2X2Icon,
} from "@heroicons/react/24/outline";
import Link from "next/link";
import { useRouter } from "next/navigation";
import type { CollectionSourceType, JobStatus } from "@monitube/contracts";
import type { FormEvent, MouseEvent } from "react";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";

import {
  createCollectionRequest,
  deleteSource,
  getChannelSubscriberHistory,
  getCurrentUser,
  listSourceJobs,
  getExplore,
  getCommentDetail,
  searchCollected,
  getSourceVideos,
  getSourceWorkspace,
  getVideoCommentThreads,
  listRecentJobFailures,
  listSources,
  updateSource,
  type CommentThreadItem,
  type CommentThreadSort,
  type CollectedVideo,
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
import {
  LoginScreen,
  MetricCard,
  StatusPill,
} from "../features/collection/workbench-components";
import { CollectionDrawer, SettingsDrawer } from "../features/collection/workbench-drawers";
import { ChannelDetails, ExploreSection } from "../features/collection/workbench-explore";
import { SourcesPage, StatusPage } from "../features/collection/workbench-pages";
import { useActiveJobPolling } from "../features/collection/use-active-job-polling";
import { useDialogFocusTrap } from "../features/collection/use-dialog-focus-trap";
import { VideoModal } from "../features/collection/workbench-video-modal";
import {
  activeJobKey,
  bucketLabels,
  collectionNotice,
  collectionStatusDetail,
  collectionStatusValue,
  dedupeSources,
  defaultCollectionPreferences,
  formatCount,
  formatDate,
  formatKpiDate,
  formatReset,
  formatShortDate,
  formFromPreferences,
  idempotencyKey,
  initialForm,
  isAbortError,
  isTerminalJob,
  jobFailureReason,
  mergeCommentThreads,
  metricLabel,
  normalizePreferences,
  preferencesStorageKey,
  sourceCoverage,
  sourceLabel,
  sourceRequest,
  sourceScope,
  sourceTypeCopy,
  stageLabels,
  statusCopy,
  validate,
  videoMetric,
  type CollectionPreferences,
  type FormState,
  type ViewMetric,
  type WorkspacePage,
} from "../features/collection/workbench-model";

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
  const handleUnauthorized = useCallback(() => changeAuthUser(null), [changeAuthUser]);

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

  const changeCollectedSearchScope = (nextScope: CollectedSearchScope) => {
    setSearchScope(nextScope);
    if (searchQuery.trim().length < 2) return;
    searchRequest.current += 1;
    setSubmittedSearchQuery(searchQuery.trim());
    setSubmittedSearchScope(nextScope);
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

  const openCollectionDrawer = (event?: MouseEvent<HTMLButtonElement>, sourceType?: CollectionSourceType) => {
    collectionTriggerRef.current = event?.currentTarget ?? null;
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

  useActiveJobPolling({
    authUser,
    page,
    sources,
    activeSourceId,
    job,
    jobSourceId,
    sourcesRef,
    activeSourceIdRef,
    localJobRef,
    activeJobsRef,
    endpointSupportedRef: activeJobsEndpointSupportedRef,
    handledTerminalJobsRef,
    wakePollRef: wakeActiveJobsPollRef,
    setSources,
    setJob,
    setJobSourceId,
    onUnauthorized: handleUnauthorized,
    refreshSources,
    refreshResults,
    refreshExplore,
    refreshRecentFailures,
  });

  const closeActiveVideoModal = useCallback(() => {
    if (selectedCommentId) closeCommentDetail();
    else closeVideoDrawer();
  }, [closeCommentDetail, closeVideoDrawer, selectedCommentId]);
  useDialogFocusTrap({ open: isSettingsOpen, dialogRef: settingsDrawerRef, onClose: closeSettingsDrawer });
  useDialogFocusTrap({ open: isCollectionOpen, dialogRef: collectionDrawerRef, onClose: closeCollectionDrawer });
  useDialogFocusTrap({ open: Boolean(selectedCommentId || selectedVideo), dialogRef: videoModalRef, onClose: closeActiveVideoModal });

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
          <StatusPage
            failures={recentFailures}
            error={recentFailuresError}
            loading={isRecentFailuresLoading}
            onRefresh={() => { void refreshRecentFailures(); }}
          />
        )}

        <SourcesPage
          page={page}
          sources={sources}
          explore={explore}
          activeSourceId={activeSourceId}
          openMenuId={openSourceMenuId}
          updatingSourceId={updatingSourceId}
          deletingSourceId={deletingSourceId}
          onAdd={(type) => openCollectionDrawer(undefined, type)}
          onOpen={openSourceWorkspace}
          onMenuChange={setOpenSourceMenuId}
          onToggleRefresh={(source) => { void toggleSubscriptionRefresh(source); }}
          onRemove={(source) => { void removeSource(source); }}
        />

        <ChannelDetails
          channels={sourceRegisteredChannels}
          videos={explore.videos}
          selectedChannel={selectedExploreChannel}
          subscriberHistory={subscriberHistory}
          onSelect={(channelId, targetId) => {
            setExploreChannelId(channelId);
            setExploreVisibleCount(12);
            if (channelId && targetId) {
              const source = sources.find((item) => item.targetId === targetId);
              if (source) setActiveSourceId(source.id);
            }
          }}
        />

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

        <ExploreSection
          data={explore}
          error={exploreError}
          loading={isExploreLoading}
          loadingMore={isExploreLoadingMore}
          visibleVideos={visibleExploreVideos}
          selectedChannel={selectedExploreChannel}
          subscriberHistory={subscriberHistory}
          query={searchQuery}
          scope={searchScope}
          submittedQuery={submittedSearchQuery}
          submittedScope={submittedSearchScope}
          searchResults={searchResults}
          searchError={searchError}
          searchLoading={isSearchLoading}
          loadMoreRef={exploreLoadMoreRef}
          onQueryChange={setSearchQuery}
          onScopeChange={changeCollectedSearchScope}
          onSubmit={submitCollectedSearch}
          onSelectChannel={(channelId) => {
            setExploreChannelId(channelId);
            setExploreVisibleCount(12);
            void refreshExplore(channelId);
          }}
          onOpenVideo={openVideoDrawer}
          onOpenComment={openCommentDetail}
        />

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
        <SettingsDrawer
          drawerRef={settingsDrawerRef}
          username={authUser}
          draft={settingsDraft}
          setDraft={setSettingsDraft}
          onClose={closeSettingsDrawer}
          onSave={saveSettings}
          onReset={resetSettings}
        />
      )}

      {isCollectionOpen && (
        <CollectionDrawer
          drawerRef={collectionDrawerRef}
          form={form}
          isStarting={isStarting}
          onClose={closeCollectionDrawer}
          onSubmit={() => { void launchJob(); }}
          onUpdate={update}
        />
      )}

      {(selectedVideo || selectedCommentId) && (
        <VideoModal
          modalRef={videoModalRef}
          selectedVideo={selectedVideo}
          selectedCommentId={selectedCommentId}
          commentDetail={selectedCommentDetail}
          isCommentDetailLoading={isCommentDetailLoading}
          commentDetailError={commentDetailError}
          commentThreads={commentThreads}
          commentSort={commentSort}
          nextCommentsCursor={nextCommentsCursor}
          isCommentsLoading={isCommentsLoading}
          commentsError={commentsError}
          loadMoreRef={commentThreadsLoadMoreRef}
          onCloseVideo={closeVideoDrawer}
          onCloseComment={closeCommentDetail}
          onOpenVideo={openVideoDrawer}
          onOpenComment={openCommentDetail}
          onSortChange={changeCommentSort}
          onLoadComments={(video, cursor) => { void loadComments(video, cursor); }}
        />
      )}

      {(notice || error) && <div className={error ? "toast toast-error" : "toast"} role="status"><span aria-hidden="true">{error ? <ExclamationTriangleIcon /> : <CheckCircleIcon />}</span><p>{error ?? notice}</p></div>}
    </div>
  );
}
