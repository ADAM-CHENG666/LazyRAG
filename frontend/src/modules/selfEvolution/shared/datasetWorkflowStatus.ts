import type { StepStatus, ThreadEventStage } from './types';

const DATASET_FLOW_STAGES = [
  'dataset.material_preparation',
  'dataset.topic_discovery',
  'dataset.case_generation',
] as const;

export type DatasetFlowStage = (typeof DATASET_FLOW_STAGES)[number];

export type DatasetTabId = 'materials' | 'topics' | 'cases';

const DATASET_FLOW_STAGE_SET = new Set<string>(DATASET_FLOW_STAGES);

const TAB_BY_STAGE: Record<DatasetFlowStage, DatasetTabId> = {
  'dataset.material_preparation': 'materials',
  'dataset.topic_discovery': 'topics',
  'dataset.case_generation': 'cases',
};

const STAGE_BY_TAB: Record<DatasetTabId, DatasetFlowStage> = {
  materials: 'dataset.material_preparation',
  topics: 'dataset.topic_discovery',
  cases: 'dataset.case_generation',
};

export function isDatasetSubStage(stage?: string): boolean {
  return !!stage && DATASET_FLOW_STAGE_SET.has(stage);
}

export function datasetTabForFlowStage(stage?: string): DatasetTabId | undefined {
  return stage && isDatasetSubStage(stage)
    ? TAB_BY_STAGE[stage as DatasetFlowStage]
    : undefined;
}

export function datasetFlowStageForTab(tab: DatasetTabId): DatasetFlowStage {
  return STAGE_BY_TAB[tab];
}

/** A plan-capacity gate pauses Dataset itself; it is not a completed checkpoint. */
export function terminalDatasetWorkflowStatus(status?: string): StepStatus | undefined {
  return status?.trim().toLowerCase() === 'paused' ? 'paused' : undefined;
}

const COARSE_STAGE_BY_NAME: Record<string, ThreadEventStage> = {
  dataset: 'dataset',
  eval: 'eval',
  candidate_eval: 'abtest',
  run: 'analysis',
  analysis: 'analysis',
  apply: 'repair',
  repair: 'repair',
  abtest: 'abtest',
};

function isPending(status?: string) {
  const normalized = status?.trim().toLowerCase();
  return !normalized || ['pending', 'created', 'queued', 'waiting', '待执行', '等待中', 'idle'].includes(normalized);
}

function isRunning(status?: string) {
  const normalized = status?.trim().toLowerCase();
  return !!normalized && ['running', 'executing', 'pausing', '执行中', '运行中'].includes(normalized);
}

function isCheckpointStatus(status?: string) {
  const normalized = status?.trim().toLowerCase();
  return !!normalized && ['completed', 'done', 'success', 'paused', '已完成'].includes(normalized);
}

export function toThreadEventStage(value: unknown): ThreadEventStage | undefined {
  if (typeof value !== 'string') {
    return undefined;
  }
  const normalized = value.trim();
  if (!normalized) {
    return undefined;
  }
  return COARSE_STAGE_BY_NAME[normalized] || COARSE_STAGE_BY_NAME[normalized.split('.')[0]];
}

type DatasetStepLike = {
  stage?: string;
  status?: string;
  nextStepRunId?: string;
  orderIndex?: number;
  stepId?: string;
};

export type DatasetFinalResultStatus = 'done' | 'partial';

export type DatasetWorkflowView<T extends DatasetStepLike = DatasetStepLike> = {
  /** Step that represents the coarse「数据集生成」status. */
  representative?: T;
  /** Raw /steps status for the coarse Dataset stage. */
  topStatus?: string;
  canContinue: boolean;
  continueStepId?: string;
  checkpoint?: T;
  /** Last row per Dataset sub-stage (raw /steps status). */
  subStatuses: Record<DatasetTabId, string>;
  activeStepId?: string;
  suggestedTab?: DatasetTabId;
};

/**
 * Single derivation for Dataset top status, sub-nav, and continue affordance.
 * All three UI surfaces must read this — never invent a parallel status.
 */
export function deriveDatasetView<T extends DatasetStepLike>(
  steps: T[],
  activeStepId?: string,
): DatasetWorkflowView<T> {
  const subStatuses: Record<DatasetTabId, string> = {
    materials: 'pending',
    topics: 'pending',
    cases: 'pending',
  };
  const lastByStage = new Map<DatasetFlowStage, T>();
  for (const step of steps) {
    const stage = step.stage;
    if (!stage || !DATASET_FLOW_STAGE_SET.has(stage)) {
      continue;
    }
    lastByStage.set(stage as DatasetFlowStage, step);
    const tab = TAB_BY_STAGE[stage as DatasetFlowStage];
    if (tab && step.status) {
      subStatuses[tab] = step.status;
    }
  }

  const checkpoint = getDatasetCheckpointWaitingStep(steps);
  const ordered = DATASET_FLOW_STAGES
    .map((stage) => lastByStage.get(stage))
    .filter((step): step is T => step != null);
  const started = ordered.filter((step) => !isPending(step.status));
  const running = started.filter((step) => isRunning(step.status));

  let representative: T | undefined;
  if (running.length > 0) {
    representative = running[0];
  } else if (checkpoint) {
    representative = checkpoint;
  } else {
    representative = started.at(-1) ?? ordered[0];
  }

  let suggestedTab: DatasetTabId | undefined;
  if (activeStepId) {
    const active = steps.find((step) => step.stepId === activeStepId);
    suggestedTab = datasetTabForFlowStage(active?.stage);
  }
  if (!suggestedTab && checkpoint) {
    suggestedTab = datasetTabForFlowStage(checkpoint.stage);
  }
  if (!suggestedTab && representative) {
    suggestedTab = datasetTabForFlowStage(representative.stage);
  }
  if (!suggestedTab) {
    const paused = ordered.find((step) => step.status?.trim().toLowerCase() === 'paused');
    suggestedTab = datasetTabForFlowStage(paused?.stage);
  }

  return {
    representative,
    topStatus: representative?.status,
    canContinue: Boolean(checkpoint),
    continueStepId: checkpoint?.nextStepRunId?.trim() || undefined,
    checkpoint,
    subStatuses,
    activeStepId,
    suggestedTab,
  };
}

