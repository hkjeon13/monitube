import { ChevronRightIcon, MagnifyingGlassIcon } from "@heroicons/react/24/outline";
import type { FormEvent, RefObject } from "react";

import { CommentRow } from "../../components/comment-thread";
import type {
  ChannelSubscriberSnapshot,
  CollectedSearchData,
  CollectedSearchScope,
  CollectedVideo,
  ExploreChannel,
  ExploreData,
} from "../../lib/api";
import { SubscriberTrend } from "./workbench-components";
import {
  formatCount,
  formatShortDate,
  searchFieldLabel,
  searchScopeLabels,
  youtubeThumbnail,
} from "./workbench-model";

type ChannelOverviewProps = {
  channel: ExploreChannel;
  subscriberHistory: ChannelSubscriberSnapshot[];
};

function ChannelOverview({ channel, subscriberHistory }: ChannelOverviewProps) {
  return (
    <section className="explore-channel-overview" aria-labelledby="channel-overview-title">
      <div className="channel-overview-avatar">
        {channel.thumbnailUrl ? <img src={channel.thumbnailUrl} alt="" /> : <span>{(channel.title ?? channel.handle ?? "Y").slice(0, 1).toUpperCase()}</span>}
      </div>
      <div className="channel-overview-copy">
        <p className="section-kicker">CHANNEL OVERVIEW</p>
        <h3 id="channel-overview-title">{channel.title ?? channel.handle ?? channel.youtubeChannelId}</h3>
        <p className="channel-overview-id">{channel.handle ? `@${channel.handle.replace(/^@/, "")} · ` : ""}{channel.youtubeChannelId}</p>
        {channel.description && <p className="channel-overview-description">{channel.description}</p>}
      </div>
      <dl className="channel-overview-stats">
        <div><dt>구독자</dt><dd>{channel.hiddenSubscriberCount ? "비공개" : formatCount(channel.subscriberCount)}</dd></div>
        <div><dt>채널 영상</dt><dd>{formatCount(channel.youtubeVideoCount ?? channel.videoCount)}</dd></div>
        <div><dt>저장 영상</dt><dd>{formatCount(channel.videoCount)}</dd></div>
        <div><dt>수집 댓글</dt><dd>{formatCount(channel.commentCount)}</dd></div>
      </dl>
      {!channel.hiddenSubscriberCount && <SubscriberTrend samples={subscriberHistory} />}
    </section>
  );
}

type ChannelStripProps = {
  channels: ExploreChannel[];
  videos: CollectedVideo[];
  selectedChannelId: string | null;
  emptyMessage?: string;
  onSelect: (channelId: string | null, targetId?: string) => void;
};

function ChannelStrip({ channels, videos, selectedChannelId, emptyMessage, onSelect }: ChannelStripProps) {
  return (
    <div className="explore-channel-strip" aria-label="수집된 채널">
      {channels.map((channel) => {
        const coverVideo = videos.find((video) => video.channelId === channel.youtubeChannelId);
        const selected = channel.youtubeChannelId === selectedChannelId;
        const avatarUrl = channel.thumbnailUrl ?? (coverVideo ? youtubeThumbnail(coverVideo.youtubeVideoId) : undefined);
        return (
          <button className={selected ? "explore-channel-avatar-button explore-channel-avatar-button-selected" : "explore-channel-avatar-button"} type="button" key={channel.youtubeChannelId} onClick={() => onSelect(selected ? null : channel.youtubeChannelId, channel.targetId)} aria-pressed={selected} aria-label={`${channel.title ?? channel.handle ?? channel.youtubeChannelId} 채널 개요 보기`} title={channel.title ?? channel.handle ?? channel.youtubeChannelId}>
            {avatarUrl ? <img src={avatarUrl} alt="" /> : <span className="explore-avatar">{(channel.title ?? channel.handle ?? "Y").slice(0, 1).toUpperCase()}</span>}
          </button>
        );
      })}
      {channels.length === 0 && emptyMessage && <div className="explore-empty">{emptyMessage}</div>}
    </div>
  );
}

