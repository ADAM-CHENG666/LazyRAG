import type { VisualStatus } from './types';
import { acceptsAttemptUpdate } from './executionStore';

export const TOPIC_DISCOVERY_STAGE = 'dataset.topic_discovery';

export const TOPIC_PHASE_ORDER = ['entities', 'semantic', 'topics'] as const;
export type TopicPhaseId = (typeof TOPIC_PHASE_ORDER)[number];

const PHASE_LABEL: Record<TopicPhaseId, string> = {
  entities: '实体提取',
  semantic: '语义发现',
  topics: '主题生成',
};

const PHASE_BY_OPERATION: Record<string, TopicPhaseId> = {
  'dataset.extract_chunk_entities': 'entities',
  'dataset.chunk_entities_manifest': 'entities',
  'dataset.cluster_embeddings': 'semantic',
  'dataset.label_embedding_cluster': 'semantic',
  'dataset.embedding_label_manifest': 'semantic',
  'dataset.topic_manifest': 'topics',
};

const PARTITION_OPS = new Set([
  'dataset.extract_chunk_entities',
  'dataset.label_embedding_cluster',
]);

const COMPLETE_OPS = new Set([
  'dataset.chunk_entities_manifest',
  'dataset.embedding_label_manifest',
  'dataset.topic_manifest',
]);

type PartitionStatus = 'running' | 'completed' | 'failed' | 'canceled';

export type TopicLabelPartitionEvent = {
  event: string;
  stage: string;
  tab?: string;
  operationId?: string;
  attemptId?: string;
  stepId: string;
  partition?: { id?: string; total?: number };
  status?: string;
  progress?: { current?: number | null; total?: number | null };
};

export type TopicPhaseState = {
  id: TopicPhaseId;
  label: string;
  completed: number;
  total: number;
  running: number;
  failed: number;
  status: VisualStatus;
};

export type TopicDiscoveryProgress = {
  stepId: string;
  partitions: Partial<Record<TopicPhaseId, Record<string, PartitionStatus>>>;
  attempts: Partial<Record<TopicPhaseId, Record<string, string>>>;
  phases: Record<TopicPhaseId, TopicPhaseState>;
};

export type TopicDiscoveryStepView = {
  key: TopicPhaseId;
  label: string;
  completed: number;
  total: number | null;
  status: VisualStatus;
  summary: string;
};

export function applyTopicLabelPartitionEvent(
  current: TopicDiscoveryProgress | undefined,
  event: TopicLabelPartitionEvent,
): TopicDiscoveryProgress | undefined {
  if (event.stage !== TOPIC_DISCOVERY_STAGE && event.tab !== 'topics') {
    return current;
  }

  if (event.event === 'step.finish') {
    return current ? completeAllPhases(current) : current;
  }

  const operationId = event.operationId || event.event;
  const phaseId = PHASE_BY_OPERATION[operationId];
  if (!phaseId || !event.stepId) {
    return current;
  }

  const base =
    current?.stepId === event.stepId
      ? cloneProgress(current)
      : emptyProgress(event.stepId);

  completeEarlierPhases(base, phaseId);

  if (PARTITION_OPS.has(operationId) && event.partition?.id && isPartitionStatus(event.status)) {
    const statuses = { ...(base.partitions[phaseId] || {}) };
    const attemptId = base.attempts[phaseId]?.[event.partition.id];
    if (!acceptsAttemptUpdate(
      statuses[event.partition.id] ? { attemptId, status: statuses[event.partition.id] } : undefined,
      { attemptId: event.attemptId, status: event.status },
    )) return current;
    statuses[event.partition.id] = event.status;
    base.partitions[phaseId] = statuses;
    if (event.attemptId) {
      base.attempts[phaseId] = { ...(base.attempts[phaseId] || {}), [event.partition.id]: event.attemptId };
    }
    const total = event.partition.total ?? base.phases[phaseId].total;
    base.phases[phaseId] = summarizePartitionPhase(phaseId, statuses, total);
    return base;
  }

  if (event.status === 'running' || event.status === 'completed' || event.status === 'failed') {
    const phase = base.phases[phaseId];
    const hintedTotal = Number(event.progress?.total ?? event.partition?.total ?? phase.total);
    if (COMPLETE_OPS.has(operationId) && event.status === 'completed') {
      const total = Math.max(phase.total, hintedTotal);
      base.phases[phaseId] = {
        ...phase,
        completed: total,
        total,
        running: 0,
        failed: 0,
        status: 'done',
      };
    } else if (event.status === 'failed') {
      base.phases[phaseId] = { ...phase, total: Math.max(phase.total, hintedTotal), failed: 1, status: 'failed' };
    } else if (phase.status !== 'done') {
      base.phases[phaseId] = {
        ...phase,
        total: Math.max(phase.total, hintedTotal),
        status: 'running',
      };
    }
  }

  return base;
}

