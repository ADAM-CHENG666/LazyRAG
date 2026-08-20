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
    progress = applyTopicLabelPartitionEvent(progress, event({ status: 'completed' }));
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
});
