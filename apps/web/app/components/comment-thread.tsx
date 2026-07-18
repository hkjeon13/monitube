"use client";

import {
  ChevronDownIcon,
  ChevronUpIcon,
  HandThumbUpIcon,
} from "@heroicons/react/24/outline";
import type { ReactNode } from "react";
import { useEffect, useMemo, useState } from "react";

import {
  getCommentReplies,
  type CollectedComment,
  type CommentThreadItem,
} from "../lib/api";

function relativeTime(value?: string) {
  if (!value) return "게시 시각 없음";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  const seconds = Math.round((date.getTime() - Date.now()) / 1_000);
  const formatter = new Intl.RelativeTimeFormat("ko-KR", { numeric: "auto" });
  const ranges: Array<[Intl.RelativeTimeFormatUnit, number]> = [
    ["year", 31_536_000],
    ["month", 2_592_000],
    ["week", 604_800],
    ["day", 86_400],
    ["hour", 3_600],
    ["minute", 60],
  ];
  for (const [unit, size] of ranges) {
    if (Math.abs(seconds) >= size) return formatter.format(Math.round(seconds / size), unit);
  }
  return "방금 전";
}

function exactTime(value?: string) {
  if (!value) return "게시 시각이 저장되지 않았습니다.";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  return new Intl.DateTimeFormat("ko-KR", {
    dateStyle: "long",
    timeStyle: "short",
    timeZone: "Asia/Seoul",
  }).format(date);
}

function mergeReplies(current: CollectedComment[], incoming: CollectedComment[]) {
  const byId = new Map(current.map((comment) => [comment.id, comment]));
  incoming.forEach((comment) => byId.set(comment.id, comment));
  return Array.from(byId.values()).sort((left, right) => {
    const leftTime = left.publishedAt ? new Date(left.publishedAt).getTime() : 0;
    const rightTime = right.publishedAt ? new Date(right.publishedAt).getTime() : 0;
    return leftTime - rightTime || left.id.localeCompare(right.id);
  });
}

export function CommentRow({
  comment,
  compact = false,
  context,
  onOpenDetail,
  detailLabel = "댓글 상세 보기",
}: {
  comment: CollectedComment;
  compact?: boolean;
  context?: ReactNode;
  onOpenDetail?: (commentId: string, trigger: HTMLElement) => void;
  detailLabel?: string;
}) {
  const [isBodyExpanded, setIsBodyExpanded] = useState(false);
  const author = comment.authorName?.trim() || "알 수 없는 작성자";
  const initial = Array.from(author)[0]?.toUpperCase() || "?";
  const canCollapse = comment.text.length > (compact ? 120 : 240) || comment.text.split("\n").length > 4;
  const timeLabel = relativeTime(comment.publishedAt);
  const exactTimeLabel = exactTime(comment.publishedAt);

  return (
    <article className={compact ? "yt-comment-row yt-comment-row-compact" : "yt-comment-row"}>
      <div className="yt-comment-avatar" aria-hidden="true">{initial}</div>
      <div className="yt-comment-content">
        {context && <div className="yt-comment-context">{context}</div>}
        <header className="yt-comment-author-line">
          <strong>{author}</strong>
          <time dateTime={comment.publishedAt} title={exactTimeLabel} aria-label={`${timeLabel}, ${exactTimeLabel}`}>{timeLabel}</time>
        </header>
        <p className={!isBodyExpanded && canCollapse ? "yt-comment-body yt-comment-body-collapsed" : "yt-comment-body"}>{comment.text}</p>
        {canCollapse && (
          <button className="yt-comment-text-toggle" type="button" onClick={() => setIsBodyExpanded((value) => !value)}>
            {isBodyExpanded ? "간략히" : "더보기"}
          </button>
        )}
        <div className="yt-comment-actions">
          <span className="yt-comment-like" aria-label={`좋아요 ${comment.likeCount ?? 0}개`}>
            <HandThumbUpIcon aria-hidden="true" />
            {(comment.likeCount ?? 0) > 0 && <span>{new Intl.NumberFormat("ko-KR", { notation: "compact" }).format(comment.likeCount ?? 0)}</span>}
          </span>
          {onOpenDetail && (
            <button className="yt-comment-detail-button" type="button" onClick={(event) => onOpenDetail(comment.id, event.currentTarget)}>
              {detailLabel}
            </button>
          )}
        </div>
      </div>
    </article>
  );
}