type ChannelDetailsProps = {
  channels: ExploreChannel[];
  videos: CollectedVideo[];
  selectedChannel: ExploreChannel | null;
  subscriberHistory: ChannelSubscriberSnapshot[];
  onSelect: (channelId: string | null, targetId?: string) => void;
};

export function ChannelDetails({ channels, videos, selectedChannel, subscriberHistory, onSelect }: ChannelDetailsProps) {
  return (
    <section className="channel-details-page" aria-label="채널 상세">
      <ChannelStrip channels={channels} videos={videos} selectedChannelId={selectedChannel?.youtubeChannelId ?? null} onSelect={onSelect} />
      {selectedChannel && <ChannelOverview channel={selectedChannel} subscriberHistory={subscriberHistory} />}
    </section>
  );
}

type ExploreSectionProps = {
  data: ExploreData;
  error: string | null;
  loading: boolean;
  loadingMore: boolean;
  visibleVideos: CollectedVideo[];
  selectedChannel: ExploreChannel | null;
  subscriberHistory: ChannelSubscriberSnapshot[];
  query: string;
  scope: CollectedSearchScope;
  submittedQuery: string;
  submittedScope: CollectedSearchScope;
  searchResults: CollectedSearchData | null;
  searchError: string | null;
  searchLoading: boolean;
  loadMoreRef: RefObject<HTMLDivElement | null>;
  onQueryChange: (query: string) => void;
  onScopeChange: (scope: CollectedSearchScope) => void;
  onSubmit: (event: FormEvent<HTMLFormElement>) => void;
  onSelectChannel: (channelId: string | null) => void;
  onOpenVideo: (video: CollectedVideo, trigger: HTMLElement) => void;
  onOpenComment: (commentId: string, trigger: HTMLElement) => void;
};

