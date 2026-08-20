import type { StepStatus, ThreadEventStage } from './types';

const DATASET_FLOW_STAGES = [
  'dataset.material_preparation',
  'dataset.topic_discovery',
  'dataset.case_generation',
] as const;

const DATASET_FLOW_STAGE_SET = new Set<string>(DATASET_FLOW_STAGES);

export function isDatasetSubStage(stage?: string): boolean {
  return !!stage && DATASET_FLOW_STAGE_SET.has(stage);
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
  return !normalized || ['pending', 'created', 'queued', 'waiting', '待执行', '等待中'].includes(normalized);
}

function isRunning(status?: string) {
  const normalized = status?.trim().toLowerCase();
  return !!normalized && ['running', 'executing', '执行中', '运行中'].includes(normalized);
}

function isTerminal(status?: string) {
  const normalized = status?.trim().toLowerCase();
  return !!normalized && ['completed', 'done', 'success', '已完成'].includes(normalized);
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

export function datasetWorkflowStepFromSteps<T extends { stage?: string; status?: string }>(
  steps: T[],
): T | undefined {
  const lastByStage = new Map<string, T>();
  for (const step of steps) {
    const stage = step.stage;
    if (!stage || !DATASET_FLOW_STAGE_SET.has(stage)) {
      continue;
    }
    lastByStage.set(stage, step);
  }
  const ordered = DATASET_FLOW_STAGES
    .map((stage) => lastByStage.get(stage))
    .filter((step): step is T => step != null);
  const started = ordered.filter((step) => !isPending(step.status));
  const running = started.filter((step) => isRunning(step.status));
  if (running.length > 0) {
    return running[0];
  }
  const representative = started.at(-1) ?? ordered[0];
  if (!representative) {
    return undefined;
  }
  const hasPendingAfter = ordered.some(
    (step) => isPending(step.status) && DATASET_FLOW_STAGES.indexOf(step.stage as typeof DATASET_FLOW_STAGES[number]) >
      DATASET_FLOW_STAGES.indexOf(representative.stage as typeof DATASET_FLOW_STAGES[number]),
  );
  if (hasPendingAfter && isTerminal(representative.status)) {
    return { ...representative, status: 'running' } as T;
  }
  return representative;
}

export function mergeWorkflowStepStatus(
  fromEvents: Partial<Record<ThreadEventStage, StepStatus>>,
  fromSteps: Partial<Record<ThreadEventStage, StepStatus>>,
): Partial<Record<ThreadEventStage, StepStatus>> {
  return { ...fromEvents, ...fromSteps };
}
