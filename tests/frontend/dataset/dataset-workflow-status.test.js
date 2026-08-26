import { describe, expect, it } from 'vitest';
import {
  deriveDatasetTopStatus,
  datasetWorkflowStepFromSteps,
  deriveDatasetView,
  getDatasetCheckpointWaitingStep,
  mergeWorkflowStepStatus,
  terminalDatasetWorkflowStatus,
  toDatasetNavStatus,
  toThreadEventStage,
} from '../../../frontend/src/modules/selfEvolution/shared/datasetWorkflowStatus.ts';

function steps(items) {
  return items.map((item, index) => ({
    ...item,
    stage: item.stage,
    status: item.status,
    orderIndex: index,
    stepId: item.stepId || `step-${index}`,
  }));
}

describe('deriveDatasetView', () => {
  it('uses the latest started dataset stage as 数据集生成', () => {
    const view = deriveDatasetView(steps([
      { stage: 'dataset.material_preparation', status: 'completed' },
      { stage: 'dataset.topic_discovery', status: 'running' },
      { stage: 'dataset.case_generation', status: 'pending' },
      { stage: 'eval', status: 'pending' },
    ]));
    expect(view.representative?.stage).toBe('dataset.topic_discovery');
    expect(view.topStatus).toBe('running');
    expect(view.canContinue).toBe(false);
    expect(view.subStatuses).toEqual({
      materials: 'completed',
      topics: 'running',
      cases: 'pending',
    });
  });

  it('uses the latest row when the same dataset stage appears twice', () => {
    const view = deriveDatasetView(steps([
      { stage: 'dataset.material_preparation', status: 'completed' },
      { stage: 'dataset.topic_discovery', status: 'pending' },
      { stage: 'dataset.case_generation', status: 'pending' },
      { stage: 'dataset.material_preparation', status: 'running' },
    ]));
    expect(view.representative?.stage).toBe('dataset.material_preparation');
    expect(view.topStatus).toBe('running');
    expect(view.subStatuses.materials).toBe('running');
  });

  it('prefers a running earlier stage over a completed later stage (re-trigger)', () => {
    const view = deriveDatasetView(steps([
      { stage: 'dataset.material_preparation', status: 'running' },
      { stage: 'dataset.topic_discovery', status: 'completed' },
      { stage: 'dataset.case_generation', status: 'pending' },
    ]));
    expect(view.topStatus).toBe('running');
    expect(view.canContinue).toBe(false);
  });

  it('keeps the real completed status when later stages are pending without a checkpoint', () => {
    // Do not invent "running" — that desyncs the top bar from the continue button.
    const view = deriveDatasetView(steps([
      { stage: 'dataset.material_preparation', status: 'completed' },
      { stage: 'dataset.topic_discovery', status: 'completed' },
      { stage: 'dataset.case_generation', status: 'pending' },
    ]));
    expect(view.representative?.stage).toBe('dataset.topic_discovery');
    expect(view.topStatus).toBe('completed');
    expect(view.canContinue).toBe(false);
  });

  it('shows completed only when all dataset stages are done', () => {
    const view = deriveDatasetView(steps([
      { stage: 'dataset.material_preparation', status: 'completed' },
      { stage: 'dataset.topic_discovery', status: 'completed' },
      { stage: 'dataset.case_generation', status: 'completed' },
    ]));
    expect(view.representative?.stage).toBe('dataset.case_generation');
    expect(view.topStatus).toBe('completed');
  });

  it('keeps a paused materials stage while later stages are pending', () => {
    const view = deriveDatasetView(steps([
      { stage: 'dataset.material_preparation', status: 'paused' },
      { stage: 'dataset.topic_discovery', status: 'pending' },
      { stage: 'dataset.case_generation', status: 'pending' },
    ]));
    expect(view.topStatus).toBe('paused');
  });

  it('aligns top status and continue when materials waits at a checkpoint', () => {
    const items = steps([
      {
        stage: 'dataset.material_preparation',
        status: 'completed',
        nextStepRunId: 'topic-step-1',
      },
      { stage: 'dataset.topic_discovery', status: 'pending' },
      { stage: 'dataset.case_generation', status: 'pending' },
    ]);

    const view = deriveDatasetView(items);
    expect(view.canContinue).toBe(true);
    expect(view.continueStepId).toBe('topic-step-1');
    expect(view.topStatus).toBe('completed');
    expect(view.suggestedTab).toBe('materials');
    expect(getDatasetCheckpointWaitingStep(items)?.stage).toBe('dataset.material_preparation');
  });

  it('after materials re-apply completes, top stays completed and next-step stays available', () => {
    const items = steps([
      {
        stage: 'dataset.material_preparation',
        status: 'completed',
        nextStepRunId: 'topics-old',
      },
      { stage: 'dataset.topic_discovery', status: 'pending' },
      { stage: 'dataset.case_generation', status: 'pending' },
      {
        stage: 'dataset.material_preparation',
        status: 'completed',
        nextStepRunId: 'topics-new',
      },
    ]);

    const view = deriveDatasetView(items);
    expect(view.canContinue).toBe(true);
    expect(view.continueStepId).toBe('topics-new');
    expect(view.topStatus).toBe('completed');
    expect(view.subStatuses.materials).toBe('completed');
  });

  it('does not treat a still-running materials re-run as a continue checkpoint', () => {
    const view = deriveDatasetView(steps([
      { stage: 'dataset.topic_discovery', status: 'pending' },
      {
        stage: 'dataset.material_preparation',
        status: 'running',
        nextStepRunId: 'topics-new',
      },
    ]));

    expect(view.canContinue).toBe(false);
    expect(view.topStatus).toBe('running');
  });

  it('suggests the active step tab when provided', () => {
    const view = deriveDatasetView(
      steps([
        { stage: 'dataset.material_preparation', status: 'completed', stepId: 'm1' },
        { stage: 'dataset.topic_discovery', status: 'running', stepId: 't1' },
      ]),
      't1',
    );
    expect(view.suggestedTab).toBe('topics');
    expect(view.activeStepId).toBe('t1');
  });
});

