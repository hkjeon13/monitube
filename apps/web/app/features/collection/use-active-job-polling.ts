import type { JobStatus } from "@monitube/contracts";
import type { Dispatch, MutableRefObject, SetStateAction } from "react";
import { useEffect } from "react";

import {
  ApiError,
  getJob,
  listActiveJobs,
  type ActiveSourceJob,
  type SourceSummary,
} from "../../lib/api";
import {
  activeJobKey,
  activeJobPollingDelay,
  isAbortError,
  isTerminalJob,
  type WorkspacePage,
} from "./workbench-model";

type ActiveJobPollingParams = {
  authUser: string | null | undefined;
  page: WorkspacePage;
  sources: SourceSummary[];
  activeSourceId: string;
  job: JobStatus | null;
  jobSourceId: string | null;
  sourcesRef: MutableRefObject<SourceSummary[]>;
  activeSourceIdRef: MutableRefObject<string>;
  localJobRef: MutableRefObject<{ sourceId: string; job: JobStatus } | null>;
  activeJobsRef: MutableRefObject<Map<string, ActiveSourceJob>>;
  endpointSupportedRef: MutableRefObject<boolean | null>;
  handledTerminalJobsRef: MutableRefObject<Set<string>>;
  wakePollRef: MutableRefObject<(() => void) | null>;
  setSources: Dispatch<SetStateAction<SourceSummary[]>>;
  setJob: Dispatch<SetStateAction<JobStatus | null>>;
  setJobSourceId: Dispatch<SetStateAction<string | null>>;
  onUnauthorized: () => void;
  refreshSources: () => Promise<void>;
  refreshResults: (sourceId: string) => Promise<void>;
  refreshExplore: (channelId?: string | null) => Promise<void>;
  refreshRecentFailures: () => Promise<void>;
};