export function CommentThread({
  item,
  onOpenDetail,
  selectedCommentId,
}: {
  item: CommentThreadItem;
  onOpenDetail?: (commentId: string, trigger: HTMLElement) => void;
  selectedCommentId?: string;
}) {
  const [expanded, setExpanded] = useState(false);
  const [replies, setReplies] = useState(item.repliesPreview);
  const [nextCursor, setNextCursor] = useState<string | undefined>();
  const [loadedFirstPage, setLoadedFirstPage] = useState(false);
  const [isLoading, setIsLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const repliesId = useMemo(() => `comment-replies-${item.comment.id.replace(/[^A-Za-z0-9_-]/g, "-")}`, [item.comment.id]);

  useEffect(() => {
    setExpanded(false);
    setReplies(item.repliesPreview);
    setNextCursor(undefined);
    setLoadedFirstPage(false);
    setIsLoading(false);
    setError(null);
  }, [item.comment.id, item.repliesPreview]);

  const loadReplyPage = async (cursor?: string) => {
    if (isLoading) return;
    setIsLoading(true);
    setError(null);
    try {
      const response = await getCommentReplies(item.comment.id, cursor);
      setReplies((current) => mergeReplies(current, response.comments));
      setNextCursor(response.nextCursor);
      setLoadedFirstPage(true);
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "답글을 불러오지 못했습니다.");
    } finally {
      setIsLoading(false);
    }
  };

  const showReplies = () => {
    setExpanded(true);
    if (item.storedReplyCount > item.repliesPreview.length && !loadedFirstPage) void loadReplyPage();
  };

  return (
    <section className={selectedCommentId === item.comment.id ? "yt-comment-thread yt-comment-thread-selected" : "yt-comment-thread"}>
      <CommentRow comment={item.comment} onOpenDetail={onOpenDetail} />
      {item.storedReplyCount > 0 && !expanded && (
        <button className="yt-replies-toggle" type="button" aria-expanded="false" aria-controls={repliesId} onClick={showReplies}>
          <ChevronDownIcon aria-hidden="true" /> 답글 {new Intl.NumberFormat("ko-KR").format(item.storedReplyCount)}개 보기
        </button>
      )}
      {expanded && (
        <div className="yt-replies" id={repliesId} aria-busy={isLoading}>
          <div className="yt-replies-line" aria-hidden="true" />
          {replies.map((reply) => (
            <div className={selectedCommentId === reply.id ? "yt-reply yt-reply-selected" : "yt-reply"} key={reply.id}>
              <CommentRow comment={reply} onOpenDetail={onOpenDetail} detailLabel="상세 보기" />
            </div>
          ))}
          {isLoading && <div className="comment-inline-loading" role="status"><span className="loading-spinner" aria-hidden="true" />답글을 불러오는 중</div>}
          {error && <div className="comment-inline-error" role="status"><span>{error}</span><button type="button" onClick={() => void loadReplyPage(nextCursor)}>다시 시도</button></div>}
          {!error && nextCursor && !isLoading && <button className="yt-replies-toggle" type="button" onClick={() => void loadReplyPage(nextCursor)}>답글 더 보기</button>}
          {!isLoading && (
            <button className="yt-replies-toggle" type="button" aria-expanded="true" aria-controls={repliesId} onClick={() => setExpanded(false)}>
              <ChevronUpIcon aria-hidden="true" /> 답글 접기
            </button>
          )}
        </div>
      )}
    </section>
  );
}
