import { describe, expect, it } from 'vitest';
import {
  datasetWorkflowStepFromSteps,
  mergeWorkflowStepStatus,
  toThreadEventStage,
} from '../../../frontend/src/modules/selfEvolution/shared/datasetWorkflowStatus.ts';

function steps(items) {
  return items.map((item, index) => ({
    stage: item.stage,
    status: item.status,
    orderIndex: index,
    stepId: `step-${index}`,
  }));
}

describe('dataset workflow status from /steps', () => {
  it('uses the latest started dataset stage as 数据集生成', () => {
    const chosen = datasetWorkflowStepFromSteps(steps([
      { stage: 'dataset.material_preparation', status: 'completed' },
      { stage: 'dataset.topic_discovery', status: 'running' },
      { stage: 'dataset.case_generation', status: 'pending' },
      { stage: 'eval', status: 'pending' },
    ]));
    expect(chosen?.stage).toBe('dataset.topic_discovery');
    expect(chosen?.status).toBe('running');
  });

  it('uses the latest row when the same dataset stage appears twice', () => {
    const chosen = datasetWorkflowStepFromSteps(steps([
      { stage: 'dataset.material_preparation', status: 'completed' },
      { stage: 'dataset.topic_discovery', status: 'pending' },
      { stage: 'dataset.case_generation', status: 'pending' },
      { stage: 'dataset.material_preparation', status: 'running' },
    ]));
    expect(chosen?.stage).toBe('dataset.material_preparation');
    expect(chosen?.status).toBe('running');
  });

  it('prefers a running earlier stage over a completed later stage (re-trigger scenario)', () => {
    const chosen = datasetWorkflowStepFromSteps(steps([
      { stage: 'dataset.material_preparation', status: 'running' },
      { stage: 'dataset.topic_discovery', status: 'completed' },
      { stage: 'dataset.case_generation', status: 'pending' },
    ]));
    expect(chosen?.stage).toBe('dataset.material_preparation');
    expect(chosen?.status).toBe('running');
  });

  it('shows running when a middle stage is completed but later stages are still pending', () => {
    const chosen = datasetWorkflowStepFromSteps(steps([
      { stage: 'dataset.material_preparation', status: 'completed' },
      { stage: 'dataset.topic_discovery', status: 'completed' },
      { stage: 'dataset.case_generation', status: 'pending' },
    ]));
    expect(chosen?.stage).toBe('dataset.topic_discovery');
    expect(chosen?.status).toBe('running');
  });

  it('shows completed only when all dataset stages are done', () => {
    const chosen = datasetWorkflowStepFromSteps(steps([
      { stage: 'dataset.material_preparation', status: 'completed' },
      { stage: 'dataset.topic_discovery', status: 'completed' },
      { stage: 'dataset.case_generation', status: 'completed' },
    ]));
    expect(chosen?.stage).toBe('dataset.case_generation');
    expect(chosen?.status).toBe('completed');
  });

  it('keeps a completed materials checkpoint when later dataset stages are still pending', () => {
    const chosen = datasetWorkflowStepFromSteps(steps([
      { stage: 'dataset.material_preparation', status: 'paused' },
      { stage: 'dataset.topic_discovery', status: 'pending' },
      { stage: 'dataset.case_generation', status: 'pending' },
    ]));
    expect(chosen?.stage).toBe('dataset.material_preparation');
    expect(chosen?.status).toBe('paused');
  });
});

describe('dataset flow stage names', () => {
  it('maps each dataset /steps stage onto the dataset workflow step', () => {
    expect(toThreadEventStage('dataset.material_preparation')).toBe('dataset');
    expect(toThreadEventStage('dataset.topic_discovery')).toBe('dataset');
    expect(toThreadEventStage('dataset.case_generation')).toBe('dataset');
    expect(toThreadEventStage('dataset')).toBe('dataset');
    expect(toThreadEventStage('eval')).toBe('eval');
  });
});

describe('workflow step status merge', () => {
  it('lets /steps replace a stale SSE terminal status', () => {
    const status = mergeWorkflowStepStatus(
      { dataset: 'done' },
      { dataset: 'running' },
    );
    expect(status.dataset).toBe('running');
  });
});