export function ExploreSection({
  data,
  error,
  loading,
  loadingMore,
  visibleVideos,
  selectedChannel,
  subscriberHistory,
  query,
  scope,
  submittedQuery,
  submittedScope,
  searchResults,
  searchError,
  searchLoading,
  loadMoreRef,
  onQueryChange,
  onScopeChange,
  onSubmit,
  onSelectChannel,
  onOpenVideo,
  onOpenComment,
}: ExploreSectionProps) {
  const hasSearchQuery = submittedQuery.length >= 2;
  const selectedChannelId = selectedChannel?.youtubeChannelId ?? null;
  const scopedVideos = selectedChannelId ? data.videos.filter((video) => video.channelId === selectedChannelId) : data.videos;

  return (
    <section className="explore-section" id="explore" aria-label="Explore" tabIndex={-1}>
      {error && <p className="inline-error" role="status">{error}</p>}
      <form className="explore-search" role="search" onSubmit={onSubmit}>
        <label className="visually-hidden" htmlFor="collected-search-scope">검색 대상</label>
        <select id="collected-search-scope" className="explore-search-scope" value={scope} onChange={(event) => onScopeChange(event.target.value as CollectedSearchScope)}>
          {Object.entries(searchScopeLabels).map(([value, label]) => <option key={value} value={value}>{label}</option>)}
        </select>
        <label className="visually-hidden" htmlFor="collected-search">수집 데이터 통합 검색</label>
        <input id="collected-search" value={query} onChange={(event) => onQueryChange(event.target.value)} placeholder={scope === "videos" ? "영상 제목, 채널 검색" : scope === "comments" ? "댓글 검색" : "영상 제목, 채널, 댓글 검색"} type="search" autoComplete="off" />
        <button className="explore-search-submit" type="submit" aria-label="검색" disabled={query.trim().length < 2 || searchLoading}><MagnifyingGlassIcon aria-hidden="true" /></button>
      </form>
      {searchError && <p className="inline-error" role="status">{searchError}</p>}
      {loading && data.channels.length === 0 ? <p className="explore-loading">수집 라이브러리를 불러오는 중입니다…</p> : hasSearchQuery ? (
        <section className="collected-search-results" aria-live="polite" aria-label="통합 검색 결과">
          {searchLoading && !searchResults ? (
            <div className="search-results-skeleton" aria-busy="true" aria-label="검색 결과를 불러오는 중">
              <div className="search-result-heading"><div><p className="section-kicker">UNIFIED RESULTS</p><h3>“{submittedQuery}” 검색 중</h3></div></div>
              <div className="search-result-columns">
                {(submittedScope === "all" ? ["동영상", "댓글"] : [searchScopeLabels[submittedScope]]).map((title) => <section key={title}><h4>{title}</h4>{Array.from({ length: 5 }, (_, index) => <div className="search-skeleton-item" key={index}><span className="search-skeleton-line search-skeleton-title" /><span className="search-skeleton-line search-skeleton-copy" /><span className="search-skeleton-line search-skeleton-meta" /></div>)}</section>)}
              </div>
            </div>
          ) : <>
            <div className="search-result-heading"><div><p className="section-kicker">UNIFIED RESULTS</p><h3>“{submittedQuery}” 검색 결과</h3></div><span>{formatCount((searchResults?.videos.length ?? 0) + (searchResults?.comments.length ?? 0))}개</span></div>
            <div className="search-result-columns">
              {submittedScope !== "comments" && <section><h4>동영상</h4>{searchResults?.videos.map((result) => <button className="search-video-result" type="button" key={result.video.id} onClick={(event) => onOpenVideo(result.video, event.currentTarget)}><img src={youtubeThumbnail(result.video.youtubeVideoId)} alt="" /><span><strong>{result.video.title}</strong><small>{result.matchedFields.map(searchFieldLabel).join(" · ")} · 유사도 {Math.round(result.score * 100)}%</small></span><ChevronRightIcon aria-hidden="true" /></button>)}{!searchLoading && (searchResults?.videos.length ?? 0) === 0 && <p className="search-empty">일치하는 동영상이 없습니다.</p>}</section>}
              {submittedScope !== "videos" && <section><h4>댓글</h4>{searchResults?.comments.map((result) => <div className="search-comment-result" key={result.comment.id}><CommentRow compact comment={result.comment} context={<><strong>{result.video.title}</strong><small>{result.channelTitle ?? "수집 채널"} · {result.matchedFields.map(searchFieldLabel).join(" · ")} · 유사도 {Math.round(result.score * 100)}%</small></>} onOpenDetail={onOpenComment} /></div>)}{!searchLoading && (searchResults?.comments.length ?? 0) === 0 && <p className="search-empty">일치하는 댓글이 없습니다.</p>}</section>}
            </div>
            {searchLoading && <p className="explore-loading">검색 결과를 갱신하는 중입니다…</p>}
          </>}
        </section>
      ) : <>
        <ChannelStrip channels={data.channels} videos={data.videos} selectedChannelId={selectedChannelId} emptyMessage="아직 수집된 채널이 없습니다. 첫 수집을 시작하면 이곳에 자동으로 모입니다." onSelect={onSelectChannel} />
        {selectedChannel && <ChannelOverview channel={selectedChannel} subscriberHistory={subscriberHistory} />}
        <div className="explore-video-grid" aria-label="수집된 동영상">
          {visibleVideos.map((video, index) => <button className={index === 0 ? "explore-video-card explore-video-card-featured" : "explore-video-card"} type="button" key={video.id} onClick={(event) => onOpenVideo(video, event.currentTarget)}><img src={youtubeThumbnail(video.youtubeVideoId)} alt="" loading={index < 6 ? "eager" : "lazy"} /><span className="explore-video-shade" aria-hidden="true" /><span className="explore-video-date">{formatShortDate(video.publishedAt)}</span><strong>{video.title ?? video.youtubeVideoId}</strong><footer><span>조회 {formatCount(video.viewCount)}</span><span>댓글 {formatCount(video.commentCount)}</span></footer></button>)}
          {scopedVideos.length === 0 && <div className="explore-empty">조건에 맞는 저장 동영상이 없습니다.</div>}
        </div>
        {(scopedVideos.length > visibleVideos.length || data.nextOffset !== undefined || loadingMore) && <div className="explore-load-more" ref={loadMoreRef} aria-live="polite">
          {loadingMore && <span className="explore-load-more-spinner" aria-label="추가 동영상을 불러오는 중" />}
        </div>}
      </>}
    </section>
  );
}