/**
 * The top-level Dataset card represents the final dataset, not whichever
 * sub-stage happened to finish most recently. A result status is supplied only
 * after `GET .../dataset/result` confirms that the current case-generation
 * run produced `eval.dataset`.
 */
export function deriveDatasetTopStatus<T extends DatasetStepLike>(
  steps: T[],
  resultStatus?: DatasetFinalResultStatus,
): StepStatus {
  const ordered = datasetStepsInOrder(steps);
  const lastByStage = new Map<DatasetFlowStage, T>();
  for (const step of ordered) {
    lastByStage.set(step.stage as DatasetFlowStage, step);
  }
  const latestStarted = [...ordered].reverse().find((step) => !isPending(step.status));
  if (!latestStarted) return 'pending';
  if ([...lastByStage.values()].some((step) => isRunning(step.status))) return 'running';

  const latestStatus = latestStarted.status?.trim().toLowerCase();
  if (['failed', 'error'].includes(latestStatus || '')) return 'failed';
  if (['canceled', 'cancelled'].includes(latestStatus || '')) return 'canceled';
  if (latestStatus === 'paused' && !getDatasetCheckpointWaitingStep(steps)) return 'paused';
  if (getDatasetCheckpointWaitingStep(steps)) return 'waiting';

  if (latestStarted.stage === 'dataset.case_generation' && isFinalizedStep(latestStarted.status)) {
    return resultStatus || 'running';
  }
  return 'running';
}

/** The latest started Dataset step only qualifies after its final root is due. */
export function datasetFinalizationStep<T extends DatasetStepLike>(steps: T[]): T | undefined {
  const latestStarted = [...datasetStepsInOrder(steps)]
    .reverse()
    .find((step) => !isPending(step.status));
  return latestStarted?.stage === 'dataset.case_generation' && isFinalizedStep(latestStarted.status)
    ? latestStarted
    : undefined;
}

function datasetStepsInOrder<T extends DatasetStepLike>(steps: T[]): T[] {
  return steps
    .map((step, index) => ({ step, index }))
    .filter(({ step }) => step.stage && DATASET_FLOW_STAGE_SET.has(step.stage))
    .sort((left, right) => (left.step.orderIndex ?? left.index) - (right.step.orderIndex ?? right.index))
    .map(({ step }) => step);
}

function isFinalizedStep(status?: string) {
  const normalized = status?.trim().toLowerCase();
  return ['completed', 'done', 'success', 'partial', 'partial_failed', '已完成', '部分失败'].includes(normalized || '');
}

/** @deprecated Prefer deriveDatasetView().representative — kept for coarse stage merge. */
export function datasetWorkflowStepFromSteps<T extends DatasetStepLike>(
  steps: T[],
): T | undefined {
  return deriveDatasetView(steps).representative;
}

/** The finished Dataset sub-step waiting for the user's next-step action. */
export function getDatasetCheckpointWaitingStep<T extends DatasetStepLike>(
  steps: T[],
): T | undefined {
  const ordered = steps
    .map((step, index) => ({ step, index }))
    .filter(({ step }) => isDatasetSubStage(step.stage))
    .sort((left, right) => (left.step.orderIndex ?? left.index) - (right.step.orderIndex ?? right.index));
  let laterStageStarted = false;
  for (let index = ordered.length - 1; index >= 0; index -= 1) {
    const step = ordered[index].step;
    const waiting = isCheckpointStatus(step.status) && Boolean(step.nextStepRunId?.trim());
    if (waiting && !laterStageStarted) {
      return step;
    }
    if (!isPending(step.status)) {
      laterStageStarted = true;
    }
  }
  return undefined;
}

export type DatasetNavStatus =
  | 'done'
  | 'running'
  | 'paused'
  | 'pending'
  | 'failed'
  | 'partial'
  | 'stale';

/** Map a raw /steps status onto the Dataset sub-nav vocabulary. */
export function toDatasetNavStatus(status?: string): DatasetNavStatus {
  const normalized = status?.trim().toLowerCase();
  if (!normalized) return 'pending';
  if (['completed', 'done', 'success', '已完成'].includes(normalized)) return 'done';
  if (['running', 'pausing', 'executing', '执行中', '运行中'].includes(normalized)) return 'running';
  if (['paused', '已暂停', '暂停'].includes(normalized)) return 'paused';
  if (['failed', 'cancelled', 'canceled', 'cancelling', '失败'].includes(normalized)) return 'failed';
  if (['partial_failed', 'partial', '部分失败'].includes(normalized)) return 'partial';
  return 'pending';
}

export function mergeWorkflowStepStatus(
  fromEvents: Partial<Record<ThreadEventStage, StepStatus>>,
  fromSteps: Partial<Record<ThreadEventStage, StepStatus>>,
): Partial<Record<ThreadEventStage, StepStatus>> {
  return { ...fromEvents, ...fromSteps };
}
