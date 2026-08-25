import type { CaseStageKey, OperationStatus, VisualStatus } from './types';
import { acceptsAttemptUpdate, mergeStableExecutionStatus } from './executionStore';

export const CASE_GENERATION_STAGE = 'dataset.case_generation';

const CASE_STAGE_BY_OPERATION: Record<string, CaseStageKey> = {
  'dataset.qaplan_spec': 'plan',
  'dataset.generate_case': 'generate',
  'dataset.enhance_case': 'grading',
};

const CASE_STAGE_ORDER: CaseStageKey[] = ['plan', 'generate', 'grading'];

export type CaseGenerationEvent = {
  stage: string;
  tab?: string;
  operationId?: string;
  attemptId?: string;
  event: string;
  stepId: string;
  partition?: { id?: string; total?: number };
  status?: string;
};

export type CaseGenerationStep = {
  key: CaseStageKey;
  completed: number;
  total: number;
  running: number;
  failed: number;
  canceled: number;
  status: VisualStatus;
};

export type CaseGenerationProgress = {
  stepId: string;
  totals: Partial<Record<CaseStageKey, number>>;
  partitions: Partial<Record<CaseStageKey, Record<string, OperationStatus>>>;
  attempts: Partial<Record<CaseStageKey, Record<string, string>>>;
};

type StableCaseGenerationStep = {
  status?: string;
  completed?: number | null;
  total?: number | null;
  status_counts?: Partial<Record<OperationStatus, number | null>> | null;
};

export type CaseGenerationDisplayStep = {
  completed: number;
  total: number;
  running: number;
  failed: number;
  canceled: number;
  pending: number;
  status: VisualStatus;
};

type CaseExecutionReconciliation = {
  reconciliationToken: number;
  lastReconciledToken: number;
  expectedOverviewToken: number;
  loadedOverviewToken?: number;
  expectedListToken: number;
  loadedListToken?: number;
  overviewExecutionRevision?: string;
  listExecutionRevision?: string;
};

export function applyCaseGenerationPartitionEvent(
  current: CaseGenerationProgress | undefined,
  event: CaseGenerationEvent,
): CaseGenerationProgress | undefined {
  if (event.stage !== CASE_GENERATION_STAGE && event.tab !== 'cases') return current;
  const key = CASE_STAGE_BY_OPERATION[event.operationId || event.event];
  const caseId = event.partition?.id;
  const status = toOperationStatus(event.status);
  if (!key || !event.stepId || !caseId || !status) return current;

  const next = current?.stepId === event.stepId ? cloneProgress(current) : emptyProgress(event.stepId);
  const currentStatus = next.partitions[key]?.[caseId];
  const currentAttemptId = next.attempts[key]?.[caseId];
  if (!acceptsAttemptUpdate(
    currentStatus ? { attemptId: currentAttemptId, status: currentStatus } : undefined,
    { attemptId: event.attemptId, status },
  )) return current;
  next.partitions[key] = { ...(next.partitions[key] || {}), [caseId]: status };
  if (event.attemptId) {
    next.attempts[key] = { ...(next.attempts[key] || {}), [caseId]: event.attemptId };
  }
  if (event.partition?.total != null) {
    next.totals[key] = Math.max(next.totals[key] || 0, event.partition.total);
  }
  return next;
}

export function caseGenerationSteps(progress: CaseGenerationProgress | undefined): CaseGenerationStep[] {
  return CASE_STAGE_ORDER.map((key) => summarizeStep(
    key,
    progress?.partitions[key] || {},
    progress?.totals[key] || 0,
  ));
}

