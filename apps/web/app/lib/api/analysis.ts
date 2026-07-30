import type {
  AnalysisBreakdownRow,
  AnalysisComment,
  AnalysisOverview,
  AnalysisQuery,
  AnalysisTrendPoint,
  AnalysisVideo,
  WorkspaceAnalysisSummary,
} from "./types";
import {
  asArray,
  asBoolean,
  asNumber,
  asRecord,
  asText,
  normalizeTopWords,
  normalizeVideo,
} from "./normalizers";
import { ApiError, request } from "./transport";

function requiredNumber(record: Record<string, unknown>, key: string) {
  return asNumber(record[key]) ?? 0;
}

function normalizeTrend(value: unknown): AnalysisTrendPoint[] {
  return asArray(value).flatMap((item) => {
    const record = asRecord(item);
    const period = asText(record?.period);
    if (!record || !period) return [];
    return [{
      period,
      count: requiredNumber(record, "count"),
      topLevelCount: requiredNumber(record, "topLevelCount"),
      replyCount: requiredNumber(record, "replyCount"),
    }];
  });
}

function normalizeBreakdown(value: unknown): AnalysisBreakdownRow[] {
  return asArray(value).flatMap((item) => {
    const record = asRecord(item);
    const id = asText(record?.id);
    const kind = asText(record?.kind);
    const label = asText(record?.label);
    if (!record || !id || !label || (kind !== "channel" && kind !== "keyword")) return [];
    return [{
      id,
      kind,
      label,
      videoCount: requiredNumber(record, "videoCount"),
      viewCount: requiredNumber(record, "viewCount"),
      likeCount: requiredNumber(record, "likeCount"),
      youtubeCommentCount: requiredNumber(record, "youtubeCommentCount"),
      collectedCommentCount: requiredNumber(record, "collectedCommentCount"),
      topLevelCount: requiredNumber(record, "topLevelCount"),
      replyCount: requiredNumber(record, "replyCount"),
      ...(asText(record.latestPublishedAt) ? { latestPublishedAt: asText(record.latestPublishedAt) } : {}),
    }];
  });
}

function normalizeAnalysisVideo(value: unknown): AnalysisVideo | null {
  const record = asRecord(value);
  const video = normalizeVideo(value);
  if (!record || !video) return null;
  return {
    ...video,
    youtubeVideoId: video.id,
    commentCount: requiredNumber(record, "youtubeCommentCount"),
    youtubeCommentCount: requiredNumber(record, "youtubeCommentCount"),
    collectedCommentCount: requiredNumber(record, "collectedCommentCount"),
    topLevelCount: requiredNumber(record, "topLevelCount"),
    replyCount: requiredNumber(record, "replyCount"),
    ...(asText(record.channelTitle) ? { channelTitle: asText(record.channelTitle) } : {}),
    ...(asText(record.statisticsFetchedAt) ? { statisticsFetchedAt: asText(record.statisticsFetchedAt) } : {}),
  };
}

function normalizeAnalysisComment(value: unknown): AnalysisComment | null {
  const record = asRecord(value);
  const id = asText(record?.id);
  const videoId = asText(record?.videoId);
  if (!record || !id || !videoId) return null;
  return {
    id,
    videoId,
    likeCount: requiredNumber(record, "likeCount"),
    isReply: asBoolean(record.isReply) ?? false,
    ...(asText(record.videoTitle) ? { videoTitle: asText(record.videoTitle) } : {}),
    ...(asText(record.channelTitle) ? { channelTitle: asText(record.channelTitle) } : {}),
    ...(asText(record.text) ? { text: asText(record.text) } : {}),
    ...(asText(record.authorName) ? { authorName: asText(record.authorName) } : {}),
    ...(asText(record.publishedAt) ? { publishedAt: asText(record.publishedAt) } : {}),
  };
}

export async function getAnalysisOverview(query: AnalysisQuery = {}): Promise<AnalysisOverview> {
  const params = new URLSearchParams();
  if (query.scope) params.set("scope", query.scope);
  if (query.targetId) params.append("targetId", query.targetId);
  if (query.channelId) params.append("channelId", query.channelId);
  if (query.from) params.set("from", query.from);
  if (query.to) params.set("to", query.to);
  if (query.commentType) params.set("commentType", query.commentType);
  if (query.section) params.set("section", query.section);
  params.set("limit", String(query.limit ?? 10));

  const response = await request<unknown>(`/v1/analysis/overview?${params.toString()}`, { method: "GET" });
  const record = asRecord(response);
  const rawSummary = asRecord(record?.summary);
  const rawCoverage = asRecord(record?.coverage);
  if (!record || !rawSummary || !rawCoverage) {
    throw new ApiError("분석 결과를 해석하지 못했습니다.", 502);
  }

  const summary: WorkspaceAnalysisSummary = {
    videoCount: requiredNumber(rawSummary, "videoCount"),
    totalViewCount: requiredNumber(rawSummary, "totalViewCount"),
    medianViewCount: requiredNumber(rawSummary, "medianViewCount"),
    totalLikeCount: requiredNumber(rawSummary, "totalLikeCount"),
    youtubeCommentCount: requiredNumber(rawSummary, "youtubeCommentCount"),
    collectedCommentCount: requiredNumber(rawSummary, "collectedCommentCount"),
    commentedVideoCount: requiredNumber(rawSummary, "commentedVideoCount"),
    identifiedAuthorCount: requiredNumber(rawSummary, "identifiedAuthorCount"),
    topLevelCount: requiredNumber(rawSummary, "topLevelCount"),
    replyCount: requiredNumber(rawSummary, "replyCount"),
    averageCommentLikeCount: requiredNumber(rawSummary, "averageCommentLikeCount"),
    ...(asText(rawSummary.latestVideoPublishedAt) ? { latestVideoPublishedAt: asText(rawSummary.latestVideoPublishedAt) } : {}),
    ...(asText(rawSummary.latestCommentPublishedAt) ? { latestCommentPublishedAt: asText(rawSummary.latestCommentPublishedAt) } : {}),
    ...(asText(rawSummary.statisticsFetchedAt) ? { statisticsFetchedAt: asText(rawSummary.statisticsFetchedAt) } : {}),
  };

  const generatedAt = asText(rawCoverage.generatedAt);
  if (!generatedAt) throw new ApiError("분석 생성 시각이 없습니다.", 502);
  return {
    summary,
    videoTrend: normalizeTrend(record.videoTrend),
    commentTrend: normalizeTrend(record.commentTrend),
    channelBreakdown: normalizeBreakdown(record.channelBreakdown),
    keywordBreakdown: normalizeBreakdown(record.keywordBreakdown),
    topVideos: asArray(record.topVideos).flatMap((item) => {
      const video = normalizeAnalysisVideo(item);
      return video ? [video] : [];
    }),
    topComments: asArray(record.topComments).flatMap((item) => {
      const comment = normalizeAnalysisComment(item);
      return comment ? [comment] : [];
    }),
    topWords: normalizeTopWords(record.topWords),
    coverage: {
      visibleTargetCount: requiredNumber(rawCoverage, "visibleTargetCount"),
      includedVideoCount: requiredNumber(rawCoverage, "includedVideoCount"),
      videosWithStatistics: requiredNumber(rawCoverage, "videosWithStatistics"),
      sampledComments: requiredNumber(rawCoverage, "sampledComments"),
      totalComments: requiredNumber(rawCoverage, "totalComments"),
      partialData: asBoolean(rawCoverage.partialData) ?? false,
      generatedAt,
    },
  };
}
