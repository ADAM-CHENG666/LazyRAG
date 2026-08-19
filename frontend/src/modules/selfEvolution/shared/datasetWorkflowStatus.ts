import type { StepStatus, ThreadEventStage } from './types';

const DATASET_FLOW_STAGES = [
  'dataset.material_preparation',
  'dataset.topic_discovery',
  'dataset.case_generation',
] as const;

const DATASET_FLOW_STAGE_SET = new Set<string>(DATASET_FLOW_STAGES);

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
  return started.at(-1) ?? ordered[0];
}

export function mergeWorkflowStepStatus(
  fromEvents: Partial<Record<ThreadEventStage, StepStatus>>,
  fromSteps: Partial<Record<ThreadEventStage, StepStatus>>,
): Partial<Record<ThreadEventStage, StepStatus>> {
  return { ...fromEvents, ...fromSteps };
}
