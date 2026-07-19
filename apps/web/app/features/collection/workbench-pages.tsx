import {
  ArrowPathIcon,
  CheckCircleIcon,
  EllipsisHorizontalIcon,
  ExclamationTriangleIcon,
  PlusIcon,
} from "@heroicons/react/24/outline";

import type { ExploreData, RecentJobFailure, SourceSummary } from "../../lib/api";
import { SourceCollectionState } from "./workbench-components";
import {
  formatCount,
  formatDate,
  sourceLabel,
  sourceTargetValue,
  sourceTypeCopy,
  type WorkspacePage,
} from "./workbench-model";

type StatusPageProps = {
  failures: RecentJobFailure[];
  error: string | null;
  loading: boolean;
  onRefresh: () => void;
};

export function StatusPage({ failures, error, loading, onRefresh }: StatusPageProps) {
  return (
    <section className="status-page" aria-labelledby="status-page-title">
      <div className="workspace-page-heading status-page-heading">
        <p className="section-kicker">COLLECTION OPERATIONS</p>
        <h1 id="status-page-title">Status</h1>
        <p>현재 계정이 구독 중인 공유 수집 대상의 상태와 최근 실패 원인, 재시도 가능 여부를 확인합니다.</p>
      </div>
      <section className="panel recent-failures-panel" aria-labelledby="recent-failures-title" aria-busy={loading}>
        <div className="panel-heading">
          <div>
            <p className="section-kicker">RECENT FAILURES</p>
            <h2 id="recent-failures-title">최근 공유 수집 대상 실패</h2>
          </div>
          <button className="icon-button" type="button" onClick={onRefresh} disabled={loading} aria-label="최근 수집 실패 새로고침">
            <ArrowPathIcon className={loading ? "icon-spinning" : undefined} aria-hidden="true" />
          </button>
        </div>

        {error && (
          <div className="recent-failures-state recent-failures-error" role="status">
            <ExclamationTriangleIcon aria-hidden="true" />
            <div><strong>실패 현황을 불러오지 못했습니다.</strong><p>{error}</p></div>
            <button type="button" onClick={onRefresh}>다시 시도</button>
          </div>
        )}
        {!error && loading && failures.length === 0 && (
          <div className="recent-failures-state" role="status"><span className="loading-spinner" aria-hidden="true" /><p>최근 실패 기록을 불러오는 중입니다.</p></div>
        )}
        {!error && !loading && failures.length === 0 && (
          <div className="recent-failures-state recent-failures-empty" role="status">
            <CheckCircleIcon aria-hidden="true" />
            <div><strong>구독 중인 공유 수집 대상의 최근 실패가 없습니다.</strong><p>새 실패가 기록되면 대상과 원인을 이곳에 표시합니다.</p></div>
          </div>
        )}
        {failures.length > 0 && (
          <ol className="recent-failure-list" aria-live="polite">
            {failures.map((failure) => (
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
            ))}
          </ol>
        )}
      </section>
    </section>
  );
}

type SourcesPageProps = {
  page: WorkspacePage;
  sources: SourceSummary[];
  explore: ExploreData;
  activeSourceId: string;
  openMenuId: string | null;
  updatingSourceId: string | null;
  deletingSourceId: string | null;
  onAdd: (type?: "keyword") => void;
  onOpen: (sourceId: string) => void;
  onMenuChange: (sourceId: string | null) => void;
  onToggleRefresh: (source: SourceSummary) => void;
  onRemove: (source: SourceSummary) => void;
};

export function SourcesPage({
  page,
  sources,
  explore,
  activeSourceId,
  openMenuId,
  updatingSourceId,
  deletingSourceId,
  onAdd,
  onOpen,
  onMenuChange,
  onToggleRefresh,
  onRemove,
}: SourcesPageProps) {
  const isKeywords = page === "keywords";
  const displayedSources = isKeywords ? sources.filter((source) => source.type === "keyword") : sources;

  return (
    <section className="sources-page" aria-labelledby="sources-page-title">
      <div className="workspace-page-heading"><p className="section-kicker">{isKeywords ? "KEYWORD COLLECTION" : "COLLECTION TARGETS"}</p><h1 id="sources-page-title">{isKeywords ? "Keywords" : "Sources"}</h1><p>{isKeywords ? "등록한 키워드의 수집 범위와 최신 상태를 관리합니다." : "중복 없이 정규화된 수집 대상을 관리하고, 선택한 대상의 수집 범위를 확인합니다."}</p></div>
      <div className="sources-page-actions">
        <button className="source-add-button" type="button" onClick={() => onAdd(isKeywords ? "keyword" : undefined)} aria-label={isKeywords ? "키워드 등록" : "수집 대상 추가"}>
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
            const menuOpen = openMenuId === source.id;
            const channel = source.targetId ? explore.channels.find((item) => item.targetId === source.targetId) : undefined;
            const targetValue = sourceTargetValue(source);
            const channelName = channel?.title ?? channel?.handle ?? (source.type === "channel" ? "채널 정보 확인 중" : targetValue);
            const channelId = channel?.youtubeChannelId ?? targetValue;
            return (
              <article key={source.id} className={`source-page-card${source.id === activeSourceId ? " source-page-card-active" : ""}${menuOpen ? " source-page-card-menu-open" : ""}`}>
                <button type="button" className="source-page-select" onClick={() => { onMenuChange(null); onOpen(source.id); }} aria-label={`${sourceLabel(source)} 작업 공간 열기`}>
                  <span className="source-type-chip">{sourceTypeCopy(source.type)}</span>
                  <strong title={channelId}>{channelName}</strong>
                </button>
                <SourceCollectionState source={source} />
                <span className="source-collection-rate source-video-collection-rate">{channel?.videoCollectionRate === undefined ? "—" : `${channel.videoCollectionRate}%`}</span>
                <span className="source-collection-rate source-comment-collection-rate">{channel?.commentCollectionRate === undefined ? "—" : `${channel.commentCollectionRate}%`}</span>
                <div className="source-card-actions">
                  <button className="source-more-button" type="button" disabled={deletingSourceId === source.id} onClick={() => onMenuChange(menuOpen ? null : source.id)} aria-label={`${sourceLabel(source)} 관리 메뉴`} aria-expanded={menuOpen} aria-haspopup="menu"><EllipsisHorizontalIcon aria-hidden="true" /></button>
                  {menuOpen && <div className="source-action-menu" role="menu" aria-label={`${sourceLabel(source)} 관리`}>
                    {canToggleRefresh && <button type="button" role="menuitem" disabled={updatingSourceId === source.id} onClick={() => { onMenuChange(null); onToggleRefresh(source); }}>{source.enabled ? "수집 일시정지" : "수집 재개"}</button>}
                    <button className="source-action-menu-delete" type="button" role="menuitem" onClick={() => onRemove(source)}>삭제</button>
                  </div>}
                </div>
              </article>
            );
          })}
          {displayedSources.length === 0 && <div className="explore-empty">{isKeywords ? "아직 등록된 키워드가 없습니다." : "아직 등록된 수집 대상이 없습니다."}</div>}
        </div>
      </div>
    </section>
  );
}