export function topicDiscoverySteps(
  progress: TopicDiscoveryProgress | undefined,
  overview?: { status?: string; total_topics?: number | null },
  currentStageStatus?: VisualStatus,
): TopicDiscoveryStepView[] {
  const phases = progress?.phases ?? emptyProgress('').phases;
  // A prior published manifest may still report completed while a new topic
  // discovery step is already emitting SSE. Its snapshot must not hide the
  // current step's partial execution progress.
  const hasPartialExecution = Boolean(progress) && TOPIC_PHASE_ORDER.some(
    (id) => phases[id].status !== 'done',
  ) && TOPIC_PHASE_ORDER.some(
    (id) => phases[id].status !== 'pending' || phases[id].total > 0,
  );
  const overviewDone = currentStageStatus !== 'running' && !hasPartialExecution && (
    overview?.status === 'completed' || overview?.status === 'succeeded'
  );
  const topicCount = overview?.total_topics ?? null;

  return TOPIC_PHASE_ORDER.map((id) => {
    const phase = phases[id];
    let status = phase.status;
    let completed = phase.completed;
    let total: number | null = phase.total || null;
    if (overviewDone) {
      status = 'done';
      if (id === 'topics' && topicCount != null) {
        completed = topicCount;
        total = topicCount;
      } else if (total && total > 0) {
        completed = total;
      } else {
        completed = 0;
        total = null;
      }
    } else if (id === 'topics' && status === 'done' && topicCount != null) {
      completed = topicCount;
      total = topicCount;
    }
    return {
      key: id,
      label: PHASE_LABEL[id],
      completed,
      total,
      status,
      summary: progressSummary(status, completed, total, {
        running: phase.running,
        failed: phase.failed,
        pending: Math.max(0, (total || 0) - completed - phase.running - phase.failed),
      }),
    };
  });
}

export function progressSummary(
  status: VisualStatus,
  completed: number,
  total: number | null,
  counts?: { running?: number; failed?: number; pending?: number; stale?: number },
) {
  if (status === 'done' || (total != null && total > 0 && completed === total && (counts?.failed || 0) === 0)) {
    return '全部完成';
  }
  const notes = [
    counts?.failed ? `${counts.failed} 失败` : '',
    counts?.running ? `${counts.running} 执行中` : '',
    counts?.stale ? `${counts.stale} 待更新` : '',
    counts?.pending ? `${counts.pending} 未开始` : '',
  ].filter(Boolean);
  if (notes.length) {
    return notes.join(' · ');
  }
  if (status === 'running') {
    return '执行中';
  }
  if (status === 'failed') {
    return '失败';
  }
  return '未开始';
}

function emptyPhase(id: TopicPhaseId): TopicPhaseState {
  return {
    id,
    label: PHASE_LABEL[id],
    completed: 0,
    total: 0,
    running: 0,
    failed: 0,
    status: 'pending',
  };
}

function emptyProgress(stepId: string): TopicDiscoveryProgress {
  return {
    stepId,
    partitions: {},
    attempts: {},
    phases: {
      entities: emptyPhase('entities'),
      semantic: emptyPhase('semantic'),
      topics: emptyPhase('topics'),
    },
  };
}

function cloneProgress(progress: TopicDiscoveryProgress): TopicDiscoveryProgress {
  return {
    stepId: progress.stepId,
    partitions: {
      entities: { ...progress.partitions.entities },
      semantic: { ...progress.partitions.semantic },
      topics: { ...progress.partitions.topics },
    },
    attempts: {
      entities: { ...progress.attempts.entities },
      semantic: { ...progress.attempts.semantic },
      topics: { ...progress.attempts.topics },
    },
    phases: {
      entities: { ...progress.phases.entities },
      semantic: { ...progress.phases.semantic },
      topics: { ...progress.phases.topics },
    },
  };
}

function summarizePartitionPhase(
  id: TopicPhaseId,
  statuses: Record<string, PartitionStatus>,
  total: number,
): TopicPhaseState {
  let completed = 0;
  let running = 0;
  let failed = 0;
  for (const status of Object.values(statuses)) {
    if (status === 'completed') completed += 1;
    else if (status === 'running') running += 1;
    else if (status === 'failed') failed += 1;
  }
  const resolvedTotal = Math.max(total, Object.keys(statuses).length);
  let status: VisualStatus = 'pending';
  if (failed && !running && completed + failed >= resolvedTotal) status = 'failed';
  else if (running || (completed > 0 && completed < resolvedTotal)) status = 'running';
  else if (resolvedTotal > 0 && completed >= resolvedTotal) status = 'done';
  return {
    id,
    label: PHASE_LABEL[id],
    completed,
    total: resolvedTotal,
    running,
    failed,
    status,
  };
}

function completeEarlierPhases(progress: TopicDiscoveryProgress, active: TopicPhaseId) {
  const activeIndex = TOPIC_PHASE_ORDER.indexOf(active);
  for (const id of TOPIC_PHASE_ORDER.slice(0, activeIndex)) {
    const phase = progress.phases[id];
    if (phase.status === 'done') continue;
    progress.phases[id] = {
      ...phase,
      completed: phase.total,
      running: 0,
      status: 'done',
    };
  }
}

function completeAllPhases(progress: TopicDiscoveryProgress): TopicDiscoveryProgress {
  const next = cloneProgress(progress);
  for (const id of TOPIC_PHASE_ORDER) {
    const phase = next.phases[id];
    next.phases[id] = {
      ...phase,
      completed: phase.total,
      running: 0,
      status: 'done',
    };
  }
  return next;
}

function isPartitionStatus(status: string | undefined): status is PartitionStatus {
  return status === 'running' || status === 'completed' || status === 'failed' || status === 'canceled';
}
