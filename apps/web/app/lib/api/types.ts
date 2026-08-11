import type { JobStatus } from "@monitube/contracts";

export interface StartJobRequest {
  include_comments: boolean;
  max_videos?: number;
  max_comments_per_video?: number;
}

export interface SourceSummary {
  id: string;
  type: string;
  enabled: boolean;
  config: Record<string, unknown>;
  nextRunAt?: string;
  targetId?: string;
  canonicalKey?: string;
  coverage?: Record<string, unknown>;
  lastCompletedAt?: string;
  latestJob?: JobStatus;
  storedVideoCount: number;
  storedCommentCount: number;
  reportedCommentCount: number;
}

export interface AuthUser { username: string; }

export type CollectionRequestDisposition = "cached" | "joined" | "queued" | "successor_queued";

export interface CollectionRequestResponse {
  id: string;
  disposition: CollectionRequestDisposition;
  targetId: string;
  source: SourceSummary;
  job?: JobStatus;
}

export interface CollectedVideo {
  id: string;
  youtubeVideoId: string;
  channelId?: string;
  title: string;
  description?: string;
  publishedAt?: string;
  viewCount?: number;
  likeCount?: number;
  commentCount?: number;
  durationSeconds?: number;
  privacyStatus?: string;
  madeForKids?: boolean;
  fetchedAt?: string;
}

export interface TopWord {
  label: string;
  count?: number;
}

export interface CommentSummary {
  total: number;
  latestPublishedAt?: string;
  topWords: TopWord[];
}

export interface SourceAnalysis {
  videoCount?: number;
  commentCount?: number;
  latestVideoPublishedAt?: string;
  latestCommentPublishedAt?: string;
  topWords: TopWord[];
  generatedAt?: string;
  asOfJobId?: string;
  dataVersion?: number;
  status?: "fresh" | "stale" | "building" | "failed";
  topWordsStatus?: "fresh" | "stale" | "building" | "failed";
  partialData?: boolean;
  coverage?: Record<string, unknown>;
}

export type SourceVideoMetric = "views" | "likes" | "comments";
export type SourceTopVideos = Record<SourceVideoMetric, CollectedVideo[]>;

export interface SourceResults {
  source: SourceSummary;
  latestJob?: JobStatus;
  videos: CollectedVideo[];
  commentSummary?: CommentSummary;
  analysis?: SourceAnalysis;
  topVideos?: SourceTopVideos;
  videosNextCursor?: string;
  videosSnapshotAt?: string;
  videosTotal?: number;
  videoPagination?: "cursor" | "legacy";
}

export interface SourceOverview {
  source: SourceSummary;
  latestJob?: JobStatus;
  summary?: SourceAnalysis;
  topVideos: SourceTopVideos;
}

export interface PagedSourceVideos {
  videos: CollectedVideo[];
  nextCursor?: string;
  snapshotAt?: string;
  total: number;
}

export interface ActiveSourceJob {
  sourceId: string;
  targetId?: string;
  job: JobStatus;
}

export interface RecentJobFailure {
  sourceId: string;
  targetId?: string;
  sourceType: string;
  sourceLabel: string;
  failedAt: string;
  reason: string;
  errorCode?: string;
  retryable?: boolean;
  failedChildCount: number;
  job: JobStatus;
}

export interface CollectedComment {
  id: string;
  text: string;
  videoId?: string;
  parentCommentId?: string;
  threadId?: string;
  publishedAt?: string;
  likeCount?: number;
  authorName?: string;
  authorChannelId?: string;
}

export interface CommentThreadItem {
  comment: CollectedComment;
  repliesPreview: CollectedComment[];
  storedReplyCount: number;
}

export type CommentThreadSort = "newest" | "oldest" | "recommended";

export interface PagedCommentThreads {
  sort: CommentThreadSort;
  items: CommentThreadItem[];
  nextCursor?: string;
}

export interface PagedCommentReplies {
  comments: CollectedComment[];
  nextCursor?: string;
}

export interface CommentDetailData {
  comment: CollectedComment;
  parentComment?: CollectedComment;
  storedReplyCount: number;
  video: CollectedVideo;
  replies: CollectedComment[];
  authorComments: Array<{
    comment: CollectedComment;
    video: CollectedVideo;
    channelTitle?: string;
  }>;
}

export interface TargetPin {
  targetId: string;
  enabled: boolean;
  intervalMinutes: number;
  nextRunAt: string;
  lastDispatchedAt?: string;
}

export interface ExploreChannel {
  youtubeChannelId: string;
  handle?: string;
  title?: string;
  description?: string;
  thumbnailUrl?: string;
  subscriberCount?: number;
  viewCount?: number;
  youtubeVideoCount?: number;
  hiddenSubscriberCount?: boolean;
  videoCount: number;
  commentCount: number;
  youtubeCommentCount: number;
  videoCollectionRate: number;
  commentCollectionRate: number;
  lastFetchedAt?: string;
  targetId?: string;
  pin?: TargetPin;
}

export interface ExploreData {
  channels: ExploreChannel[];
  videos: CollectedVideo[];
  nextOffset?: number;
}

export interface ChannelSubscriberSnapshot {
  fetchedAt: string;
  subscriberCount?: number;
  hiddenSubscriberCount?: boolean;
}

export interface CollectedSearchVideo {
  video: CollectedVideo;
  score: number;
  matchedFields: string[];
  transcriptSnippet?: {
    text: string;
    startMs: number;
    durationMs: number;
    matchedTerms: string[];
  };
}

