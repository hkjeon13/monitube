"use client";

import {
  ArrowPathIcon,
  ChatBubbleLeftRightIcon,
  ChevronRightIcon,
  ClockIcon,
  EyeIcon,
  FilmIcon,
  HandThumbUpIcon,
  InformationCircleIcon,
  UsersIcon,
} from "@heroicons/react/24/outline";
import type { MouseEvent } from "react";
import { useEffect, useMemo, useState } from "react";

import {
  getAnalysisOverview,
  type AnalysisBreakdownRow,
  type AnalysisCommentType,
  type AnalysisOverview,
  type AnalysisScope,
  type AnalysisTrendPoint,
  type AnalysisVideo,
  type CollectedVideo,
  type ExploreChannel,
  type SourceSummary,
} from "../../lib/api";
import { formatCount, formatShortDate, sourceLabel } from "../collection/workbench-model";

type AnalysisView = "overview" | "videos" | "comments";
type Period = "7" | "30" | "90" | "all";

function periodRange(period: Period) {
  if (period === "all") return {};
  const to = new Date();
  const from = new Date(to);
  from.setUTCDate(from.getUTCDate() - Number(period) + 1);
  return {
    from: from.toISOString().slice(0, 10),
    to: to.toISOString().slice(0, 10),
  };
}

function TrendBars({ points, label }: { points: AnalysisTrendPoint[]; label: string }) {
  const max = Math.max(1, ...points.map((point) => point.count));
  if (!points.length) return <p className="analysis-empty">선택한 조건에 해당하는 추이가 없습니다.</p>;
  return (
    <div className="analysis-trend" role="img" aria-label={`${label} 기간별 추이`}>
      {points.map((point) => (
        <div className="analysis-trend-item" key={point.period}>
          <span className="analysis-trend-value">{formatCount(point.count)}</span>
          <span className="analysis-trend-track">
            <span style={{ height: `${Math.max(4, (point.count / max) * 100)}%` }} />
          </span>
          <time dateTime={point.period}>{formatShortDate(point.period).replace(/\s/g, "")}</time>
        </div>
      ))}
    </div>
  );
}

function BreakdownTable({ rows }: { rows: AnalysisBreakdownRow[] }) {
  if (!rows.length) return <p className="analysis-empty">비교할 수집 범위가 없습니다.</p>;
  return (
    <div className="analysis-table-wrap">
      <table className="analysis-table">
        <thead><tr><th>범위</th><th>영상</th><th>조회수</th><th>수집 댓글</th><th>최근 영상</th></tr></thead>
        <tbody>
          {rows.map((row) => (
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
    void getAnalysisOverview({
      scope,
      ...(scope === "channel" && channelId ? { channelId } : {}),
      ...(scope === "keyword" && targetId ? { targetId } : {}),
      ...periodRange(period),
      commentType,
      limit: 10,
    }).then((response) => {
      if (!cancelled) setData(response);
    }).catch((caught) => {
      if (!cancelled) setError(caught instanceof Error ? caught.message : "통계를 불러오지 못했습니다.");
    }).finally(() => {
      if (!cancelled) setLoading(false);
    });
    return () => { cancelled = true; };
  }, [channelId, commentType, period, refreshKey, scope, targetId]);

  const summary = data?.summary;
  const breakdown = scope === "keyword" ? data?.keywordBreakdown : data?.channelBreakdown;
  const sampled = data?.coverage.sampledComments ?? 0;
  const total = data?.coverage.totalComments ?? 0;

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
          {view !== "comments" && <section className="analysis-panel analysis-panel-wide"><div className="analysis-panel-heading"><div><p className="section-kicker">PUBLISHING</p><h2>영상 게시 추이</h2></div><span>{formatCount(summary?.videoCount)}개</span></div><TrendBars points={data.videoTrend} label="영상 게시" /></section>}
          {view !== "videos" && <section className="analysis-panel analysis-panel-wide"><div className="analysis-panel-heading"><div><p className="section-kicker">CONVERSATION</p><h2>댓글 게시 추이</h2></div><span>{formatCount(summary?.collectedCommentCount)}개</span></div><TrendBars points={data.commentTrend} label="댓글 게시" /></section>}

          {view === "overview" && <section className="analysis-panel analysis-panel-wide"><div className="analysis-panel-heading"><div><p className="section-kicker">COMPARISON</p><h2>{scope === "keyword" ? "키워드별 성과" : "채널별 성과"}</h2></div><span>상위 {formatCount(breakdown?.length)}</span></div><BreakdownTable rows={breakdown ?? []} /></section>}

          {view !== "comments" && <section className="analysis-panel analysis-panel-wide"><div className="analysis-panel-heading"><div><p className="section-kicker">TOP CONTENT</p><h2>조회수 상위 영상</h2></div><span>최대 10개</span></div><VideoRanking videos={data.topVideos} onOpen={onOpenVideo} /></section>}

          {view !== "videos" && <section className="analysis-panel analysis-panel-comments"><div className="analysis-panel-heading"><div><p className="section-kicker">TOP COMMENTS</p><h2>반응이 큰 댓글</h2></div><span>좋아요순</span></div>{data.topComments.length ? <ul className="analysis-comment-list">{data.topComments.map((comment) => <li key={comment.id}><button type="button" onClick={(event: MouseEvent<HTMLButtonElement>) => onOpenComment(comment.id, event.currentTarget)}><span className="analysis-comment-meta"><strong>{comment.authorName ?? "YouTube 사용자"}</strong><span>{comment.isReply ? "답글" : "댓글"} · 좋아요 {formatCount(comment.likeCount)}</span></span><p>{comment.text ?? "내용이 없는 댓글입니다."}</p><small>{comment.channelTitle ?? "채널"} · {comment.videoTitle ?? comment.videoId}</small></button></li>)}</ul> : <p className="analysis-empty">선택한 조건의 댓글이 없습니다.</p>}</section>}

          {view !== "videos" && <section className="analysis-panel analysis-panel-words"><div className="analysis-panel-heading"><div><p className="section-kicker">LANGUAGE</p><h2>자주 등장한 단어</h2></div><span>{formatCount(sampled)} / {formatCount(total)} 표본</span></div>{data.topWords.length ? <ol className="analysis-word-list">{data.topWords.map((word, index) => <li key={word.label}><span>{index + 1}</span><strong>{word.label}</strong><small>{formatCount(word.count)}</small></li>)}</ol> : <p className="analysis-empty">분석할 댓글 단어가 없습니다.</p>}</section>}
        </div>

        <footer className="analysis-coverage"><InformationCircleIcon /><p>조회수·좋아요는 가장 최근 저장된 영상 통계이며, 기간 필터는 영상은 게시일·댓글은 댓글 게시일에 각각 적용됩니다. {data.coverage.partialData ? "일부 수집 대상은 댓글 범위가 제한되어 있습니다." : "현재 선택 범위의 저장 데이터를 모두 반영했습니다."}</p></footer>
      </>}
    </section>
  );
}
