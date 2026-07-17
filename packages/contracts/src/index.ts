export const jobStates = [
  "queued",
  "running",
  "waiting_retry",
  "waiting_quota",
  "completed",
  "completed_with_warnings",
  "failed",
  "cancelled",
] as const;

export type JobState = (typeof jobStates)[number];

export const collectionSourceTypes = ["channel", "keyword", "video"] as const;
export type CollectionSourceType = (typeof collectionSourceTypes)[number];

export type QuotaBucket = "search_queries" | "core";

export interface ChannelSourceConfig {
  input: string;
  includeComments: boolean;
  /** Collect every currently discoverable upload, instead of a numeric cap. */
  collectAllVideos?: boolean;
  /** Fetch every available page of public comments for each selected video. */
  collectAllComments?: boolean;
  maxVideos?: number;
  maxCommentPagesPerVideo?: number;
}

export interface KeywordSourceConfig {
  query: string;
  publishedAfter?: string;
  publishedBefore?: string;
  regionCode?: string;
  relevanceLanguage?: string;
  order: "date" | "relevance" | "viewCount";
  /** Legacy compatibility only; keyword collection continues until pagination ends. */
  maxPagesPerRun?: number;
  includeComments: boolean;
  collectAllComments?: boolean;
  maxCommentPagesPerVideo?: number;
}

export interface VideoSourceConfig {
  /** YouTube watch URL, youtu.be URL, or 11-character video ID. */
  input: string;
  includeComments: boolean;
  collectAllComments?: boolean;
  maxCommentPagesPerVideo?: number;
}

export interface CreateCollectionSourceRequest {
  type: CollectionSourceType;
  config: ChannelSourceConfig | KeywordSourceConfig | VideoSourceConfig;
}

export interface QuotaEstimate {
  bucket: QuotaBucket;
  estimatedCalls: number;
  estimatedUnits: number;
  limit: number;
  resetAt: string;
}

export interface PartialError {
  scope: "channel" | "video" | "comment" | "source";
  sourceId: string;
  code: string;
  retryable: boolean;
  message?: string;
}

export interface JobProgress {
  completed: number;
  total?: number;
  unit: "sources" | "pages" | "videos" | "comments";
}

export interface JobStatus {
  id: string;
  state: JobState;
  currentStage: string;
  progress: JobProgress;
  videoProgress?: JobProgress;
  commentProgress?: JobProgress;
  pauseReason?: string;
  quotaBucket?: QuotaBucket;
  resumeAt?: string;
  resumeIsAutomatic: boolean;
  partialErrors: PartialError[];
}

export interface CollectionSource {
  id: string;
  type: CollectionSourceType;
  enabled: boolean;
  config: ChannelSourceConfig | KeywordSourceConfig | VideoSourceConfig;
  nextRunAt?: string;
  latestJob?: JobStatus;
}
