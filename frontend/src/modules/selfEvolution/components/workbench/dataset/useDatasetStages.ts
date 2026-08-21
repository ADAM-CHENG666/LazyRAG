import { useCallback, useEffect, useRef, useState } from "react";
import { AgentAppsAuth } from "@/components/auth";
import { getJson, threadRoot } from "./api";
import type { DatasetTab, ThreadStepsResponse, VisualStatus } from "./types";
import {
  activeDatasetTabForThread,
  datasetTabForStage,
  deriveDatasetStageState,
  INITIAL_STAGE_STATUSES,
  isCurrentDatasetExecutionEvent,
} from "./stageState";

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
  progress?: { current?: number | null; total?: number | null };
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
  const [statuses, setStatuses] = useState<StageStatuses>(INITIAL_STAGE_STATUSES);
  const [activeTab, setActiveTab] = useState<DatasetTab>();
  const [activeTabThreadId, setActiveTabThreadId] = useState<string>();
  const eventHandler = useRef(onStageEvent);
  eventHandler.current = onStageEvent;
  const resumeStream = useRef<(force?: boolean) => void>(() => undefined);
  const activeStepId = useRef<string>();
  const inactiveStepIds = useRef(new Set<string>());

  const refreshSteps = useCallback(async () => {
    if (!threadId) return undefined;
    try {
      const response = await getJson<ThreadStepsResponse>(`${threadRoot(threadId)}/steps`);
      const next = deriveDatasetStageState(response);
      activeStepId.current = next.activeStepId;
      inactiveStepIds.current = new Set(
        (response.items || [])
          .map((item) => item.step_id)
          .filter((stepId) => stepId && stepId !== next.activeStepId),
      );
      setStatuses(next.statuses);
      setActiveTab(next.activeTab);
      setActiveTabThreadId(threadId);
      return next;
    } catch {
      // The stepper keeps its previous state; the stage panels report their own errors.
      return undefined;
    }
  }, [threadId]);

  const resumeAfterWrite = useCallback(() => {
    resumeStream.current(true);
  }, []);

  useEffect(() => {
    setStatuses(INITIAL_STAGE_STATUSES);
    setActiveTab(undefined);
    setActiveTabThreadId(undefined);
    activeStepId.current = undefined;
    inactiveStepIds.current.clear();
  }, [threadId]);

  useEffect(() => {
    if (!threadId) return undefined;
    let stopped = false;
    let round = 0;
    let activeController: AbortController | undefined;
    let lastEventId: string | undefined;
    let streamEnded = false;

    const consume = async () => {
      const myRound = ++round;
      streamEnded = false;
      activeController?.abort();
      const controller = new AbortController();
      activeController = controller;
      try {
        await refreshSteps();
        if (stopped || myRound !== round) return;
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
              await refreshSteps();
              if (event) eventHandler.current(event);
              reachedDone = true;
              break;
            }
            if (!event) continue;
            const isFlowEvent = event.event === "step.finish" || event.event === "checkpoint.continue";
            if (!isCurrentDatasetExecutionEvent(activeStepId.current, event.stepId)) {
              if (inactiveStepIds.current.has(event.stepId)) continue;
              const next = await refreshSteps();
              if (!isCurrentDatasetExecutionEvent(next?.activeStepId, event.stepId)) continue;
            }
            eventHandler.current(event);
            if (isFlowEvent) {
              await refreshSteps();
            }
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
    resumeStream.current = (force?: boolean) => {
      if (stopped) return;
      if (!force && !streamEnded) return;
      void consume();
    };

    return () => {
      stopped = true;
      resumeStream.current = () => undefined;
      activeController?.abort();
    };
  }, [refreshSteps, threadId]);

  return {
    statuses,
    activeTab: activeDatasetTabForThread(threadId, activeTabThreadId, activeTab),
    refreshSteps,
    resumeAfterWrite,
  };
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
    progress?: { current?: number | null; total?: number | null };
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
  const tab = stage ? datasetTabForStage(stage) : undefined;
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
        progress: payload.progress,
      }
    : undefined;
}
