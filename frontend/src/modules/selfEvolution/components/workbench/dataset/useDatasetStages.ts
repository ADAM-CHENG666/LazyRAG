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
 * @param onStageEvent receives every Dataset stream event.  Callers decide
 * whether it affects transient local progress or published stage data.
 */
export function useDatasetStages(threadId: string | undefined, onStageEvent: (event: DatasetStreamEvent) => void) {
  const [statuses, setStatuses] = useState<StageStatuses>(INITIAL_STATUSES);
  const [activeTab, setActiveTab] = useState<DatasetTab>();
  const eventHandler = useRef(onStageEvent);
  eventHandler.current = onStageEvent;

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

  useEffect(() => {
    void refreshSteps();
  }, [refreshSteps]);

  useEffect(() => {
    if (!threadId) return undefined;
    const controller = new AbortController();
    let stopped = false;
    const seenStepIds = new Set<string>();

    const consume = async () => {
      try {
        const response = await fetch(`${threadRoot(threadId)}/events:stream`, {
          headers: { Accept: "text/event-stream", ...AgentAppsAuth.getAuthHeaders() },
          signal: controller.signal,
        });
        if (!response.ok || !response.body) return;
        const reader = response.body.getReader();
        const decoder = new TextDecoder("utf-8");
        let buffer = "";
        while (!stopped) {
          const { value, done } = await reader.read();
          if (done) break;
          buffer += decoder.decode(value, { stream: true });
          const frames = buffer.split(/\r?\n\r?\n/);
          buffer = frames.pop() || "";
          for (const frame of frames) {
            const event = readDatasetStreamEvent(frame);
            if (!event) continue;
            const isFlowEvent = event.event === "step.finish" || event.event === "checkpoint.continue" || event.event === "done";
            if (isFlowEvent || (event.stepId && !seenStepIds.has(event.stepId))) {
              if (event.stepId) seenStepIds.add(event.stepId);
              void refreshSteps();
            }
            eventHandler.current(event);
          }
        }
      } catch {
        // Aborted on unmount, or Evo closed the stream when the run ended.
      }
    };

    void consume();
    return () => {
      stopped = true;
      controller.abort();
    };
  }, [refreshSteps, threadId]);

  return { statuses, activeTab, refreshSteps };
}

function readDatasetStreamEvent(frame: string): DatasetStreamEvent | undefined {
  // A frame may carry the JSON payload across several `data:` lines.
  const lines = frame.split(/\r?\n/);
  const event = lines
    .find((line) => line.startsWith("event:"))
    ?.slice(6)
    .trim();
  const data = lines
    .filter((line) => line.startsWith("data:"))
    .map((line) => line.slice(5).trim())
    .join("");
  if (!data) return undefined;
  try {
    const payload = JSON.parse(data) as {
      stage?: string;
      current_step?: string;
      step_id?: string;
      operation_id?: string;
      attempt_id?: string;
      status?: string;
      partition?: { id?: string; index?: number; total?: number };
    };
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
  } catch {
    return undefined;
  }
}
