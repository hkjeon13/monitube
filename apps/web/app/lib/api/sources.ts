import type {
  CreateCollectionSourceRequest,
  JobStatus,
} from "@monitube/contracts";
import type {
  ActiveSourceJob,
  CollectionRequestDisposition,
  CollectionRequestResponse,
  RecentJobFailure,
  SourceSummary,
  StartJobRequest,
} from "./types";
import {
  asBoolean,
  asNumber,
  asRecord,
  asText,
  firstArray,
  normalizeJob,
  normalizeSource,
} from "./normalizers";
import { ApiError, request } from "./transport";

export async function createSource(requestBody: CreateCollectionSourceRequest) {
  const response = await request<unknown>("/v1/sources", {
    method: "POST",
    body: JSON.stringify(requestBody),
  });

  const source = normalizeSource(response);
  if (!source) throw new ApiError("수집 source 응답을 해석할 수 없습니다.", 502);
  return source;
}

/**
 * Update the current user's subscription settings for a collection target.
 *
 * A source ID in the browser API is deliberately a user-scoped subscription
 * ID.  Toggling it must not change another user's shared collection target or
 * its worker schedule.
 */
export async function updateSource(
  sourceId: string,
  payload: { enabled?: boolean },
): Promise<SourceSummary> {
  const response = await request<unknown>(`/v1/sources/${encodeURIComponent(sourceId)}`, {
    method: "PATCH",
    body: JSON.stringify(payload),
  });
  const source = normalizeSource(response);
  if (!source) throw new ApiError("수집 대상 업데이트 응답을 해석하지 못했습니다.", 502);
  return source;
}

export async function startJob(sourceId: string, requestBody: StartJobRequest) {
  return request<JobStatus>(
    `/v1/sources/${encodeURIComponent(sourceId)}/jobs`,
    {
      method: "POST",
      body: JSON.stringify(requestBody),
    },
  );
}

export async function createCollectionRequest(
  requestBody: CreateCollectionSourceRequest,
  options: { forceRefresh?: boolean; idempotencyKey?: string } = {},
): Promise<CollectionRequestResponse> {
  const response = await request<unknown>("/v1/collection-requests", {
    method: "POST",
    headers: options.idempotencyKey ? { "Idempotency-Key": options.idempotencyKey } : undefined,
    body: JSON.stringify({
      ...requestBody,
      ...(options.forceRefresh ? { forceRefresh: true } : {}),
    }),
  });
  const record = asRecord(response);
  const source = normalizeSource(record?.source);
  const id = asText(record?.id);
  const targetId = asText(record?.targetId ?? record?.target_id);
  const disposition = asText(record?.disposition);

  if (!record || !id || !targetId || !source || !disposition) {
    throw new ApiError("수집 요청 응답을 해석할 수 없습니다.", 502);
  }
  if (!["cached", "joined", "queued", "successor_queued"].includes(disposition)) {
    throw new ApiError("알 수 없는 수집 요청 상태입니다.", 502);
  }

  return {
    id,
    targetId,
    disposition: disposition as CollectionRequestDisposition,
    source,
    ...(normalizeJob(record.job) ? { job: normalizeJob(record.job) } : {}),
  };
}

export async function getJob(jobId: string, signal?: AbortSignal): Promise<JobStatus> {
  const response = await request<unknown>(`/v1/jobs/${encodeURIComponent(jobId)}`, { method: "GET", signal });
  const job = normalizeJob(response);
  if (!job) throw new ApiError("작업 상태 응답을 해석할 수 없습니다.", 502);
  return job;
}

export async function listActiveJobs(signal?: AbortSignal): Promise<ActiveSourceJob[]> {
  const response = await request<unknown>("/v1/jobs/active", { method: "GET", signal });
  const record = asRecord(response);
  return firstArray(record ?? {}, ["jobs", "items", "data"]).flatMap((item) => {
    const itemRecord = asRecord(item);
    const sourceId = asText(itemRecord?.sourceId ?? itemRecord?.source_id);
    const job = normalizeJob(itemRecord?.job ?? item);
    if (!sourceId || !job) return [];
    const targetId = asText(itemRecord?.targetId ?? itemRecord?.target_id);
    return [{ sourceId, ...(targetId ? { targetId } : {}), job }];
  });
}

export async function listRecentJobFailures(limit = 10, signal?: AbortSignal): Promise<RecentJobFailure[]> {
  const safeLimit = Number.isFinite(limit) ? Math.max(1, Math.min(50, Math.floor(limit))) : 10;
  const response = await request<unknown>(`/v1/jobs/recent-failures?limit=${safeLimit}`, { method: "GET", signal });
  const record = asRecord(response);
  const values = Array.isArray(response) ? response : firstArray(record ?? {}, ["failures", "items", "data"]);
  return values.flatMap((item) => {
    const itemRecord = asRecord(item);
    const sourceId = asText(itemRecord?.sourceId ?? itemRecord?.source_id);
    const failedAt = asText(itemRecord?.failedAt ?? itemRecord?.failed_at);
    const reason = asText(itemRecord?.reason);
    const retryable = asBoolean(itemRecord?.retryable);
    const failedChildCount = asNumber(itemRecord?.failedChildCount ?? itemRecord?.failed_child_count);
    const job = normalizeJob(itemRecord?.job);
    if (!sourceId || !failedAt || !reason || failedChildCount === undefined || !job) return [];
    const targetId = asText(itemRecord?.targetId ?? itemRecord?.target_id);
    const errorCode = asText(itemRecord?.errorCode ?? itemRecord?.error_code);
    return [{
      sourceId,
      ...(targetId ? { targetId } : {}),
      sourceType: asText(itemRecord?.sourceType ?? itemRecord?.source_type) ?? "unknown",
      sourceLabel: asText(itemRecord?.sourceLabel ?? itemRecord?.source_label) ?? sourceId,
      failedAt,
      reason,
      ...(errorCode ? { errorCode } : {}),
      ...(retryable === undefined ? {} : { retryable }),
      failedChildCount: Math.max(0, Math.floor(failedChildCount)),
      job,
    }];
  });
}

export async function listSourceJobs(sourceId: string): Promise<JobStatus[]> {
  const response = await request<unknown>(`/v1/sources/${encodeURIComponent(sourceId)}/jobs`, { method: "GET" });
  return (Array.isArray(response) ? response : []).flatMap((job) => {
    const normalized = normalizeJob(job);
    return normalized ? [normalized] : [];
  });
}

export async function listSources() {
  const response = await request<unknown>("/v1/sources", { method: "GET" });
  const record = asRecord(response);
  const sourceValues = Array.isArray(response)
    ? response
    : firstArray(record ?? {}, ["sources", "items", "data"]);
  return sourceValues.flatMap((source) => {
    const normalized = normalizeSource(source);
    return normalized ? [normalized] : [];
  });
}

export async function deleteSource(sourceId: string): Promise<void> {
  await request<void>(`/v1/sources/${encodeURIComponent(sourceId)}`, { method: "DELETE" });
}