export interface CollectedSearchComment {
  comment: CollectedComment;
  video: CollectedVideo;
  channelTitle?: string;
  score: number;
  matchedFields: string[];
}

export interface CollectedSearchData {
  query: string;
  videos: CollectedSearchVideo[];
  comments: CollectedSearchComment[];
}

export type CollectedSearchScope = "all" | "videos" | "comments";

export type AnalysisScope = "all" | "channel" | "keyword";
export type AnalysisCommentType = "all" | "top_level" | "reply";
export type AnalysisSection = "all" | "core" | "content";

export interface AnalysisTrendPoint {
  period: string;
  count: number;
  topLevelCount: number;
  replyCount: number;
}

export interface AnalysisBreakdownRow {
  id: string;
  kind: "channel" | "keyword";
  label: string;
  videoCount: number;
  viewCount: number;
  likeCount: number;
  youtubeCommentCount: number;
  collectedCommentCount: number;
  topLevelCount: number;
  replyCount: number;
  latestPublishedAt?: string;
}

export interface AnalysisVideo extends CollectedVideo {
  channelTitle?: string;
  youtubeCommentCount: number;
  collectedCommentCount: number;
  topLevelCount: number;
  replyCount: number;
  statisticsFetchedAt?: string;
}

export interface AnalysisComment {
  id: string;
  videoId: string;
  videoTitle?: string;
  channelTitle?: string;
  text?: string;
  authorName?: string;
  publishedAt?: string;
  likeCount: number;
  isReply: boolean;
}

export interface WorkspaceAnalysisSummary {
  videoCount: number;
  totalViewCount: number;
  medianViewCount: number;
  totalLikeCount: number;
  youtubeCommentCount: number;
  collectedCommentCount: number;
  commentedVideoCount: number;
  identifiedAuthorCount: number;
  topLevelCount: number;
  replyCount: number;
  averageCommentLikeCount: number;
  latestVideoPublishedAt?: string;
  latestCommentPublishedAt?: string;
  statisticsFetchedAt?: string;
}

export interface AnalysisCoverage {
  visibleTargetCount: number;
  includedVideoCount: number;
  videosWithStatistics: number;
  sampledComments: number;
  totalComments: number;
  partialData: boolean;
  generatedAt: string;
}

export interface AnalysisCommentSignals {
  replyRate: number;
  authorDiversityRate: number;
  questionRate: number;
  questionCount: number;
  questionSampleSize: number;
}

export interface TfidfKeyword {
  term: string;
  score: number;
  termCount: number;
  documentCount: number;
  documentRate: number;
}

export interface AnalysisKeywordCoverage {
  indexedVideoDocuments: number;
  indexedCommentDocuments: number;
  analyzerVersion: string;
}

export interface AnalysisOverview {
  summary: WorkspaceAnalysisSummary;
  videoTrend: AnalysisTrendPoint[];
  commentTrend: AnalysisTrendPoint[];
  channelBreakdown: AnalysisBreakdownRow[];
  keywordBreakdown: AnalysisBreakdownRow[];
  topVideos: AnalysisVideo[];
  topComments: AnalysisComment[];
  topWords: TopWord[];
  videoKeywords: TfidfKeyword[];
  commentKeywords: TfidfKeyword[];
  keywordCoverage: AnalysisKeywordCoverage;
  commentSignals: AnalysisCommentSignals;
  coverage: AnalysisCoverage;
}

export interface AnalysisQuery {
  scope?: AnalysisScope;
  targetId?: string;
  channelId?: string;
  from?: string;
  to?: string;
  commentType?: AnalysisCommentType;
  section?: AnalysisSection;
  limit?: number;
}

export interface AnalysisPerformanceSummary {
  videoCount: number;
  comparableVideoCount: number;
  snapshotEligible7d: number;
  medianViewsPerDay: number;
  medianLikeRate: number;
  medianCommentRatePerThousand: number;
  totalViewGrowth7d: number;
  collectionCoverageRate?: number;
}

export interface AnalysisInsight {
  id: string;
  kind: "growth" | "breakout" | "opportunity" | "conversation" | "quality";
  tone: "positive" | "attention" | "neutral";
  title: string;
  description: string;
  videoId?: string;
  value?: number;
  unit?: "views" | "multiple" | "percent" | "comments_per_thousand";
}

export interface AnalysisPerformanceVideo {
  id: string;
  channelId?: string;
  channelTitle?: string;
  title?: string;
  publishedAt?: string;
  statisticsFetchedAt?: string;
  viewCount: number;
  likeCount: number;
  youtubeCommentCount: number;
  collectedCommentCount: number;
  ageDays: number;
  viewsPerDay: number;
  likeRate: number;
  commentRatePerThousand: number;
  engagementRate: number;
  collectionCoverageRate?: number;
  viewGrowth7d?: number;
  likeGrowth7d?: number;
  commentGrowth7d?: number;
  growthWindowDays?: number;
  channelMedianViewsPerDay?: number;
  channelMedianEngagementRate?: number;
  channelMedianMultiple?: number;
}

export interface AnalysisPublishingCell {
  weekday: number;
  hourBucket: number;
  videoCount: number;
  medianViewsPerDay: number;
}

export interface AnalysisInsights {
  performanceSummary: AnalysisPerformanceSummary;
  insights: AnalysisInsight[];
  performanceVideos: AnalysisPerformanceVideo[];
  publishingHeatmap: AnalysisPublishingCell[];
  coverage: {
    generatedAt: string;
    videoCount: number;
    comparableVideoCount: number;
    snapshotEligible7d: number;
  };
}
