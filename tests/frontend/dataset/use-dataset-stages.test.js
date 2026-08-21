import { describe, expect, it } from 'vitest';

import {
  activeDatasetTabForThread,
  datasetTabForStage,
  deriveDatasetStageState,
  isCurrentDatasetExecutionEvent,
} from '../../../frontend/src/modules/selfEvolution/components/workbench/dataset/stageState.ts';

describe('deriveDatasetStageState', () => {
  it('keeps paused case generation visible and selected', () => {
    const state = deriveDatasetStageState({
      thread_id: 'thr-a7f9a9d6',
      active_step_id: '',
      items: [
        { step_id: 'm1', stage: 'dataset.material_preparation', status: 'completed' },
        { step_id: 't1', stage: 'dataset.topic_discovery', status: 'completed' },
        { step_id: 'c1', stage: 'dataset.case_generation', status: 'paused' },
      ],
    });

    expect(state.statuses).toEqual({
      materials: 'done',
      topics: 'done',
      cases: 'paused',
    });
    expect(state.activeTab).toBe('cases');
    expect(state.activeStepId).toBe('c1');
  });

  it('exposes the active execution id used to reject events from older rounds', () => {
    const state = deriveDatasetStageState({
      thread_id: 'thr-1',
      active_step_id: 'topic-2',
      items: [
        { step_id: 'topic-1', stage: 'dataset.topic_discovery', status: 'completed' },
        { step_id: 'topic-2', stage: 'dataset.topic_discovery', status: 'running' },
      ],
    });

    expect(state.activeStepId).toBe('topic-2');
    expect(isCurrentDatasetExecutionEvent(state.activeStepId, 'topic-1')).toBe(false);
    expect(isCurrentDatasetExecutionEvent(state.activeStepId, 'topic-2')).toBe(true);
    expect(isCurrentDatasetExecutionEvent(state.activeStepId, '')).toBe(false);
  });

});

describe('Dataset SSE stage mapping', () => {
  it('maps a Dataset SSE event to its navigation substep', () => {
    expect(datasetTabForStage('dataset.topic_discovery')).toBe('topics');
  });
});

describe('Dataset active stage ownership', () => {
  it('does not use the previous Thread active stage while a new Thread is loading', () => {
    expect(activeDatasetTabForThread('thr-new', 'thr-old', 'topics')).toBeUndefined();
    expect(activeDatasetTabForThread('thr-new', 'thr-new', 'topics')).toBe('topics');
  });
});
