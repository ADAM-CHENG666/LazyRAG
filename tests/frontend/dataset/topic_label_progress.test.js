import { describe, expect, it } from 'vitest';
import {
  applyTopicLabelPartitionEvent,
  topicDiscoverySteps,
} from '../../../frontend/src/modules/selfEvolution/components/workbench/dataset/topicLabelProgress.ts';

function event(overrides = {}) {
  return {
    event: 'dataset.extract_chunk_entities',
    stage: 'dataset.topic_discovery',
    operationId: 'dataset.extract_chunk_entities',
    attemptId: 'attempt-1',
    stepId: 'thread-1:dataset.topic_discovery:1',
    partition: { id: 'chunk-1', total: 3 },
    status: 'running',
    ...overrides,
  };
}

describe('topic discovery 3-phase progress', () => {
  it('tracks entity extraction partitions', () => {
    let progress;
    progress = applyTopicLabelPartitionEvent(progress, event());
    progress = applyTopicLabelPartitionEvent(progress, event({
      partition: { id: 'chunk-2', total: 3 },
      status: 'completed',
    }));
    progress = applyTopicLabelPartitionEvent(progress, event({ status: 'completed' }));

    const steps = topicDiscoverySteps(progress);
    expect(steps[0].label).toBe('实体提取');
    expect(steps[0].completed).toBe(2);
    expect(steps[0].total).toBe(3);
    expect(steps[0].status).toBe('running');
    expect(steps[0].summary).toBe('1 未开始');
    expect(steps[1].status).toBe('pending');
    expect(steps[2].status).toBe('pending');
  });

  it('starts semantic discovery after clustering and tracks cluster labels', () => {
    let progress;
    progress = applyTopicLabelPartitionEvent(progress, event({
      partition: { id: 'chunk-1', total: 1 },
      status: 'completed',
    }));
    progress = applyTopicLabelPartitionEvent(progress, event({
      event: 'dataset.cluster_embeddings',
      operationId: 'dataset.cluster_embeddings',
      partition: undefined,
      status: 'completed',
      progress: { total: 4 },
    }));
    progress = applyTopicLabelPartitionEvent(progress, event({
      event: 'dataset.label_embedding_cluster',
      operationId: 'dataset.label_embedding_cluster',
      partition: { id: 'cluster-1', total: 4 },
      status: 'completed',
    }));

    const steps = topicDiscoverySteps(progress);
    expect(steps[0].status).toBe('done');
    expect(steps[0].summary).toBe('全部完成');
    expect(steps[1].label).toBe('语义发现');
    expect(steps[1].completed).toBe(1);
    expect(steps[1].total).toBe(4);
    expect(steps[1].status).toBe('running');
  });

  it('marks topic generation complete from the merge operation', () => {
    let progress;
    progress = applyTopicLabelPartitionEvent(progress, event({
      event: 'dataset.topic_manifest',
      operationId: 'dataset.topic_manifest',
      partition: undefined,
      status: 'completed',
    }));

    const steps = topicDiscoverySteps(progress, { status: 'completed', total_topics: 8 });
    expect(steps.every((step) => step.status === 'done')).toBe(true);
    expect(steps[2].label).toBe('主题生成');
    expect(steps[2].completed).toBe(8);
    expect(steps[2].total).toBe(8);
    expect(steps[2].summary).toBe('全部完成');
  });

  it('keeps a new SSE execution round visible when overview still has the prior completed snapshot', () => {
    const progress = applyTopicLabelPartitionEvent(undefined, event({
      stepId: 'thread-1:dataset.topic_discovery:2',
      status: 'running',
    }));

    const steps = topicDiscoverySteps(progress, { status: 'completed', total_topics: 8 });
    expect(steps[0]).toMatchObject({ completed: 0, total: 3, status: 'running' });
    expect(steps[1].status).toBe('pending');
    expect(steps[2].status).toBe('pending');
  });

  it('never invents a 1/1 total when a later operation arrives before partition events', () => {
    const progress = applyTopicLabelPartitionEvent(undefined, event({
      event: 'dataset.cluster_embeddings',
      operationId: 'dataset.cluster_embeddings',
      partition: undefined,
      status: 'running',
    }));

    const steps = topicDiscoverySteps(progress);
    expect(steps[0]).toMatchObject({ status: 'done', completed: 0, total: null });
    expect(steps[1]).toMatchObject({ status: 'running', completed: 0, total: null });
  });

  it('does not invent entity or semantic totals from a completed overview', () => {
    const steps = topicDiscoverySteps(undefined, { status: 'completed', total_topics: 8 });

    expect(steps[0]).toMatchObject({ completed: 0, total: null, status: 'done' });
    expect(steps[1]).toMatchObject({ completed: 0, total: null, status: 'done' });
    expect(steps[2]).toMatchObject({ completed: 8, total: 8, status: 'done' });
  });

  it('does not flash the previous completed overview while /steps says this stage is running', () => {
    const steps = topicDiscoverySteps(
      undefined,
      { status: 'completed', total_topics: 8 },
      'running',
    );

    expect(steps.every((step) => step.status === 'pending')).toBe(true);
    expect(steps.every((step) => step.completed === 0)).toBe(true);
  });

  it('does not let an earlier attempt overwrite a retried partition', () => {
    let progress = applyTopicLabelPartitionEvent(undefined, event({ status: 'failed' }));
    progress = applyTopicLabelPartitionEvent(progress, event({ attemptId: 'attempt-2', status: 'running' }));
    progress = applyTopicLabelPartitionEvent(progress, event({ attemptId: 'attempt-1', status: 'completed' }));

    expect(topicDiscoverySteps(progress)[0]).toMatchObject({ status: 'running', completed: 0 });
  });

  it('keeps partition failures visible after step.finish instead of claiming all done', () => {
    let progress = applyTopicLabelPartitionEvent(undefined, event({
      partition: { id: 'chunk-1', total: 2 },
      status: 'completed',
    }));
    progress = applyTopicLabelPartitionEvent(progress, event({
      partition: { id: 'chunk-2', total: 2 },
      attemptId: 'attempt-2',
      status: 'failed',
    }));
    progress = applyTopicLabelPartitionEvent(progress, {
      event: 'step.finish',
      stage: 'dataset.topic_discovery',
      stepId: 'thread-1:dataset.topic_discovery:1',
    });

    const steps = topicDiscoverySteps(progress);
    expect(steps[0].status).toBe('partial');
    expect(steps[0].summary).toContain('失败');
    expect(steps[0].summary).not.toBe('全部完成');
  });

  it('does not blank later phases after step.finish before overview refresh returns', () => {
    let progress = applyTopicLabelPartitionEvent(undefined, event({
      partition: { id: 'chunk-1', total: 2 },
      status: 'completed',
    }));
    progress = applyTopicLabelPartitionEvent(progress, {
      event: 'step.finish',
      stage: 'dataset.topic_discovery',
      stepId: 'thread-1:dataset.topic_discovery:1',
    });

    const steps = topicDiscoverySteps(progress);
    expect(steps[0].status).toBe('done');
    expect(steps[1].status).toBe('done');
    expect(steps[2].status).toBe('done');
  });
});
