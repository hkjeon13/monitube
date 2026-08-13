"use client";

import {
  ArrowPathIcon,
  ArrowTrendingUpIcon,
  BoltIcon,
  ChatBubbleLeftRightIcon,
  ChevronRightIcon,
  ClockIcon,
  DocumentTextIcon,
  EyeIcon,
  FilmIcon,
  HandThumbUpIcon,
  InformationCircleIcon,
  UsersIcon,
} from "@heroicons/react/24/outline";
import {
  ChevronDownIcon,
  ChevronUpIcon,
} from "@heroicons/react/20/solid";
import type { MouseEvent } from "react";
import { useEffect, useMemo, useState } from "react";

import {
  getAnalysisInsights,
  getAnalysisOverview,
  type AnalysisBreakdownRow,
  type AnalysisCommentType,
  type AnalysisInsights,
  type AnalysisOverview,
  type AnalysisPerformanceVideo,
  type AnalysisScope,
  type AnalysisTrendPoint,
  type AnalysisVideo,
  type CollectedVideo,
  type ExploreChannel,
  type SourceSummary,
} from "../../lib/api";
import { formatCount, formatShortDate, sourceLabel } from "../collection/workbench-model";
import { KeywordFrequencyPanel } from "./keyword-frequency-panel";

type AnalysisView = "overview" | "videos" | "comments";
type Period = "7" | "30" | "90" | "all";
type BreakdownSortKey = "label" | "videoCount" | "viewCount" | "collectedCommentCount" | "latestPublishedAt";
type SortDirection = "ascending" | "descending";
type PerformanceMetric = "growth" | "velocity" | "engagement" | "conversation";

function periodRange(period: Period) {
  if (period === "all") return {};
  const today = new Date();
  const from = new Date(Date.UTC(today.getUTCFullYear(), today.getUTCMonth(), today.getUTCDate()));
  from.setUTCDate(from.getUTCDate() - Number(period) + 1);
  const to = new Date(Date.UTC(today.getUTCFullYear(), today.getUTCMonth(), today.getUTCDate() + 1));
  return {
    from: from.toISOString().slice(0, 10),
    to: to.toISOString().slice(0, 10),
  };
}

function StorageMetric({
  kind,
  documents,
  tokens,
  countedDocuments,
}: {
  kind: "transcript" | "comment";
  documents: number;
  tokens: number;
  countedDocuments: number;
}) {
  const complete = countedDocuments >= documents;
  return (
    <article className="analysis-storage-metric">
      <div className="analysis-storage-icon">
        {kind === "transcript" ? <DocumentTextIcon /> : <ChatBubbleLeftRightIcon />}
      </div>
      <div className="analysis-storage-documents">
        <span>{kind === "transcript" ? "영상 대본" : "댓글"}</span>
        <strong>{formatCount(documents)}개</strong>
      </div>
      <div className="analysis-storage-tokens">
        <span>공백 토큰</span>
        <strong>{formatCount(tokens)}</strong>
      </div>
      <small className={complete ? "analysis-storage-complete" : "analysis-storage-progress"}>
        {complete ? "집계 완료" : `집계 중 · ${formatCount(countedDocuments)} / ${formatCount(documents)}개 반영`}
      </small>
    </article>
  );
}

function completeDailyTrend(points: AnalysisTrendPoint[], period: Period) {
  if (period !== "7" && period !== "30") return points;
  const existing = new Map(points.map((point) => [point.period.slice(0, 10), point]));
  const today = new Date();
  const start = new Date(Date.UTC(today.getUTCFullYear(), today.getUTCMonth(), today.getUTCDate()));
  start.setUTCDate(start.getUTCDate() - Number(period) + 1);
  return Array.from({ length: Number(period) }, (_, index) => {
    const date = new Date(start);
    date.setUTCDate(start.getUTCDate() + index);
    const key = date.toISOString().slice(0, 10);
    return existing.get(key) ?? { period: key, count: 0, topLevelCount: 0, replyCount: 0 };
  });
}

function chartDateLabel(value: string) {
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  return new Intl.DateTimeFormat("ko-KR", {
    month: "numeric",
    day: "numeric",
    timeZone: "UTC",
  }).format(date).replace(/\s/g, "");
}