describe('dataset top status', () => {
  it('stays waiting at a materials checkpoint instead of inventing running', () => {
    const items = steps([
      {
        stage: 'dataset.material_preparation',
        status: 'completed',
        nextStepRunId: 'topic-step-1',
      },
      { stage: 'dataset.topic_discovery', status: 'pending' },
      { stage: 'dataset.case_generation', status: 'pending' },
    ]);

    expect(deriveDatasetTopStatus(items)).toBe('waiting');
  });

  it('does not invent running after topic discovery finishes while cases are still pending', () => {
    const items = steps([
      { stage: 'dataset.material_preparation', status: 'completed' },
      {
        stage: 'dataset.topic_discovery',
        status: 'completed',
        nextStepRunId: 'case-step-1',
      },
      { stage: 'dataset.case_generation', status: 'pending' },
    ]);

    expect(deriveDatasetTopStatus(items)).toBe('waiting');
  });

  it('does not invent running when a finished sub-stage has no next_step_run_id yet', () => {
    const items = steps([
      { stage: 'dataset.material_preparation', status: 'completed' },
      { stage: 'dataset.topic_discovery', status: 'completed' },
      { stage: 'dataset.case_generation', status: 'pending' },
    ]);

    expect(deriveDatasetTopStatus(items)).not.toBe('running');
    expect(deriveDatasetTopStatus(items)).toBe('paused');
  });

  it('marks the top bar done when all three dataset stages are completed', () => {
    const items = steps([
      { stage: 'dataset.material_preparation', status: 'completed' },
      { stage: 'dataset.topic_discovery', status: 'completed' },
      { stage: 'dataset.case_generation', status: 'completed' },
    ]);

    expect(deriveDatasetTopStatus(items)).toBe('done');
  });

  it('keeps pausing and cancelling as running until the terminal status lands', () => {
    expect(deriveDatasetTopStatus(steps([
      { stage: 'dataset.material_preparation', status: 'pausing' },
      { stage: 'dataset.topic_discovery', status: 'pending' },
    ]))).toBe('running');
    expect(deriveDatasetTopStatus(steps([
      { stage: 'dataset.topic_discovery', status: 'cancelling' },
    ]))).toBe('running');
  });

  it('surfaces a failed dataset stage on the top bar', () => {
    expect(deriveDatasetTopStatus(steps([
      { stage: 'dataset.material_preparation', status: 'completed' },
      { stage: 'dataset.topic_discovery', status: 'failed' },
      { stage: 'dataset.case_generation', status: 'pending' },
    ]))).toBe('failed');
  });

  it('treats legacy partial_failed step rows as completed for top-bar aggregation', () => {
    expect(deriveDatasetTopStatus(steps([
      { stage: 'dataset.material_preparation', status: 'completed' },
      { stage: 'dataset.topic_discovery', status: 'partial_failed' },
      { stage: 'dataset.case_generation', status: 'completed' },
    ]))).toBe('done');
  });
});

describe('datasetWorkflowStepFromSteps compat', () => {
  it('matches deriveDatasetView.representative', () => {
    const items = steps([
      { stage: 'dataset.material_preparation', status: 'completed', nextStepRunId: 't1' },
      { stage: 'dataset.topic_discovery', status: 'pending' },
    ]);
    expect(datasetWorkflowStepFromSteps(items)?.status)
      .toBe(deriveDatasetView(items).representative?.status);
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

describe('terminal dataset status', () => {
  it('keeps the dataset workflow paused when case generation waits for a plan adjustment', () => {
    expect(terminalDatasetWorkflowStatus('paused')).toBe('paused');
  });
});

describe('dataset sub-nav status', () => {
  it('does not surface partition partial failure on the sub-nav', () => {
    expect(toDatasetNavStatus('partial_failed')).toBe('done');
    expect(toDatasetNavStatus('completed')).toBe('done');
    expect(toDatasetNavStatus('failed')).toBe('failed');
  });
});
