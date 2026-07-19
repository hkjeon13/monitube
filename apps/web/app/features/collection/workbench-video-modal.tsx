import { ArrowLeftIcon, ArrowPathIcon, ChevronRightIcon, XMarkIcon } from "@heroicons/react/24/outline";
import type { RefObject } from "react";

import { CommentRow, CommentThread } from "../../components/comment-thread";
import type {
  CommentDetailData,
  CommentThreadItem,
  CommentThreadSort,
  CollectedVideo,
} from "../../lib/api";
import {
  commentSortLabels,
  formatCount,
  formatDate,
  formatDuration,
  youtubeThumbnail,
} from "./workbench-model";

type VideoModalProps = {
  modalRef: RefObject<HTMLElement | null>;
  selectedVideo: CollectedVideo | null;
  selectedCommentId: string | null;
  commentDetail: CommentDetailData | null;
  isCommentDetailLoading: boolean;
  commentDetailError: string | null;
  commentThreads: CommentThreadItem[];
  commentSort: CommentThreadSort;
  nextCommentsCursor?: string;
  isCommentsLoading: boolean;
  commentsError: string | null;
  loadMoreRef: RefObject<HTMLDivElement | null>;
  onCloseVideo: () => void;
  onCloseComment: () => void;
  onOpenVideo: (video: CollectedVideo, trigger: HTMLElement) => void;
  onOpenComment: (commentId: string, trigger: HTMLElement) => void;
  onSortChange: (sort: CommentThreadSort) => void;
  onLoadComments: (video: CollectedVideo, cursor?: string) => void;
};

export function VideoModal({
  modalRef,
  selectedVideo,
  selectedCommentId,
  commentDetail,
  isCommentDetailLoading,
  commentDetailError,
  commentThreads,
  commentSort,
  nextCommentsCursor,
  isCommentsLoading,
  commentsError,
  loadMoreRef,
  onCloseVideo,
  onCloseComment,
  onOpenVideo,
  onOpenComment,
  onSortChange,
  onLoadComments,
}: VideoModalProps) {
  const activeVideo = selectedVideo ?? commentDetail?.video ?? null;
  const selectedThread = commentDetail
    ? {
        comment: commentDetail.parentComment ?? commentDetail.comment,
        repliesPreview: commentDetail.replies,
        storedReplyCount: commentDetail.storedReplyCount,
      }
    : null;
  const close = selectedVideo ? onCloseVideo : onCloseComment;

  return (
    <div className="video-modal-layer">
      <div className="video-modal-backdrop" aria-hidden="true" onClick={close} />
      <section ref={modalRef} className="video-modal" role="dialog" aria-modal="true" aria-labelledby={selectedCommentId ? "comment-detail-title" : "video-modal-title"} tabIndex={-1}>
        <div className="video-modal-toolbar">
          {selectedCommentId && selectedVideo ? (
            <button className="icon-button" type="button" aria-label="영상 댓글로 돌아가기" data-drawer-initial-focus onClick={onCloseComment}><ArrowLeftIcon aria-hidden="true" /></button>
          ) : <span />}
          <button className="icon-button" type="button" aria-label="상세 팝업 닫기" data-drawer-initial-focus={!selectedCommentId || !selectedVideo ? true : undefined} onClick={close}><XMarkIcon aria-hidden="true" /></button>
        </div>
        <div className="video-modal-scroll">
          {selectedCommentId ? (
            <div className="comment-detail-view">
              <header className="comment-detail-header">
                <p className="section-kicker">COMMENT DETAIL</p>
                <h2 id="comment-detail-title">댓글 스레드</h2>
                {activeVideo && <p>{activeVideo.title}</p>}
              </header>
              {isCommentDetailLoading && <div className="comments-loading" role="status"><span className="loading-spinner" aria-hidden="true" />댓글 상세 정보를 불러오는 중입니다.</div>}
              {commentDetailError && <div className="inline-error" role="status">{commentDetailError}</div>}
              {commentDetail && selectedThread && <>
                <section className="comment-detail-thread" aria-label="선택한 댓글 스레드">
                  <CommentThread item={selectedThread} onOpenDetail={onOpenComment} selectedCommentId={selectedCommentId} />
                </section>
                <section className="comment-detail-video-section">
                  <p className="section-kicker">ON VIDEO</p>
                  <button className="comment-detail-video" type="button" onClick={(event) => { onCloseComment(); onOpenVideo(commentDetail.video, event.currentTarget); }}>
                    <img src={youtubeThumbnail(commentDetail.video.youtubeVideoId)} alt="" />
                    <span><strong>{commentDetail.video.title}</strong><small>{commentDetail.video.youtubeVideoId}</small></span>
                    <ChevronRightIcon aria-hidden="true" />
                  </button>
                </section>
                <section className="comment-author-comments" aria-labelledby="author-comments-title">
                  <div><p className="section-kicker">SAME AUTHOR</p><h3 id="author-comments-title">이 작성자의 다른 댓글</h3></div>
                  {!commentDetail.comment.authorChannelId && <p className="comments-loading">저장된 작성자 식별 정보가 없어 다른 댓글을 연결할 수 없습니다.</p>}
                  {commentDetail.comment.authorChannelId && commentDetail.authorComments.length === 0 && <p className="comments-loading">같은 작성자의 다른 저장 댓글이 아직 없습니다.</p>}
                  {commentDetail.authorComments.length > 0 && <div className="comment-author-list">{commentDetail.authorComments.map((item) => <div className="comment-author-result" key={item.comment.id}><CommentRow compact comment={item.comment} context={<strong>{item.video.title}</strong>} onOpenDetail={onOpenComment} /></div>)}</div>}
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
                      <select aria-label="댓글 정렬" value={commentSort} onChange={(event) => onSortChange(event.target.value as CommentThreadSort)}>
                        {(Object.entries(commentSortLabels) as Array<[CommentThreadSort, string]>).map(([value, label]) => <option value={value} key={value}>{label}</option>)}
                      </select>
                    </label>
                    <button className="icon-button" type="button" aria-label="댓글 새로고침" disabled={isCommentsLoading} onClick={() => onLoadComments(selectedVideo)}><ArrowPathIcon aria-hidden="true" /></button>
                  </div>
                </div>
                {commentsError && <div className="comment-load-error" role="status"><span>{commentsError}</span><button type="button" onClick={() => onLoadComments(selectedVideo)}>다시 시도</button></div>}
                {isCommentsLoading && commentThreads.length === 0 && <div className="comments-loading" role="status"><span className="loading-spinner" aria-hidden="true" />공개 댓글을 불러오는 중입니다.</div>}
                {!isCommentsLoading && !commentsError && commentThreads.length === 0 && <p className="comments-loading">저장된 공개 댓글이 없거나 댓글 수집이 선택되지 않았습니다.</p>}
                {commentThreads.length > 0 && <div className="yt-comment-list">{commentThreads.map((item) => <CommentThread item={item} onOpenDetail={onOpenComment} key={item.comment.id} />)}</div>}
                <div className="comment-load-sentinel" ref={loadMoreRef} aria-hidden="true" />
                {isCommentsLoading && commentThreads.length > 0 && <div className="comment-list-loading" role="status"><span className="loading-spinner" aria-hidden="true" />댓글을 더 불러오는 중</div>}
                {nextCommentsCursor && !isCommentsLoading && <button className="comments-load-more" type="button" onClick={() => onLoadComments(selectedVideo, nextCommentsCursor)}>댓글 더 보기</button>}
              </section>
            </>
          ) : null}
        </div>
      </section>
    </div>
  );
}