function TrendChart({ points: rawPoints, label, period }: { points: AnalysisTrendPoint[]; label: string; period: Period }) {
  const [activeIndex, setActiveIndex] = useState<number | null>(null);
  const points = completeDailyTrend(rawPoints, period);
  if (!points.length) return <p className="analysis-empty">선택한 조건에 해당하는 추이가 없습니다.</p>;

  const chartWidth = Math.max(720, points.length * 28 + 72);
  const chartHeight = 252;
  const plotLeft = 48;
  const plotRight = chartWidth - 18;
  const plotTop = 24;
  const plotBottom = 198;
  const maxCount = Math.max(1, ...points.map((point) => point.count));
  const yMax = Math.max(1, Math.ceil(maxCount * 1.12));
  const slotWidth = (plotRight - plotLeft) / points.length;
  const barWidth = Math.max(8, Math.min(20, slotWidth * 0.62));
  const xAt = (index: number) => plotLeft + slotWidth * (index + 0.5);
  const yAt = (count: number) => plotBottom - (count / yMax) * (plotBottom - plotTop);
  const coordinates = points.map((point, index) => ({
    x: xAt(index),
    y: yAt(point.count),
  }));
  const labelInterval = Math.max(1, Math.ceil(points.length / 7));
  const activePoint = activeIndex == null ? null : points[activeIndex];
  const activeCoordinate = activeIndex == null ? null : coordinates[activeIndex];
  const tooltipBelowPoint = (activeCoordinate?.y ?? plotTop) < 78;
  const years = new Set(points.map((point) => new Date(point.period).getUTCFullYear()).filter(Number.isFinite));
  const commonYear = years.size === 1 ? [...years][0] : null;
  const yTicks = [0, Math.round(yMax / 2), yMax];

  return (
    <div className="analysis-trend" role="group" aria-label={`${label} 기간별 추이`}>
      <div className="analysis-trend-canvas" style={{ minWidth: `${chartWidth}px` }}>
        <svg
          viewBox={`0 0 ${chartWidth} ${chartHeight}`}
          preserveAspectRatio="none"
          aria-hidden="true"
        >
          {yTicks.map((tick) => {
            const y = plotBottom - (tick / yMax) * (plotBottom - plotTop);
            return <g key={tick}><line className="analysis-trend-grid" x1={plotLeft} x2={plotRight} y1={y} y2={y} /><text className="analysis-trend-y-label" x={plotLeft - 9} y={y + 3}>{formatCount(tick)}</text></g>;
          })}
          <line className="analysis-trend-axis" x1={plotLeft} x2={plotLeft} y1={plotTop} y2={plotBottom} />
          <line className="analysis-trend-axis" x1={plotLeft} x2={plotRight} y1={plotBottom} y2={plotBottom} />
          {points.map((point, index) => {
            const coordinate = coordinates[index];
            return <rect key={point.period} className={activeIndex === index ? "analysis-trend-bar analysis-trend-bar-active" : "analysis-trend-bar"} x={coordinate.x - barWidth / 2} y={coordinate.y} width={barWidth} height={Math.max(1, plotBottom - coordinate.y)} rx="2" />;
          })}
          <text className="analysis-trend-axis-title" x={plotRight} y={chartHeight - 7}>날짜{commonYear ? ` (${commonYear}년)` : ""}</text>
        </svg>

        {points.map((point, index) => {
          const coordinate = coordinates[index];
          const showDate = index === 0 || index === points.length - 1 || index % labelInterval === 0;
          return (
            <span key={point.period}>
              <button
                type="button"
                className={`analysis-trend-point${activeIndex === index ? " analysis-trend-point-active" : ""}`}
                style={{
                  left: `${(coordinate.x / chartWidth) * 100}%`,
                  top: `${(plotTop / chartHeight) * 100}%`,
                  height: `${((plotBottom - plotTop) / chartHeight) * 100}%`,
                  width: `${Math.max(24, slotWidth) / chartWidth * 100}%`,
                }}
                aria-label={`${formatShortDate(point.period)} ${formatCount(point.count)}개`}
                onMouseEnter={() => setActiveIndex(index)}
                onMouseLeave={() => setActiveIndex(null)}
                onFocus={() => setActiveIndex(index)}
                onBlur={() => setActiveIndex(null)}
              />
              {showDate && (
                <time
                  className="analysis-trend-date"
                  dateTime={point.period}
                  style={{
                    left: `${(coordinate.x / chartWidth) * 100}%`,
                    top: `${((plotBottom + 11) / chartHeight) * 100}%`,
                  }}
                >
                  {chartDateLabel(point.period)}
                </time>
              )}
            </span>
          );
        })}

        {activePoint && activeCoordinate && (
          <div
            className="analysis-trend-tooltip"
            role="status"
            style={{
              left: `${(Math.min(chartWidth - 88, Math.max(88, activeCoordinate.x)) / chartWidth) * 100}%`,
              top: `${((activeCoordinate.y + (tooltipBelowPoint ? 12 : -12)) / chartHeight) * 100}%`,
              transform: `translate(-50%, ${tooltipBelowPoint ? "0" : "-100%"})`,
            }}
          >
            <time dateTime={activePoint.period}>{formatShortDate(activePoint.period)}</time>
            <strong>{formatCount(activePoint.count)}개</strong>
            {label === "댓글 게시" && (
              <small>일반 {formatCount(activePoint.topLevelCount)} · 답글 {formatCount(activePoint.replyCount)}</small>
            )}
          </div>
        )}
      </div>
    </div>
  );
}