export function caseGenerationDisplayStep(
  transient: CaseGenerationStep | undefined,
  stable: StableCaseGenerationStep | undefined,
  currentStageStatus: VisualStatus,
): CaseGenerationDisplayStep {
  const stableTotal = stable?.total ?? 0;
  const stableCounts = stable?.status_counts;
  const stableCompleted = stable?.completed ?? 0;
  const stableRunning = stableCounts?.running ?? 0;
  const stableFailed = stableCounts?.failed ?? 0;
  const stableCanceled = stableCounts?.canceled ?? 0;
  const stablePending = stableCounts?.pending ?? 0;
  if (transient?.total) {
    const total = Math.max(stableTotal, transient.total);
    // The execution store contains every observed partition of the current
    // step. The Case table and overview must derive from that same source;
    // stable counts can belong to the previous execution round.
    const completed = transient.completed;
    const running = transient.running;
    const failed = transient.failed;
    const canceled = transient.canceled;
    const pending = Math.max(0, total - completed - running - failed - canceled);
    return {
      completed,
      total,
      running,
      failed,
      canceled,
      pending,
      status: failed ? 'failed' : running ? 'running' : completed === total && total > 0 ? 'done' : 'pending',
    };
  }

  const total = stableTotal;
  if (currentStageStatus === 'running') {
    return {
      completed: 0,
      total,
      running: 0,
      failed: 0,
      canceled: 0,
      pending: total,
      status: 'pending',
    };
  }

  return {
    completed: stableCompleted,
    total,
    running: stableRunning,
    failed: stableFailed,
    canceled: stableCanceled,
    pending: stablePending,
    status: toVisualStatus(stable?.status),
  };
}

export function shouldReconcileCaseExecution(input: CaseExecutionReconciliation): boolean {
  return input.reconciliationToken > input.lastReconciledToken
    && input.loadedOverviewToken === input.expectedOverviewToken
    && input.loadedListToken === input.expectedListToken
    && Boolean(input.overviewExecutionRevision)
    && input.overviewExecutionRevision === input.listExecutionRevision;
}

export function overlayCaseProgress<T extends { case_id: string; stages: Record<CaseStageKey, OperationStatus> }>(
  rows: T[],
  progress: CaseGenerationProgress | undefined,
): T[] {
  if (!progress) return rows;
  return rows.map((row) => {
    let changed = false;
    const stages = { ...row.stages };
    for (const key of CASE_STAGE_ORDER) {
      const next = progress.partitions[key]?.[row.case_id];
      if (next && next !== stages[key]) {
        const merged = mergeStableExecutionStatus(stages[key], next);
        if (merged === stages[key]) continue;
        stages[key] = merged;
        changed = true;
      }
    }
    return changed ? { ...row, stages } : row;
  });
}

function toOperationStatus(status?: string): OperationStatus | undefined {
  if (status === 'running' || status === 'completed' || status === 'failed' || status === 'canceled') {
    return status;
  }
  return undefined;
}

function toVisualStatus(status?: string): VisualStatus {
  if (status === 'completed' || status === 'succeeded') return 'done';
  if (status === 'running') return 'running';
  if (status === 'paused') return 'paused';
  if (status === 'failed' || status === 'canceled') return 'failed';
  return 'pending';
}

function emptyProgress(stepId: string): CaseGenerationProgress {
  return { stepId, totals: {}, partitions: {}, attempts: {} };
}

function cloneProgress(progress: CaseGenerationProgress): CaseGenerationProgress {
  return {
    stepId: progress.stepId,
    totals: { ...progress.totals },
    partitions: {
      plan: { ...progress.partitions.plan },
      generate: { ...progress.partitions.generate },
      grading: { ...progress.partitions.grading },
    },
    attempts: {
      plan: { ...progress.attempts.plan },
      generate: { ...progress.attempts.generate },
      grading: { ...progress.attempts.grading },
    },
  };
}

function summarizeStep(
  key: CaseStageKey,
  statuses: Record<string, OperationStatus>,
  total: number,
): CaseGenerationStep {
  const values = Object.values(statuses);
  const completed = values.filter((status) => status === 'completed').length;
  const running = values.filter((status) => status === 'running').length;
  const failed = values.filter((status) => status === 'failed').length;
  const canceled = values.filter((status) => status === 'canceled').length;
  const resolvedTotal = Math.max(total, values.length);
  const status: VisualStatus = failed
    ? 'failed'
    : running
      ? 'running'
      : resolvedTotal > 0 && completed === resolvedTotal
        ? 'done'
        : canceled === resolvedTotal && resolvedTotal > 0
          ? 'failed'
          : 'pending';
  return { key, completed, total: resolvedTotal, running, failed, canceled, status };
}
