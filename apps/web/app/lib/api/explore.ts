import type {
  ChannelSubscriberSnapshot,
  CollectedSearchData,
  CollectedSearchScope,
  ExploreData,
  TargetPin,
} from "./types";
import {
  asBoolean,
  asNumber,
  asRecord,
  asText,
  asTextArray,
  firstArray,
  normalizeComment,
  normalizeExploreChannel,
  normalizePin,
  normalizeVideo,
} from "./normalizers";
import { ApiError, request } from "./transport";

export async function getExplore(channelId?: string, offset = 0, limit = 60): Promise<ExploreData> {
  const query = new URLSearchParams({ offset: String(offset), limit: String(limit) });
  if (channelId) query.set("channelId", channelId);
  const response = await request<unknown>(`/v1/explore?${query.toString()}`, { method: "GET" });
  const record = asRecord(response);
  return {
    channels: firstArray(record ?? {}, ["channels"]).flatMap((item) => {
      const channel = normalizeExploreChannel(item);
      return channel ? [channel] : [];
    }),
    videos: firstArray(record ?? {}, ["videos"]).flatMap((item) => {
      const video = normalizeVideo(item);
      return video ? [video] : [];
    }),
    ...(asNumber(record?.nextOffset ?? record?.next_offset) !== undefined ? { nextOffset: asNumber(record?.nextOffset ?? record?.next_offset) } : {}),
  };
}

export async function getChannelSubscriberHistory(channelId: string): Promise<ChannelSubscriberSnapshot[]> {
  const response = await request<unknown>(`/v1/channels/${encodeURIComponent(channelId)}/subscriber-history`, { method: "GET" });
  return (Array.isArray(response) ? response : []).flatMap((item) => {
    const record = asRecord(item);
    const fetchedAt = asText(record?.fetchedAt ?? record?.fetched_at);
    if (!fetchedAt) return [];
    return [{
      fetchedAt,
      ...(asNumber(record?.subscriberCount ?? record?.subscriber_count) !== undefined ? { subscriberCount: asNumber(record?.subscriberCount ?? record?.subscriber_count) } : {}),
      ...(asBoolean(record?.hiddenSubscriberCount ?? record?.hidden_subscriber_count) !== undefined ? { hiddenSubscriberCount: asBoolean(record?.hiddenSubscriberCount ?? record?.hidden_subscriber_count) } : {}),
    }];
  });
}

export async function searchCollected(query: string, scope: CollectedSearchScope = "all"): Promise<CollectedSearchData> {
  const response = await request<unknown>(`/v1/search?q=${encodeURIComponent(query)}&scope=${encodeURIComponent(scope)}&limit=20`, { method: "GET" });
  const record = asRecord(response);
  return {
    query: asText(record?.query) ?? query,
    videos: firstArray(record ?? {}, ["videos"]).flatMap((item) => {
      const itemRecord = asRecord(item);
      const video = normalizeVideo(itemRecord?.video);
      const score = asNumber(itemRecord?.score);
      if (!video || score === undefined) return [];
      return [{ video, score, matchedFields: asTextArray(firstArray(itemRecord ?? {}, ["matchedFields", "matched_fields"])) }];
    }),
    comments: firstArray(record ?? {}, ["comments"]).flatMap((item) => {
      const itemRecord = asRecord(item);
      const comment = normalizeComment(itemRecord?.comment);
      const video = normalizeVideo(itemRecord?.video);
      const score = asNumber(itemRecord?.score);
      if (!comment || !video || score === undefined) return [];
      return [{
        comment, video, score,
        matchedFields: asTextArray(firstArray(itemRecord ?? {}, ["matchedFields", "matched_fields"])),
        ...(asText(itemRecord?.channelTitle ?? itemRecord?.channel_title) ? { channelTitle: asText(itemRecord?.channelTitle ?? itemRecord?.channel_title) } : {}),
      }];
    }),
  };
}

export async function updateTargetPin(targetId: string, payload: { enabled: boolean; intervalMinutes: number }): Promise<TargetPin> {
  const response = await request<unknown>(`/v1/collection-targets/${encodeURIComponent(targetId)}/pin`, {
    method: "PUT", body: JSON.stringify(payload),
  });
  const pin = normalizePin(response);
  if (!pin) throw new ApiError("핀 상태를 해석하지 못했습니다.", 502);
  return pin;
}