function SortableTableHeader({
  children,
  column,
  sort,
  onSort,
}: {
  children: string;
  column: BreakdownSortKey;
  sort: { key: BreakdownSortKey; direction: SortDirection };
  onSort: (key: BreakdownSortKey) => void;
}) {
  const activeDirection = sort?.key === column ? sort.direction : null;
  const nextDirection = activeDirection === "descending" ? "ascending" : "descending";
  const DirectionIcon = activeDirection === "ascending" ? ChevronUpIcon : ChevronDownIcon;
  return (
    <th aria-sort={activeDirection ?? "none"}>
      <span className="analysis-sort-heading">
        <span>{children}</span>
        <button
          type="button"
          className={`analysis-sort-control${activeDirection ? " analysis-sort-active" : ""}`}
          aria-label={`${children} ${nextDirection === "ascending" ? "오름차순" : "내림차순"}으로 정렬`}
          onClick={() => onSort(column)}
        >
          <DirectionIcon aria-hidden="true" />
        </button>
      </span>
    </th>
  );
}

function BreakdownTable({ rows }: { rows: AnalysisBreakdownRow[] }) {
  const [sort, setSort] = useState<{ key: BreakdownSortKey; direction: SortDirection }>({
    key: "videoCount",
    direction: "descending",
  });
  const handleSort = (key: BreakdownSortKey) => {
    setSort((current) => ({
      key,
      direction: current.key === key && current.direction === "descending" ? "ascending" : "descending",
    }));
  };
  const sortedRows = useMemo(() => {
    return rows
      .map((row, index) => ({ row, index }))
      .sort((left, right) => {
        const leftValue = left.row[sort.key];
        const rightValue = right.row[sort.key];
        if (leftValue == null && rightValue == null) return left.index - right.index;
        if (leftValue == null) return 1;
        if (rightValue == null) return -1;

        const comparison = typeof leftValue === "string"
          ? leftValue.localeCompare(String(rightValue), "ko", { numeric: true, sensitivity: "base" })
          : leftValue - Number(rightValue);
        if (comparison === 0) return left.index - right.index;
        return sort.direction === "ascending" ? comparison : -comparison;
      })
      .map(({ row }) => row);
  }, [rows, sort]);

  if (!rows.length) return <p className="analysis-empty">비교할 수집 범위가 없습니다.</p>;
  return (
    <div className="analysis-table-wrap">
      <table className="analysis-table">
        <thead>
          <tr>
            <SortableTableHeader column="label" sort={sort} onSort={handleSort}>범위</SortableTableHeader>
            <SortableTableHeader column="videoCount" sort={sort} onSort={handleSort}>영상</SortableTableHeader>
            <SortableTableHeader column="viewCount" sort={sort} onSort={handleSort}>조회수</SortableTableHeader>
            <SortableTableHeader column="collectedCommentCount" sort={sort} onSort={handleSort}>수집 댓글</SortableTableHeader>
            <SortableTableHeader column="latestPublishedAt" sort={sort} onSort={handleSort}>최근 영상</SortableTableHeader>
          </tr>
        </thead>
        <tbody>
          {sortedRows.map((row) => (
            <tr key={`${row.kind}-${row.id}`}>
              <td><strong>{row.label}</strong><span>{row.kind === "channel" ? "채널" : "키워드"}</span></td>
              <td>{formatCount(row.videoCount)}</td>
              <td>{formatCount(row.viewCount)}</td>
              <td>{formatCount(row.collectedCommentCount)}</td>
              <td>{formatShortDate(row.latestPublishedAt)}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function VideoRanking({
  videos,
  onOpen,
}: {
  videos: AnalysisVideo[];
  onOpen: (video: CollectedVideo, trigger: HTMLElement) => void;
}) {
  if (!videos.length) return <p className="analysis-empty">선택한 기간의 영상이 없습니다.</p>;
  const maxViews = Math.max(1, ...videos.map((video) => video.viewCount ?? 0));
  return (
    <ol className="analysis-video-ranking">
      {videos.map((video, index) => (
        <li key={video.id}>
          <button type="button" onClick={(event) => onOpen(video, event.currentTarget)}>
            <span className="analysis-rank">{String(index + 1).padStart(2, "0")}</span>
            <span className="analysis-video-copy">
              <strong>{video.title}</strong>
              <small>{video.channelTitle ?? video.channelId ?? "채널 정보 없음"} · {formatShortDate(video.publishedAt)}</small>
              <span className="analysis-ranking-bar"><span style={{ width: `${((video.viewCount ?? 0) / maxViews) * 100}%` }} /></span>
            </span>
            <span className="analysis-video-stat"><EyeIcon />{formatCount(video.viewCount)}</span>
            <span className="analysis-video-stat"><ChatBubbleLeftRightIcon />{formatCount(video.collectedCommentCount)}</span>
            <ChevronRightIcon aria-hidden="true" />
          </button>
        </li>
      ))}
    </ol>
  );
}

function collectedVideoFromPerformance(video: AnalysisPerformanceVideo): CollectedVideo {
  return {
    id: video.id,
    youtubeVideoId: video.id,
    ...(video.channelId ? { channelId: video.channelId } : {}),
    title: video.title ?? video.id,
    ...(video.publishedAt ? { publishedAt: video.publishedAt } : {}),
    viewCount: video.viewCount,
    likeCount: video.likeCount,
    commentCount: video.youtubeCommentCount,
    ...(video.statisticsFetchedAt ? { fetchedAt: video.statisticsFetchedAt } : {}),
  };
}

function ActionInsights({
  data,
  onOpen,
}: {
  data: AnalysisInsights;
  onOpen: (video: CollectedVideo, trigger: HTMLElement) => void;
}) {
  const videos = new Map(data.performanceVideos.map((video) => [video.id, video]));
  if (!data.insights.length) return <p className="analysis-empty">표본이 쌓이면 자동 인사이트를 제공합니다.</p>;
  return (
    <div className="analysis-insight-grid">
      {data.insights.map((insight) => {
        const video = insight.videoId ? videos.get(insight.videoId) : undefined;
        const content = <>
          <span className={`analysis-insight-icon analysis-insight-${insight.tone}`}>
            {insight.kind === "growth" || insight.kind === "breakout" ? <ArrowTrendingUpIcon /> : insight.kind === "opportunity" ? <BoltIcon /> : <ChatBubbleLeftRightIcon />}
          </span>
          <span><strong>{insight.title}</strong><small>{insight.description}</small></span>
          {video && <ChevronRightIcon aria-hidden="true" />}
        </>;
        return video
          ? <button key={insight.id} type="button" onClick={(event) => onOpen(collectedVideoFromPerformance(video), event.currentTarget)}>{content}</button>
          : <article key={insight.id}>{content}</article>;
      })}
    </div>
  );
}

function PerformanceRanking({
  videos,
  onOpen,
}: {
  videos: AnalysisPerformanceVideo[];
  onOpen: (video: CollectedVideo, trigger: HTMLElement) => void;
}) {
  const [metric, setMetric] = useState<PerformanceMetric>("growth");
  const metricValue = (video: AnalysisPerformanceVideo) => {
    if (metric === "growth") return video.viewGrowth7d ?? -1;
    if (metric === "velocity") return video.viewsPerDay;
    if (metric === "engagement") return video.engagementRate;
    return video.commentRatePerThousand;
  };
  const ranked = [...videos]
    .filter((video) => metric !== "growth" || video.viewGrowth7d !== undefined)
    .sort((left, right) => metricValue(right) - metricValue(left))
    .slice(0, 10);
  const valueLabel = (video: AnalysisPerformanceVideo) => {
    if (metric === "growth") return `+${formatCount(video.viewGrowth7d)}`;
    if (metric === "velocity") return `${formatCount(Math.round(video.viewsPerDay))}/일`;
    if (metric === "engagement") return `${video.engagementRate.toFixed(2)}%`;
    return `${video.commentRatePerThousand.toFixed(1)}/천`;
  };
  return <>
    <div className="analysis-metric-tabs" role="group" aria-label="영상 성과 기준">
      {([
        ["growth", "7일 성장"],
        ["velocity", "조회 효율"],
        ["engagement", "참여율"],
        ["conversation", "댓글 전환"],
      ] as const).map(([value, label]) => (
        <button key={value} type="button" className={metric === value ? "analysis-metric-active" : ""} onClick={() => setMetric(value)}>{label}</button>
      ))}
    </div>
    {ranked.length ? <ol className="analysis-performance-ranking">
      {ranked.map((video, index) => (
        <li key={video.id}>
          <button type="button" onClick={(event) => onOpen(collectedVideoFromPerformance(video), event.currentTarget)}>
            <span className="analysis-rank">{String(index + 1).padStart(2, "0")}</span>
            <span><strong>{video.title ?? video.id}</strong><small>{video.channelTitle ?? video.channelId ?? "채널 정보 없음"}</small></span>
            <b>{valueLabel(video)}</b>
            <ChevronRightIcon aria-hidden="true" />
          </button>
        </li>
      ))}
    </ol> : <p className="analysis-empty">{metric === "growth" ? "7일 비교가 가능한 영상이 아직 없습니다." : "비교할 영상이 없습니다."}</p>}
  </>;
}

function PublishingHeatmap({ cells }: { cells: AnalysisInsights["publishingHeatmap"] }) {
  const weekdays = ["월", "화", "수", "목", "금", "토", "일"];
  const hours = [0, 3, 6, 9, 12, 15, 18, 21];
  const lookup = new Map(cells.map((cell) => [`${cell.weekday}-${cell.hourBucket}`, cell]));
  const max = Math.max(1, ...cells.map((cell) => cell.medianViewsPerDay));
  return (
    <div className="analysis-heatmap-block">
      <div className="analysis-heatmap-legend" role="note" aria-label="색 농도 범례: 연할수록 중앙 조회수가 낮고 진할수록 높습니다">
        <span>낮음</span>
        <span className="analysis-heatmap-gradient" aria-hidden="true" />
        <span>높음</span>
        <small>색이 진할수록 해당 시간대의 중앙 조회/일이 높습니다.</small>
      </div>
      <div className="analysis-heatmap-scroll">
        <div className="analysis-heatmap">
          <span />
          {hours.map((hour) => <strong key={hour}>{String(hour).padStart(2, "0")}–{String((hour + 3) % 24).padStart(2, "0")}</strong>)}
          {weekdays.flatMap((weekday, index) => [
            <strong key={`${weekday}-label`}>{weekday}</strong>,
            ...hours.map((hour) => {
              const cell = lookup.get(`${index + 1}-${hour}`);
              const endHour = (hour + 3) % 24;
              const rangeLabel = `${String(hour).padStart(2, "0")}–${String(endHour).padStart(2, "0")}`;
              const intensity = cell ? 0.12 + (cell.medianViewsPerDay / max) * 0.78 : 0;
              const description = cell
                ? `${weekday}요일 ${rangeLabel} · 영상 ${cell.videoCount}개 · 중앙 ${formatCount(Math.round(cell.medianViewsPerDay))}회/일`
                : `${weekday}요일 ${rangeLabel} · 영상 없음`;
              return <div
                key={`${weekday}-${hour}`}
                className={cell ? "" : "analysis-heatmap-empty-cell"}
                role="img"
                aria-label={description}
                style={cell ? { backgroundColor: `rgba(226, 111, 55, ${intensity})` } : undefined}
                title={description}
              >
                {cell && <><b>{formatCount(Math.round(cell.medianViewsPerDay))}</b><small>{cell.videoCount}개</small></>}
              </div>;
            }),
          ])}
        </div>
      </div>
    </div>
  );
}

export function AnalysisDashboard({
  sources,
  channels,
  onOpenVideo,
  onOpenComment,
}: {
  sources: SourceSummary[];
  channels: ExploreChannel[];
  onOpenVideo: (video: CollectedVideo, trigger: HTMLElement) => void;
  onOpenComment: (commentId: string, trigger: HTMLElement) => void;
}) {
  const [view, setView] = useState<AnalysisView>("overview");
  const [scope, setScope] = useState<AnalysisScope>("all");
  const [period, setPeriod] = useState<Period>("30");
  const [commentType, setCommentType] = useState<AnalysisCommentType>("all");
  const [channelId, setChannelId] = useState("");
  const [targetId, setTargetId] = useState("");
  const [data, setData] = useState<AnalysisOverview | null>(null);
  const [insightsData, setInsightsData] = useState<AnalysisInsights | null>(null);
  const [insightsError, setInsightsError] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [refreshKey, setRefreshKey] = useState(0);
  const keywordSources = useMemo(
    () => sources.filter((source) => source.type === "keyword" && source.targetId),
    [sources],
  );

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    setError(null);
    setInsightsError(null);
    setInsightsData(null);
    const query = {
      scope,
      ...(scope === "channel" && channelId ? { channelId } : {}),
      ...(scope === "keyword" && targetId ? { targetId } : {}),
      ...periodRange(period),
      commentType,
      limit: 10,
    };
    const load = async () => {
      try {
        let overview: AnalysisOverview;
        if (period === "all") {
          overview = await getAnalysisOverview({ ...query, section: "core" });
          if (cancelled) return;
          setData(overview);
          const [contentResult, insightsResult] = await Promise.allSettled([
            getAnalysisOverview({ ...query, section: "content" }),
            getAnalysisInsights({ ...query, limit: 20 }),
          ]);
          if (cancelled) return;
          if (contentResult.status === "fulfilled") {
            const content = contentResult.value;
            setData({
              ...overview,
              topComments: content.topComments,
              topWords: content.topWords,
              commentSignals: {
                ...overview.commentSignals,
                questionRate: content.commentSignals.questionRate,
                questionCount: content.commentSignals.questionCount,
                questionSampleSize: content.commentSignals.questionSampleSize,
              },
              coverage: {
                ...overview.coverage,
                sampledComments: content.coverage.sampledComments,
              },
            });
          } else {
            setError(contentResult.reason instanceof Error ? contentResult.reason.message : "댓글 텍스트 분석을 불러오지 못했습니다.");
          }
          if (insightsResult.status === "fulfilled") {
            setInsightsData(insightsResult.value);
          } else {
            setInsightsError(insightsResult.reason instanceof Error ? insightsResult.reason.message : "성과 인사이트를 불러오지 못했습니다.");
          }
        } else {
          overview = await getAnalysisOverview(query);
          if (cancelled) return;
          setData(overview);
          try {
            const insights = await getAnalysisInsights({ ...query, limit: 20 });
            if (!cancelled) setInsightsData(insights);
          } catch (caught) {
            if (!cancelled) setInsightsError(caught instanceof Error ? caught.message : "성과 인사이트를 불러오지 못했습니다.");
          }
        }
      } catch (caught) {
        if (!cancelled) {
          setError(caught instanceof Error ? caught.message : "통계를 불러오지 못했습니다.");
        }
      } finally {
        if (!cancelled) setLoading(false);
      }
    };
    void load();
    return () => { cancelled = true; };
  }, [channelId, commentType, period, refreshKey, scope, targetId]);

  const summary = data?.summary;
  const breakdown = scope === "keyword" ? data?.keywordBreakdown : data?.channelBreakdown;

  return (
    <section className="analysis-page" id="analysis" aria-labelledby="analysis-title">
      <header className="analysis-heading">
        <div>
          <p className="section-kicker">WORKSPACE INTELLIGENCE</p>
          <h1 id="analysis-title">Analysis</h1>
          <p>수집된 영상 성과와 공개 댓글 반응을 한 화면에서 비교합니다.</p>
        </div>
        <div className="analysis-freshness">
          <span className={data?.coverage.partialData ? "analysis-dot analysis-dot-warning" : "analysis-dot"} />
          <span>{data ? `갱신 ${formatShortDate(data.coverage.generatedAt)}` : "집계 준비 중"}</span>
          <button type="button" onClick={() => setRefreshKey((key) => key + 1)} disabled={loading} aria-label="분석 새로고침">
            <ArrowPathIcon className={loading ? "icon-spinning" : ""} />
          </button>
        </div>
      </header>

      <div className="analysis-toolbar" aria-label="분석 조건">
        <div className="analysis-tabs" role="tablist" aria-label="분석 화면">
          {([["overview", "Overview"], ["videos", "Videos"], ["comments", "Comments"]] as const).map(([id, label]) => (
            <button key={id} type="button" role="tab" aria-selected={view === id} className={view === id ? "analysis-tab-active" : ""} onClick={() => setView(id)}>{label}</button>
          ))}
        </div>
        <div className="analysis-filters">
          <label>범위<select value={scope} onChange={(event) => setScope(event.target.value as AnalysisScope)}><option value="all">전체</option><option value="channel">채널별</option><option value="keyword">키워드별</option></select></label>
          {scope === "channel" && <label>채널<select value={channelId} onChange={(event) => setChannelId(event.target.value)}><option value="">전체 채널</option>{channels.map((channel) => <option key={channel.youtubeChannelId} value={channel.youtubeChannelId}>{channel.title ?? channel.handle ?? channel.youtubeChannelId}</option>)}</select></label>}
          {scope === "keyword" && <label>키워드<select value={targetId} onChange={(event) => setTargetId(event.target.value)}><option value="">전체 키워드</option>{keywordSources.map((source) => <option key={source.id} value={source.targetId}>{sourceLabel(source)}</option>)}</select></label>}
          <label>기간<select value={period} onChange={(event) => setPeriod(event.target.value as Period)}><option value="7">최근 7일</option><option value="30">최근 30일</option><option value="90">최근 90일</option><option value="all">전체</option></select></label>
          <label>댓글<select value={commentType} onChange={(event) => setCommentType(event.target.value as AnalysisCommentType)}><option value="all">전체</option><option value="top_level">일반 댓글</option><option value="reply">답글</option></select></label>
        </div>
      </div>

      {error && <div className="analysis-error" role="alert"><InformationCircleIcon /><span>{error}</span><button type="button" onClick={() => setRefreshKey((key) => key + 1)}>다시 시도</button></div>}
      {loading && !data && <div className="analysis-loading"><ArrowPathIcon className="icon-spinning" /><span>영상과 댓글 통계를 집계하는 중입니다…</span></div>}

      {data && <>
        <div className="analysis-kpis">
          {(view !== "comments") && <>
            <article><span><FilmIcon />영상</span><strong>{formatCount(summary?.videoCount)}</strong><small>기간 내 공개 영상</small></article>
            <article><span><EyeIcon />총 조회수</span><strong>{formatCount(summary?.totalViewCount)}</strong><small>영상당 중앙값 {formatCount(summary?.medianViewCount)}</small></article>
          </>}
          {(view !== "videos") && <>
            <article><span><ChatBubbleLeftRightIcon />수집 댓글</span><strong>{formatCount(summary?.collectedCommentCount)}</strong><small>YouTube 표시 {formatCount(summary?.youtubeCommentCount)}</small></article>
            <article><span><UsersIcon />식별 작성자</span><strong>{formatCount(summary?.identifiedAuthorCount)}</strong><small>댓글 영상 {formatCount(summary?.commentedVideoCount)}개</small></article>
          </>}
          {view === "videos" && <>
            <article><span><HandThumbUpIcon />총 좋아요</span><strong>{formatCount(summary?.totalLikeCount)}</strong><small>현재 저장 통계 기준</small></article>
            <article><span><ClockIcon />최근 게시</span><strong>{formatShortDate(summary?.latestVideoPublishedAt)}</strong><small>영상 게시일 기준</small></article>
          </>}
          {view === "comments" && <>
            <article><span><ChatBubbleLeftRightIcon />일반 댓글</span><strong>{formatCount(summary?.topLevelCount)}</strong><small>답글 {formatCount(summary?.replyCount)}</small></article>
            <article><span><HandThumbUpIcon />평균 좋아요</span><strong>{(summary?.averageCommentLikeCount ?? 0).toFixed(1)}</strong><small>댓글 1개당</small></article>
          </>}
        </div>

        <div className="analysis-layout">
          <section className="analysis-panel analysis-panel-wide analysis-storage-panel">
            <div className="analysis-panel-heading">
              <div><p className="section-kicker">CORPUS STORAGE</p><h2>수집 데이터 규모</h2></div>
              <span className="analysis-storage-heading-meta">
                현재 분석 조건 기준
                <span className="analysis-storage-info">
                  <button type="button" aria-label="공백 토큰 집계 기준" aria-describedby="analysis-storage-tooltip"><InformationCircleIcon /></button>
                  <span id="analysis-storage-tooltip" role="tooltip">앞뒤 공백을 제거한 뒤 연속된 공백·줄바꿈으로 분리한 단어 수입니다. LLM 토큰이나 형태소 분석 토큰과는 다릅니다.</span>
                </span>
              </span>
            </div>
            <div className="analysis-storage-grid">
              <StorageMetric
                kind="transcript"
                documents={data.storageMetrics.transcriptDocumentCount}
                tokens={data.storageMetrics.transcriptWhitespaceTokenCount}
                countedDocuments={data.storageMetrics.transcriptCountedDocumentCount}
              />
              <StorageMetric
                kind="comment"
                documents={data.storageMetrics.commentDocumentCount}
                tokens={data.storageMetrics.commentWhitespaceTokenCount}
                countedDocuments={data.storageMetrics.commentCountedDocumentCount}
              />
            </div>
          </section>

          {view !== "comments" && insightsError && <section className="analysis-panel analysis-panel-wide"><div className="analysis-inline-notice"><InformationCircleIcon /><span>{insightsError}</span></div></section>}

          {view !== "comments" && insightsData && <section className="analysis-panel analysis-panel-wide analysis-actions-panel">
            <div className="analysis-panel-heading"><div><p className="section-kicker">ACTIONABLE SIGNALS</p><h2>지금 확인할 성과</h2></div><span>{formatCount(insightsData.coverage.comparableVideoCount)}개 비교 가능</span></div>
            <ActionInsights data={insightsData} onOpen={onOpenVideo} />
          </section>}

          {view !== "comments" && insightsData && <section className="analysis-panel analysis-panel-wide">
            <div className="analysis-panel-heading"><div><p className="section-kicker">PERFORMANCE</p><h2>영상 성과 지표</h2></div><span>게시 경과일 보정</span></div>
            <div className="analysis-performance-summary">
              <article><span><EyeIcon />중앙 조회 효율</span><strong>{formatCount(Math.round(insightsData.performanceSummary.medianViewsPerDay))}</strong><small>영상당 일평균 조회수</small></article>
              <article><span><HandThumbUpIcon />중앙 좋아요율</span><strong>{insightsData.performanceSummary.medianLikeRate.toFixed(2)}%</strong><small>조회수 대비 좋아요</small></article>
              <article><span><ChatBubbleLeftRightIcon />댓글 전환</span><strong>{insightsData.performanceSummary.medianCommentRatePerThousand.toFixed(1)}</strong><small>조회 1천 회당 댓글</small></article>
              <article><span><ArrowTrendingUpIcon />7일 조회 증가</span><strong>+{formatCount(insightsData.performanceSummary.totalViewGrowth7d)}</strong><small>{formatCount(insightsData.performanceSummary.snapshotEligible7d)}개 영상 비교</small></article>
            </div>
          </section>}

          {view !== "comments" && <section className="analysis-panel analysis-panel-wide"><div className="analysis-panel-heading"><div><p className="section-kicker">PUBLISHING</p><h2>영상 게시 추이</h2></div><span>{formatCount(summary?.videoCount)}개</span></div><TrendChart points={data.videoTrend} label="영상 게시" period={period} /></section>}
          {view !== "videos" && <section className="analysis-panel analysis-panel-wide"><div className="analysis-panel-heading"><div><p className="section-kicker">CONVERSATION</p><h2>댓글 게시 추이</h2></div><span>{formatCount(summary?.collectedCommentCount)}개</span></div><TrendChart points={data.commentTrend} label="댓글 게시" period={period} /></section>}

          {view !== "comments" && insightsData && <section className="analysis-panel analysis-panel-wide"><div className="analysis-panel-heading"><div><p className="section-kicker">GROWTH & EFFICIENCY</p><h2>성과 영상 순위</h2></div><span>기준 전환 가능</span></div><PerformanceRanking videos={insightsData.performanceVideos} onOpen={onOpenVideo} /></section>}

          {view !== "comments" && insightsData && <section className="analysis-panel analysis-panel-publishing"><div className="analysis-panel-heading"><div><p className="section-kicker">TIMING</p><h2>게시 요일·시간 성과</h2></div><span>한국 시간 · 중앙 조회/일</span></div><PublishingHeatmap cells={insightsData.publishingHeatmap} /></section>}

          {view !== "videos" && <section className="analysis-panel analysis-panel-signals"><div className="analysis-panel-heading"><div><p className="section-kicker">COMMENT SIGNALS</p><h2>댓글 반응 신호</h2></div><span>질문은 표본 기반</span></div><div className="analysis-comment-signals">
            <article><ChatBubbleLeftRightIcon /><strong>{data.commentSignals.replyRate.toFixed(1)}%</strong><span>답글 비율</span><small>댓글 간 대화 활성도</small></article>
            <article><UsersIcon /><strong>{data.commentSignals.authorDiversityRate.toFixed(1)}%</strong><span>작성자 다양성</span><small>식별 작성자 ÷ 댓글</small></article>
            <article><BoltIcon /><strong>{data.commentSignals.questionRate.toFixed(1)}%</strong><span>질문형 댓글</span><small>{formatCount(data.commentSignals.questionCount)} / {formatCount(data.commentSignals.questionSampleSize)} 표본</small></article>
          </div></section>}

          {view === "overview" && <section className="analysis-panel analysis-panel-wide"><div className="analysis-panel-heading"><div><p className="section-kicker">COMPARISON</p><h2>{scope === "keyword" ? "키워드별 성과" : "채널별 성과"}</h2></div><span>상위 {formatCount(breakdown?.length)}</span></div><BreakdownTable rows={breakdown ?? []} /></section>}

          {view !== "comments" && <section className="analysis-panel analysis-panel-wide"><div className="analysis-panel-heading"><div><p className="section-kicker">TOP CONTENT</p><h2>조회수 상위 영상</h2></div><span>최대 10개</span></div><VideoRanking videos={data.topVideos} onOpen={onOpenVideo} /></section>}

          {view !== "videos" && <section className="analysis-panel analysis-panel-comments"><div className="analysis-panel-heading"><div><p className="section-kicker">TOP COMMENTS</p><h2>반응이 큰 댓글</h2></div><span>좋아요순</span></div>{data.topComments.length ? <ul className="analysis-comment-list">{data.topComments.map((comment) => <li key={comment.id}><button type="button" onClick={(event: MouseEvent<HTMLButtonElement>) => onOpenComment(comment.id, event.currentTarget)}><span className="analysis-comment-meta"><strong>{comment.authorName ?? "YouTube 사용자"}</strong><span>{comment.isReply ? "답글" : "댓글"} · 좋아요 {formatCount(comment.likeCount)}</span></span><p>{comment.text ?? "내용이 없는 댓글입니다."}</p><small>{comment.channelTitle ?? "채널"} · {comment.videoTitle ?? comment.videoId}</small></button></li>)}</ul> : <p className="analysis-empty">선택한 조건의 댓글이 없습니다.</p>}</section>}

          <KeywordFrequencyPanel
            key={view}
            videoKeywords={data.videoKeywords}
            commentKeywords={data.commentKeywords}
            indexedVideoDocuments={data.keywordCoverage.indexedVideoDocuments}
            indexedCommentDocuments={data.keywordCoverage.indexedCommentDocuments}
            preferredCorpus={view === "comments" ? "comment" : "video"}
            fullWidth={view === "videos"}
            onSaved={() => setRefreshKey((key) => key + 1)}
          />
        </div>

        <footer className="analysis-coverage"><InformationCircleIcon /><p>조회수·좋아요는 가장 최근 저장된 영상 통계이며, 기간 필터는 영상은 게시일·댓글은 댓글 게시일에 각각 적용됩니다. {data.coverage.partialData ? "일부 수집 대상은 댓글 범위가 제한되어 있습니다." : "현재 선택 범위의 저장 데이터를 모두 반영했습니다."}</p></footer>
      </>}
    </section>
  );
}
