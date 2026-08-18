export const TOPIC_DISCOVERY_STAGE = "dataset.topic_discovery";
export const TOPIC_LABEL_OPERATION = "dataset.label_embedding_cluster";

type PartitionStatus = "running" | "completed" | "failed" | "canceled";

export type TopicLabelPartitionEvent = {
  event: string;
  stage: string;
  operationId?: string;
  stepId: string;
  partition?: { id?: string; total?: number };
  status?: string;
};

export type TopicLabelProgress = {
  stepId: string;
  total: number;
  statuses: Record<string, PartitionStatus>;
};

export type TopicLabelProgressSummary = {
  total: number;
  completed: number;
  running: number;
  failed: number;
};

export function applyTopicLabelPartitionEvent(
  current: TopicLabelProgress | undefined,
  event: TopicLabelPartitionEvent,
): TopicLabelProgress | undefined {
  if (
    event.event !== TOPIC_LABEL_OPERATION ||
    event.stage !== TOPIC_DISCOVERY_STAGE ||
    event.operationId !== TOPIC_LABEL_OPERATION ||
    !event.stepId ||
    !event.partition?.id ||
    !isPartitionStatus(event.status)
  ) {
    return current;
  }

  const base = current?.stepId === event.stepId
    ? current
    : { stepId: event.stepId, total: 0, statuses: {} };
  return {
    stepId: base.stepId,
    total: event.partition.total ?? base.total,
    statuses: { ...base.statuses, [event.partition.id]: event.status },
  };
}

export function summarizeTopicLabelProgress(
  progress: TopicLabelProgress | undefined,
): TopicLabelProgressSummary {
  const summary: TopicLabelProgressSummary = { total: progress?.total || 0, completed: 0, running: 0, failed: 0 };
  for (const status of Object.values(progress?.statuses || {})) {
    if (status === "completed") summary.completed += 1;
    else if (status === "running") summary.running += 1;
    else if (status === "failed") summary.failed += 1;
  }
  return summary;
}

function isPartitionStatus(status: string | undefined): status is PartitionStatus {
  return status === "running" || status === "completed" || status === "failed" || status === "canceled";
}
