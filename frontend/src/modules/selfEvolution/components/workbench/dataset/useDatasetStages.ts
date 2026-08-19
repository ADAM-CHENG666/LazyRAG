import { useCallback, useEffect, useRef, useState } from "react";
import { AgentAppsAuth } from "@/components/auth";
import { getJson, threadRoot } from "./api";
import type { DatasetTab, ThreadStepsResponse, VisualStatus } from "./types";
import { toVisualStatus } from "./primitives";

const STAGE_BY_TAB: Record<DatasetTab, string> = {
  materials: "dataset.material_preparation",
  topics: "dataset.topic_discovery",
  cases: "dataset.case_generation",
};

const TAB_BY_STAGE: Record<string, DatasetTab> = Object.fromEntries(
  Object.entries(STAGE_BY_TAB).map(([tab, stage]) => [stage, tab as DatasetTab]),
) as Record<string, DatasetTab>;

export const DATASET_TABS: Array<{ id: DatasetTab; label: string }> = [
  { id: "materials", label: "材料准备" },
  { id: "topics", label: "主题发现" },
  { id: "cases", label: "用例生成" },
];

export type StageStatuses = Record<DatasetTab, VisualStatus>;

export type DatasetStreamEvent = {
  event: string;
  tab: DatasetTab;
  stage: string;
  stepId: string;
  operationId?: string;
  attemptId?: string;
  status?: string;
  partition?: { id?: string; index?: number; total?: number };
};

const INITIAL_STATUSES: StageStatuses = {
  materials: "pending",
  topics: "pending",
  cases: "pending",
};

/**
 * Derives the three dataset step states from the shared thread step list and
 * keeps them live through the thread event stream.
 *
 * One GET /events:stream lasts until Flow reaches a terminal snapshot (`done`).
 * Writes that start a new round call `resumeAfterWrite`, which reopens the
 * stream with `Last-Event-ID` only when that previous GET has already ended.
 *
 * @param onStageEvent receives every Dataset stream event.  Callers decide
 * whether it affects transient local progress or published stage data.
 */
export function useDatasetStages(threadId: string | undefined, onStageEvent: (event: DatasetStreamEvent) => void) {
  const [statuses, setStatuses] = useState<StageStatuses>(INITIAL_STATUSES);
  const [activeTab, setActiveTab] = useState<DatasetTab>();
  const eventHandler = useRef(onStageEvent);
  eventHandler.current = onStageEvent;
  const resumeStream = useRef<() => void>(() => undefined);

  const refreshSteps = useCallback(async () => {
    if (!threadId) return;
    try {
      const response = await getJson<ThreadStepsResponse>(`${threadRoot(threadId)}/steps`);
      const next = { ...INITIAL_STATUSES };
      let running: DatasetTab | undefined;
      // Later entries are re-runs of the same stage and win over earlier ones.
      for (const item of response.items || []) {
        const tab = TAB_BY_STAGE[item.stage];
        if (!tab) continue;
        next[tab] = toVisualStatus(item.status);
        if (item.step_id === response.active_step_id) running = tab;
      }
      setStatuses(next);
      setActiveTab(running);
    } catch {
      // The stepper keeps its previous state; the stage panels report their own errors.
    }
  }, [threadId]);

  const resumeAfterWrite = useCallback(() => {
    void refreshSteps();
    resumeStream.current();
  }, [refreshSteps]);

  useEffect(() => {
    void refreshSteps();
  }, [refreshSteps]);

  useEffect(() => {
    if (!threadId) return undefined;
    let stopped = false;
    let round = 0;
    let activeController: AbortController | undefined;
    const seenStepIds = new Set<string>();
    let lastEventId: string | undefined;
    let streamEnded = false;

    const consume = async () => {
      const myRound = ++round;
      streamEnded = false;
      activeController?.abort();
      const controller = new AbortController();
      activeController = controller;
      try {
        const headers: Record<string, string> = {
          Accept: "text/event-stream",
          ...AgentAppsAuth.getAuthHeaders(),
        };
        if (lastEventId) headers["Last-Event-ID"] = lastEventId;
        const response = await fetch(`${threadRoot(threadId)}/events:stream`, {
          headers,
          signal: controller.signal,
        });
        if (!response.ok || !response.body) return;
        const reader = response.body.getReader();
        const decoder = new TextDecoder("utf-8");
        let buffer = "";
        while (!stopped && myRound === round) {
          const { value, done } = await reader.read();
          if (done) break;
          buffer += decoder.decode(value, { stream: true });
          const frames = buffer.split(/\r?\n\r?\n/);
          buffer = frames.pop() || "";
          let reachedDone = false;
          for (const frame of frames) {
            const parsed = parseDatasetSseFrame(frame);
            if (!parsed) continue;
            if (parsed.cursor) lastEventId = parsed.cursor;
            const event = toDatasetStreamEvent(parsed);
            if (parsed.event === "done") {
              streamEnded = true;
              void refreshSteps();
              if (event) eventHandler.current(event);
              reachedDone = true;
              break;
            }
            if (!event) continue;
            const isFlowEvent = event.event === "step.finish" || event.event === "checkpoint.continue";
            if (isFlowEvent || (event.stepId && !seenStepIds.has(event.stepId))) {
              if (event.stepId) seenStepIds.add(event.stepId);
              void refreshSteps();
            }
            eventHandler.current(event);
          }
          if (reachedDone) {
            await reader.cancel().catch(() => undefined);
            break;
          }
        }
      } catch {
        // Aborted on unmount / reconnect, or Evo closed the stream when the run ended.
      } finally {
        if (myRound === round) streamEnded = true;
      }
    };

    void consume();
    resumeStream.current = () => {
      if (stopped || !streamEnded) return;
      void consume();
    };

    return () => {
      stopped = true;
      resumeStream.current = () => undefined;
      activeController?.abort();
    };
  }, [refreshSteps, threadId]);

  return { statuses, activeTab, refreshSteps, resumeAfterWrite };
}

type ParsedDatasetFrame = {
  event: string;
  cursor?: string;
  payload: {
    stage?: string;
    current_step?: string;
    step_id?: string;
    operation_id?: string;
    attempt_id?: string;
    status?: string;
    last_event_id?: string;
    partition?: { id?: string; index?: number; total?: number };
  };
};

function parseDatasetSseFrame(frame: string): ParsedDatasetFrame | undefined {
  const lines = frame.split(/\r?\n/);
  let event = "";
  let id: string | undefined;
  const data = lines
    .filter((line) => line.startsWith("data:"))
    .map((line) => line.slice(5).trim())
    .join("");
  for (const line of lines) {
    if (line.startsWith("id:")) id = line.slice(3).trim() || undefined;
    if (line.startsWith("event:")) event = line.slice(6).trim();
  }
  if (!data) return undefined;
  try {
    const payload = JSON.parse(data) as ParsedDatasetFrame["payload"];
    const cursor = id || (typeof payload.last_event_id === "string" ? payload.last_event_id : undefined);
    return { event: event || "message", cursor, payload };
  } catch {
    return undefined;
  }
}

function toDatasetStreamEvent(parsed: ParsedDatasetFrame): DatasetStreamEvent | undefined {
  const { event, payload } = parsed;
  const stage = payload.stage || payload.current_step;
  const tab = stage ? TAB_BY_STAGE[stage] : undefined;
  return stage && tab && event
    ? {
        event,
        tab,
        stage,
        stepId: payload.step_id || "",
        operationId: payload.operation_id,
        attemptId: payload.attempt_id,
        status: payload.status,
        partition: payload.partition,
      }
    : undefined;
}