export function useActiveJobPolling({
  authUser,
  page,
  sources,
  activeSourceId,
  job,
  jobSourceId,
  sourcesRef,
  activeSourceIdRef,
  localJobRef,
  activeJobsRef,
  endpointSupportedRef,
  handledTerminalJobsRef,
  wakePollRef,
  setSources,
  setJob,
  setJobSourceId,
  onUnauthorized,
  refreshSources,
  refreshResults,
  refreshExplore,
  refreshRecentFailures,
}: ActiveJobPollingParams) {
  useEffect(() => {
    sourcesRef.current = sources;
  }, [sources, sourcesRef]);

  useEffect(() => {
    activeSourceIdRef.current = activeSourceId;
  }, [activeSourceId, activeSourceIdRef]);

  useEffect(() => {
    localJobRef.current = job && jobSourceId ? { sourceId: jobSourceId, job } : null;
  }, [job, jobSourceId, localJobRef]);

  useEffect(() => {
    if (!authUser) return;
    let stopped = false;
    let inFlight = false;
    let pollAgain = false;
    let timer: number | undefined;
    let controller: AbortController | null = null;
    let failureCount = 0;

    const readLegacyActiveJobs = async (signal: AbortSignal) => {
      const candidates: ActiveSourceJob[] = sourcesRef.current.flatMap((source) => (
        source.latestJob && !isTerminalJob(source.latestJob)
          ? [{ sourceId: source.id, ...(source.targetId ? { targetId: source.targetId } : {}), job: source.latestJob }]
          : []
      ));
      const local = localJobRef.current;
      if (local && !isTerminalJob(local.job) && !candidates.some((entry) => activeJobKey(entry) === `${local.sourceId}:${local.job.id}`)) {
        candidates.push({ sourceId: local.sourceId, job: local.job });
      }
      const resolved = await Promise.all(candidates.map(async (entry) => {
        try {
          return { ...entry, job: await getJob(entry.job.id, signal) };
        } catch (caught) {
          if (caught instanceof ApiError && caught.status === 404) return null;
          throw caught;
        }
      }));
      return resolved.flatMap((entry) => entry ? [entry] : []);
    };

    const readActiveJobs = async (signal: AbortSignal) => {
      if (endpointSupportedRef.current !== false) {
        try {
          const entries = await listActiveJobs(signal);
          endpointSupportedRef.current = true;
          return entries;
        } catch (caught) {
          if (!(caught instanceof ApiError) || ![404, 405, 501].includes(caught.status)) throw caught;
          endpointSupportedRef.current = false;
        }
      }
      return readLegacyActiveJobs(signal);
    };

    const schedule = (delay: number) => {
      if (stopped) return;
      if (timer !== undefined) window.clearTimeout(timer);
      timer = window.setTimeout(() => { void poll(); }, delay);
    };

    const poll = async () => {
      if (stopped || inFlight) {
        pollAgain = true;
        return;
      }
      inFlight = true;
      controller = new AbortController();
      let activeEntries: ActiveSourceJob[] = [];
      try {
        const entries = await readActiveJobs(controller.signal);
        if (stopped || controller.signal.aborted) return;
        failureCount = 0;

        const previous = activeJobsRef.current;
        const explicitTerminal = entries.filter(({ job: currentJob }) => isTerminalJob(currentJob));
        activeEntries = entries.filter(({ job: currentJob }) => !isTerminalJob(currentJob));
        const current = new Map(activeEntries.map((entry) => [activeJobKey(entry), entry]));
        const disappeared = [...previous.entries()].flatMap(([key, entry]) => current.has(key) ? [] : [entry]);
        activeJobsRef.current = current;

        const latestBySource = new Map<string, ActiveSourceJob>();
        for (const entry of [...activeEntries, ...explicitTerminal]) latestBySource.set(entry.sourceId, entry);
        setSources((currentSources) => currentSources.map((source) => {
          const entry = latestBySource.get(source.id);
          return entry ? { ...source, latestJob: entry.job } : source;
        }));

        const selectedSourceId = activeSourceIdRef.current;
        const selectedEntry = latestBySource.get(selectedSourceId);
        if (selectedEntry) {
          setJob(selectedEntry.job);
          setJobSourceId(selectedSourceId);
        } else if (disappeared.some((entry) => entry.sourceId === selectedSourceId)) {
          setJob(null);
          setJobSourceId(null);
        }

        const newlyTerminal = [...explicitTerminal, ...disappeared].filter((entry) => {
          const key = activeJobKey(entry);
          if (handledTerminalJobsRef.current.has(key)) return false;
          handledTerminalJobsRef.current.add(key);
          return true;
        });
        if (newlyTerminal.length > 0) {
          const affectedSourceIds = new Set(newlyTerminal.map((entry) => entry.sourceId));
          await Promise.all([
            refreshSources(),
            affectedSourceIds.has(selectedSourceId) && selectedSourceId ? refreshResults(selectedSourceId) : Promise.resolve(),
            refreshExplore(),
            page === "jobs" ? refreshRecentFailures() : Promise.resolve(),
          ]);
        }
      } catch (caught) {
        if (isAbortError(caught) || stopped) return;
        if (caught instanceof ApiError && caught.status === 401) {
          stopped = true;
          onUnauthorized();
          return;
        }
        failureCount += 1;
      } finally {
        inFlight = false;
        controller = null;
        if (!stopped) {
          const delay = pollAgain ? 0 : activeJobPollingDelay(activeEntries, failureCount);
          pollAgain = false;
          schedule(delay);
        }
      }
    };

    const handleVisibilityChange = () => {
      if (document.visibilityState !== "visible") return;
      if (inFlight) pollAgain = true;
      else schedule(0);
    };
    wakePollRef.current = handleVisibilityChange;
    document.addEventListener("visibilitychange", handleVisibilityChange);
    schedule(0);
    return () => {
      stopped = true;
      if (timer !== undefined) window.clearTimeout(timer);
      controller?.abort();
      if (wakePollRef.current === handleVisibilityChange) wakePollRef.current = null;
      document.removeEventListener("visibilitychange", handleVisibilityChange);
    };
  }, [
    activeJobsRef,
    activeSourceIdRef,
    authUser,
    endpointSupportedRef,
    handledTerminalJobsRef,
    localJobRef,
    onUnauthorized,
    page,
    refreshExplore,
    refreshRecentFailures,
    refreshResults,
    refreshSources,
    setJob,
    setJobSourceId,
    setSources,
    sourcesRef,
    wakePollRef,
  ]);
}
