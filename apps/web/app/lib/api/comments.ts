import type {
  CommentDetailData,
  CommentThreadSort,
  PagedCommentReplies,
  PagedCommentThreads,
} from "./types";
import {
  asNumber,
  asRecord,
  asText,
  firstArray,
  normalizeComment,
  normalizeVideo,
} from "./normalizers";
import { ApiError, request } from "./transport";

export async function getVideoCommentThreads(
  videoId: string,
  sort: CommentThreadSort = "newest",
  cursor?: string,
): Promise<PagedCommentThreads> {
  const query = new URLSearchParams({ limit: "20", sort });
  if (cursor) query.set("cursor", cursor);
  const response = await request<unknown>(`/v1/videos/${encodeURIComponent(videoId)}/comment-threads?${query.toString()}`, {
    method: "GET",
  });
  const record = asRecord(response) ?? {};
  const nextCursor = asText(record?.nextCursor ?? record?.next_cursor ?? record?.nextPageToken ?? record?.next_page_token);
  const responseSort = asText(record.sort);

  return {
    sort: responseSort === "oldest" || responseSort === "recommended" ? responseSort : "newest",
    items: firstArray(record, ["items", "threads", "results"]).flatMap((item) => {
      const itemRecord = asRecord(item);
      const comment = normalizeComment(itemRecord?.comment);
      if (!comment) return [];
      const repliesPreview = firstArray(itemRecord ?? {}, ["repliesPreview", "replies_preview", "replies"]).flatMap((reply) => {
        const normalized = normalizeComment(reply);
        return normalized ? [normalized] : [];
      });
      return [{
        comment,
        repliesPreview,
        storedReplyCount: asNumber(itemRecord?.storedReplyCount ?? itemRecord?.stored_reply_count) ?? repliesPreview.length,
      }];
    }),
    ...(nextCursor ? { nextCursor } : {}),
  };
}

export async function getCommentReplies(commentId: string, cursor?: string): Promise<PagedCommentReplies> {
  const query = new URLSearchParams({ limit: "20" });
  if (cursor) query.set("cursor", cursor);
  const response = await request<unknown>(`/v1/comments/${encodeURIComponent(commentId)}/replies?${query.toString()}`, {
    method: "GET",
  });
  const record = asRecord(response) ?? {};
  const nextCursor = asText(record.nextCursor ?? record.next_cursor);
  return {
    comments: firstArray(record, ["comments", "items", "replies"]).flatMap((comment) => {
      const normalized = normalizeComment(comment);
      return normalized ? [normalized] : [];
    }),
    ...(nextCursor ? { nextCursor } : {}),
  };
}

export async function getCommentDetail(commentId: string): Promise<CommentDetailData> {
  const response = await request<unknown>(`/v1/comments/${encodeURIComponent(commentId)}`, { method: "GET" });
  const record = asRecord(response) ?? {};
  const comment = normalizeComment(record.comment);
  const parentComment = normalizeComment(record.parentComment ?? record.parent_comment);
  const video = normalizeVideo(record.video);
  if (!comment || !video) throw new ApiError("댓글 상세 정보를 해석하지 못했습니다.", 500);

  return {
    comment,
    ...(parentComment ? { parentComment } : {}),
    storedReplyCount: asNumber(record.storedReplyCount ?? record.stored_reply_count) ?? 0,
    video,
    replies: firstArray(record, ["replies", "replyComments", "reply_comments"]).flatMap((item) => {
      const itemRecord = asRecord(item);
      const reply = normalizeComment(itemRecord?.comment ?? item);
      return reply ? [reply] : [];
    }),
    authorComments: firstArray(record, ["authorComments", "author_comments"]).flatMap((item) => {
      const itemRecord = asRecord(item);
      const relatedComment = normalizeComment(itemRecord?.comment);
      const relatedVideo = normalizeVideo(itemRecord?.video);
      if (!relatedComment || !relatedVideo) return [];
      const channelTitle = asText(itemRecord?.channelTitle ?? itemRecord?.channel_title);
      return [{ comment: relatedComment, video: relatedVideo, ...(channelTitle ? { channelTitle } : {}) }];
    }),
  };
}
