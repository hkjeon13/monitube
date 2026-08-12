import type {
  AnalysisBreakdownRow,
  AnalysisComment,
  AnalysisInsight,
  AnalysisInsights,
  AnalysisOverview,
  AnalysisPerformanceVideo,
  AnalysisPublishingCell,
  AnalysisQuery,
  AnalysisTrendPoint,
  AnalysisVideo,
  WorkspaceAnalysisSummary,
  FrequencyKeyword,
  AnalysisExcludedTerms,
  AnalysisKeywordCorpus,
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

function normalizeFrequencyKeywords(value: unknown): FrequencyKeyword[] {
  return asArray(value).flatMap((item) => {
    const record = asRecord(item);
    const term = asText(record?.term);
    if (!record || !term) return [];
    return [{
      term,
      termCount: requiredNumber(record, "termCount"),
      documentCount: requiredNumber(record, "documentCount"),
      documentRate: requiredNumber(record, "documentRate"),
    }];
  });
}

function normalizeExcludedTerms(value: unknown): AnalysisExcludedTerms {
  const record = asRecord(value);
  if (!record) throw new ApiError("제외 키워드 목록을 해석하지 못했습니다.", 502);
  return {
    videoTerms: asArray(record.videoTerms).flatMap((term) => {
      const normalized = asText(term);
      return normalized ? [normalized] : [];
    }),
    commentTerms: asArray(record.commentTerms).flatMap((term) => {
      const normalized = asText(term);
      return normalized ? [normalized] : [];
    }),
  };
}

export async function getAnalysisExcludedTerms(): Promise<AnalysisExcludedTerms> {
  return normalizeExcludedTerms(await request<unknown>("/v1/analysis/excluded-terms", { method: "GET" }));
}

export async function updateAnalysisExcludedTerms(
  corpus: AnalysisKeywordCorpus,
  terms: string[],
): Promise<AnalysisExcludedTerms> {
  return normalizeExcludedTerms(await request<unknown>(`/v1/analysis/excluded-terms/${corpus}`, {
    method: "PUT",
    body: JSON.stringify({ terms }),
  }));
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
  const rawKeywordCoverage = asRecord(record?.keywordCoverage);
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
    videoKeywords: normalizeFrequencyKeywords(record.videoKeywords),
    commentKeywords: normalizeFrequencyKeywords(record.commentKeywords),
    keywordCoverage: {
      indexedVideoDocuments: requiredNumber(rawKeywordCoverage ?? {}, "indexedVideoDocuments"),
      indexedCommentDocuments: requiredNumber(rawKeywordCoverage ?? {}, "indexedCommentDocuments"),
      analyzerVersion: asText(rawKeywordCoverage?.analyzerVersion) ?? "mecab-nltk-v1",
    },
    commentSignals: {
      replyRate: requiredNumber(
        asRecord(record.commentSignals) ?? {},
        "replyRate",
      ),
      authorDiversityRate: requiredNumber(
        asRecord(record.commentSignals) ?? {},
        "authorDiversityRate",
      ),
      questionRate: requiredNumber(
        asRecord(record.commentSignals) ?? {},
        "questionRate",
      ),
      questionCount: requiredNumber(
        asRecord(record.commentSignals) ?? {},
        "questionCount",
      ),
      questionSampleSize: requiredNumber(
        asRecord(record.commentSignals) ?? {},
        "questionSampleSize",
      ),
    },
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

export async function getAnalysisInsights(query: AnalysisQuery = {}): Promise<AnalysisInsights> {
  const params = new URLSearchParams();
  if (query.scope) params.set("scope", query.scope);
  if (query.targetId) params.append("targetId", query.targetId);
  if (query.channelId) params.append("channelId", query.channelId);
  if (query.from) params.set("from", query.from);
  if (query.to) params.set("to", query.to);
  params.set("limit", String(query.limit ?? 20));

  const response = await request<unknown>(`/v1/analysis/insights?${params.toString()}`, { method: "GET" });
  const record = asRecord(response);
  const rawSummary = asRecord(record?.performanceSummary);
  const rawCoverage = asRecord(record?.coverage);
  const generatedAt = asText(rawCoverage?.generatedAt);
  if (!record || !rawSummary || !rawCoverage || !generatedAt) {
    throw new ApiError("성과 인사이트를 해석하지 못했습니다.", 502);
  }

  const insights: AnalysisInsight[] = asArray(record.insights).flatMap((item) => {
    const value = asRecord(item);
    const id = asText(value?.id);
    const kind = asText(value?.kind);
    const tone = asText(value?.tone);
    const title = asText(value?.title);
    const description = asText(value?.description);
    if (
      !value
      || !id
      || !title
      || !description
      || !["growth", "breakout", "opportunity", "conversation", "quality"].includes(kind ?? "")
      || !["positive", "attention", "neutral"].includes(tone ?? "")
    ) return [];
    return [{
      id,
      kind: kind as AnalysisInsight["kind"],
      tone: tone as AnalysisInsight["tone"],
      title,
      description,
      ...(asText(value.videoId) ? { videoId: asText(value.videoId) } : {}),
      ...(asNumber(value.value) !== undefined ? { value: asNumber(value.value) } : {}),
      ...(asText(value.unit) ? { unit: asText(value.unit) as AnalysisInsight["unit"] } : {}),
    }];
  });

  const performanceVideos: AnalysisPerformanceVideo[] = asArray(record.performanceVideos).flatMap((item) => {
    const value = asRecord(item);
    const id = asText(value?.id);
    if (!value || !id) return [];
    const optionalNumber = (key: string) => asNumber(value[key]);
    return [{
      id,
      viewCount: requiredNumber(value, "viewCount"),
      likeCount: requiredNumber(value, "likeCount"),
      youtubeCommentCount: requiredNumber(value, "youtubeCommentCount"),
      collectedCommentCount: requiredNumber(value, "collectedCommentCount"),
      ageDays: requiredNumber(value, "ageDays"),
      viewsPerDay: requiredNumber(value, "viewsPerDay"),
      likeRate: requiredNumber(value, "likeRate"),
      commentRatePerThousand: requiredNumber(value, "commentRatePerThousand"),
      engagementRate: requiredNumber(value, "engagementRate"),
      ...(asText(value.channelId) ? { channelId: asText(value.channelId) } : {}),
      ...(asText(value.channelTitle) ? { channelTitle: asText(value.channelTitle) } : {}),
      ...(asText(value.title) ? { title: asText(value.title) } : {}),
      ...(asText(value.publishedAt) ? { publishedAt: asText(value.publishedAt) } : {}),
      ...(asText(value.statisticsFetchedAt) ? { statisticsFetchedAt: asText(value.statisticsFetchedAt) } : {}),
      ...(optionalNumber("collectionCoverageRate") !== undefined ? { collectionCoverageRate: optionalNumber("collectionCoverageRate") } : {}),
      ...(optionalNumber("viewGrowth7d") !== undefined ? { viewGrowth7d: optionalNumber("viewGrowth7d") } : {}),
      ...(optionalNumber("likeGrowth7d") !== undefined ? { likeGrowth7d: optionalNumber("likeGrowth7d") } : {}),
      ...(optionalNumber("commentGrowth7d") !== undefined ? { commentGrowth7d: optionalNumber("commentGrowth7d") } : {}),
      ...(optionalNumber("growthWindowDays") !== undefined ? { growthWindowDays: optionalNumber("growthWindowDays") } : {}),
      ...(optionalNumber("channelMedianViewsPerDay") !== undefined ? { channelMedianViewsPerDay: optionalNumber("channelMedianViewsPerDay") } : {}),
      ...(optionalNumber("channelMedianEngagementRate") !== undefined ? { channelMedianEngagementRate: optionalNumber("channelMedianEngagementRate") } : {}),
      ...(optionalNumber("channelMedianMultiple") !== undefined ? { channelMedianMultiple: optionalNumber("channelMedianMultiple") } : {}),
    }];
  });

  const publishingHeatmap: AnalysisPublishingCell[] = asArray(record.publishingHeatmap).flatMap((item) => {
    const value = asRecord(item);
    if (!value) return [];
    return [{
      weekday: requiredNumber(value, "weekday"),
      hourBucket: requiredNumber(value, "hourBucket"),
      videoCount: requiredNumber(value, "videoCount"),
      medianViewsPerDay: requiredNumber(value, "medianViewsPerDay"),
    }];
  });

  return {
    performanceSummary: {
      videoCount: requiredNumber(rawSummary, "videoCount"),
      comparableVideoCount: requiredNumber(rawSummary, "comparableVideoCount"),
      snapshotEligible7d: requiredNumber(rawSummary, "snapshotEligible7d"),
      medianViewsPerDay: requiredNumber(rawSummary, "medianViewsPerDay"),
      medianLikeRate: requiredNumber(rawSummary, "medianLikeRate"),
      medianCommentRatePerThousand: requiredNumber(rawSummary, "medianCommentRatePerThousand"),
      totalViewGrowth7d: requiredNumber(rawSummary, "totalViewGrowth7d"),
      ...(asNumber(rawSummary.collectionCoverageRate) !== undefined
        ? { collectionCoverageRate: asNumber(rawSummary.collectionCoverageRate) }
        : {}),
    },
    insights,
    performanceVideos,
    publishingHeatmap,
    coverage: {
      generatedAt,
      videoCount: requiredNumber(rawCoverage, "videoCount"),
      comparableVideoCount: requiredNumber(rawCoverage, "comparableVideoCount"),
      snapshotEligible7d: requiredNumber(rawCoverage, "snapshotEligible7d"),
    },
  };
}
