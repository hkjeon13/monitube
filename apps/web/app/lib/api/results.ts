import type {
  PagedSourceVideos,
  SourceOverview,
  SourceResults,
} from "./types";
import {
  asNumber,
  asRecord,
  asText,
  firstArray,
  normalizeAnalysis,
  normalizeCommentSummary,
  normalizeJob,
  normalizeSource,
  normalizeTopVideos,
  normalizeVideo,
} from "./normalizers";
import { ApiError, request } from "./transport";

export async function getSourceResults(sourceId: string, signal?: AbortSignal): Promise<SourceResults> {
  const response = await request<unknown>(`/v1/sources/${encodeURIComponent(sourceId)}/results`, {
    method: "GET",
    signal,
  });
  const record = asRecord(response) ?? {};
  const source = normalizeSource(record.source) ?? {
    id: sourceId,
    type: "unknown",
    enabled: true,
    config: {},
  };
  const videoValues = firstArray(record, ["videos", "items", "results"]);

  const directSummary = normalizeCommentSummary(record.commentSummary ?? record.comment_summary);
  const analysis = asRecord(record.analysis);
  const analysisSummary = normalizeAnalysis(analysis);
  const analysisTopWords = analysisSummary?.topWords ?? [];
  const commentSummary = directSummary
    ? {
        ...directSummary,
        topWords: directSummary.topWords.length > 0 ? directSummary.topWords : analysisTopWords,
      }
    : analysis
      ? {
          total: asNumber(analysis.commentCount ?? analysis.comment_count) ?? 0,
          ...(asText(analysis.latestCommentPublishedAt ?? analysis.latest_comment_published_at)
            ? { latestPublishedAt: asText(analysis.latestCommentPublishedAt ?? analysis.latest_comment_published_at) }
            : {}),
          topWords: analysisTopWords,
        }
      : undefined;

  return {
    source,
    ...(normalizeJob(record.latestJob ?? record.latest_job)
      ? { latestJob: normalizeJob(record.latestJob ?? record.latest_job) }
      : {}),
    videos: videoValues.flatMap((video) => {
      const normalized = normalizeVideo(video);
      return normalized ? [normalized] : [];
    }),
    ...(commentSummary ? { commentSummary } : {}),
    ...(analysisSummary ? { analysis: analysisSummary } : {}),
    videoPagination: "legacy",
  };
}

export async function getSourceOverview(sourceId: string, signal?: AbortSignal): Promise<SourceOverview> {
  const response = await request<unknown>(`/v1/sources/${encodeURIComponent(sourceId)}/overview`, {
    method: "GET",
    signal,
  });
  const record = asRecord(response) ?? {};
  const source = normalizeSource(record.source);
  if (!source) throw new ApiError("수집 대상 개요를 해석할 수 없습니다.", 502);
  const latestJob = normalizeJob(record.latestJob ?? record.latest_job);
  const summary = normalizeAnalysis(record.summary ?? record.analysis);
  return {
    source,
    ...(latestJob ? { latestJob } : {}),
    ...(summary ? { summary } : {}),
    topVideos: normalizeTopVideos(record.topVideos ?? record.top_videos),
  };
}

export async function getSourceVideos(
  sourceId: string,
  options: { cursor?: string; limit?: number; signal?: AbortSignal } = {},
): Promise<PagedSourceVideos> {
  const query = new URLSearchParams({ limit: String(Math.min(100, Math.max(1, options.limit ?? 60))) });
  if (options.cursor) query.set("cursor", options.cursor);
  const response = await request<unknown>(
    `/v1/sources/${encodeURIComponent(sourceId)}/videos?${query.toString()}`,
    { method: "GET", signal: options.signal },
  );
  const record = asRecord(response) ?? {};
  const videos = firstArray(record, ["videos", "items", "results"]).flatMap((video) => {
    const normalized = normalizeVideo(video);
    return normalized ? [normalized] : [];
  });
  const nextCursor = asText(record.nextCursor ?? record.next_cursor);
  const snapshotAt = asText(record.snapshotAt ?? record.snapshot_at);
  return {
    videos,
    ...(nextCursor ? { nextCursor } : {}),
    ...(snapshotAt ? { snapshotAt } : {}),
    total: asNumber(record.total ?? record.totalCount ?? record.total_count) ?? videos.length,
  };
}

function isUnavailableAdditiveEndpoint(error: unknown) {
  return error instanceof ApiError && [404, 405, 501].includes(error.status);
}

/**
 * Load the bounded source workspace when the additive API is available. During
 * rolling deployments an older API may not expose it yet, so only unsupported
 * endpoint responses fall back to the legacy, unbounded results contract.
 */
export async function getSourceWorkspace(sourceId: string, signal?: AbortSignal): Promise<SourceResults> {
  try {
    const [overview, page] = await Promise.all([
      getSourceOverview(sourceId, signal),
      getSourceVideos(sourceId, { signal }),
    ]);
    const commentSummary = overview.summary
      ? {
          total: overview.summary.commentCount ?? 0,
          ...(overview.summary.latestCommentPublishedAt
            ? { latestPublishedAt: overview.summary.latestCommentPublishedAt }
            : {}),
          topWords: overview.summary.topWords,
        }
      : undefined;
    return {
      source: overview.source,
      ...(overview.latestJob ? { latestJob: overview.latestJob } : {}),
      videos: page.videos,
      ...(commentSummary ? { commentSummary } : {}),
      ...(overview.summary ? { analysis: overview.summary } : {}),
      topVideos: overview.topVideos,
      ...(page.nextCursor ? { videosNextCursor: page.nextCursor } : {}),
      ...(page.snapshotAt ? { videosSnapshotAt: page.snapshotAt } : {}),
      videosTotal: page.total,
      videoPagination: "cursor",
    };
  } catch (error) {
    if (!isUnavailableAdditiveEndpoint(error)) throw error;
    return getSourceResults(sourceId, signal);
  }
}
