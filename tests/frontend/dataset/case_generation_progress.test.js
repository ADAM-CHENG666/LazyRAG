import { describe, expect, it } from 'vitest';

import {
  applyCaseGenerationPartitionEvent,
  caseGenerationDisplayStep,
  caseGenerationSteps,
  overlayCaseProgress,
  shouldReconcileCaseExecution,
} from '../../../frontend/src/modules/selfEvolution/components/workbench/dataset/caseGenerationProgress.ts';

function event(overrides = {}) {
  return {
    event: 'dataset.qaplan_spec',
    stage: 'dataset.case_generation',
    operationId: 'dataset.qaplan_spec',
    attemptId: 'attempt-1',
    stepId: 'step-cases-1',
    partition: { id: 'case-001', total: 2 },
    status: 'running',
    ...overrides,
  };
}

function apply(progress, overrides = {}) {
  return applyCaseGenerationPartitionEvent(progress, event(overrides)).progress;
}

describe('case generation SSE progress', () => {
  it('aggregates partition events into the three overview steps and loaded case rows', () => {
    let progress;
    progress = apply(progress);
    progress = apply(progress, {
      partition: { id: 'case-002', total: 2 },
      status: 'completed',
    });
    progress = apply(progress, {
      event: 'dataset.generate_case',
      operationId: 'dataset.generate_case',
      partition: { id: 'case-002', total: 2 },
      status: 'running',
    });

    const steps = caseGenerationSteps(progress);
    expect(steps.map((step) => [step.key, step.completed, step.total, step.status])).toEqual([
      ['plan', 1, 2, 'running'],
      ['generate', 0, 2, 'running'],
      ['grading', 0, 0, 'pending'],
    ]);

    expect(overlayCaseProgress([
      { case_id: 'case-001', stages: { plan: 'pending', generate: 'pending', grading: 'pending' } },
      { case_id: 'case-002', stages: { plan: 'pending', generate: 'pending', grading: 'pending' } },
    ], progress)).toEqual([
      { case_id: 'case-001', stages: { plan: 'running', generate: 'pending', grading: 'pending' } },
      { case_id: 'case-002', stages: { plan: 'completed', generate: 'running', grading: 'pending' } },
    ]);
  });

  it('uses the newest event for a retried partition and resets on a new step', () => {
    let progress = apply(undefined, { status: 'failed' });
    progress = apply(progress, { attemptId: 'attempt-2', status: 'running' });
    expect(caseGenerationSteps(progress)[0]).toMatchObject({ status: 'running', failed: 0, running: 1 });

    const switched = applyCaseGenerationPartitionEvent(progress, event({
      stepId: 'step-cases-2',
      partition: { id: 'case-003', total: 1 },
      status: 'completed',
    }));
    expect(switched.shouldRefreshBaseline).toBe(true);
    expect(caseGenerationSteps(switched.progress)[0]).toMatchObject({ completed: 1, total: 1, status: 'done' });
    expect(overlayCaseProgress([
      { case_id: 'case-001', stages: { plan: 'failed', generate: 'pending', grading: 'pending' } },
      { case_id: 'case-003', stages: { plan: 'pending', generate: 'pending', grading: 'pending' } },
    ], switched.progress)).toEqual([
      { case_id: 'case-001', stages: { plan: 'failed', generate: 'pending', grading: 'pending' } },
      { case_id: 'case-003', stages: { plan: 'completed', generate: 'pending', grading: 'pending' } },
    ]);
  });

  it('does not let an earlier attempt overwrite a retry or a completed API snapshot', () => {
    let progress = apply(undefined, { status: 'failed' });
    progress = apply(progress, { attemptId: 'attempt-2', status: 'running' });
    progress = apply(progress, { attemptId: 'attempt-1', status: 'completed' });

    expect(caseGenerationSteps(progress)[0]).toMatchObject({ status: 'running', completed: 0, running: 1 });
    expect(overlayCaseProgress([
      { case_id: 'case-001', stages: { plan: 'completed', generate: 'pending', grading: 'pending' } },
    ], progress)).toEqual([
      { case_id: 'case-001', stages: { plan: 'completed', generate: 'pending', grading: 'pending' } },
    ]);
  });

  it('keeps imported completed baseline while live SSE only covers generated cases', () => {
    let progress;
    progress = apply(progress, {
      partition: { id: 'case-0011', total: 20 },
      status: 'running',
    });

    const displayed = caseGenerationDisplayStep(
      caseGenerationSteps(progress)[0],
      {
        status: 'pending',
        completed: 10,
        total: 20,
        status_counts: { completed: 10, pending: 10, running: 0, failed: 0, canceled: 0 },
      },
    );

    expect(displayed).toMatchObject({
      completed: 10,
      running: 1,
      pending: 9,
      total: 20,
      status: 'running',
    });
  });

  it('ignores a previous-run all-completed baseline until overview refreshes', () => {
    let progress;
    progress = apply(progress, {
      partition: { id: 'case-0011', total: 20 },
      status: 'running',
    });

    const displayed = caseGenerationDisplayStep(
      caseGenerationSteps(progress)[0],
      {
        status: 'completed',
        completed: 20,
        total: 20,
        status_counts: { completed: 20, pending: 0, running: 0, failed: 0, canceled: 0 },
      },
    );

    expect(displayed).toMatchObject({
      completed: 0,
      running: 1,
      pending: 19,
      total: 20,
      status: 'running',
    });
  });

  it('keeps imported completed when a previous-run all-completed baseline is discarded', () => {
    let progress;
    progress = apply(progress, {
      partition: { id: 'case-0011', total: 20 },
      status: 'running',
    });

    const displayed = caseGenerationDisplayStep(
      caseGenerationSteps(progress)[0],
      {
        status: 'completed',
        completed: 20,
        total: 20,
        status_counts: { completed: 20, pending: 0, running: 0, failed: 0, canceled: 0 },
      },
      8,
    );

    expect(displayed).toMatchObject({
      completed: 8,
      running: 1,
      pending: 11,
      total: 20,
      status: 'running',
    });
  });

  it('keeps imported completed when live execution starts and overview baseline was cleared', () => {
    let progress;
    progress = apply(progress, {
      event: 'dataset.generate_case',
      operationId: 'dataset.generate_case',
      partition: { id: 'case-0011', total: 20 },
      status: 'running',
    });

    const displayed = caseGenerationDisplayStep(
      caseGenerationSteps(progress)[1],
      {
        status: 'pending',
        completed: 0,
        total: 20,
        status_counts: { completed: 0, pending: 20, running: 0, failed: 0, canceled: 0 },
      },
      8,
    );

    expect(displayed).toMatchObject({
      completed: 8,
      running: 1,
      pending: 11,
      total: 20,
      status: 'running',
    });
  });

  it('uses overview baseline alone when there is no live SSE yet', () => {
    const displayed = caseGenerationDisplayStep(
      undefined,
      {
        status: 'pending',
        completed: 10,
        total: 20,
        status_counts: { completed: 10, pending: 10, running: 0, failed: 0, canceled: 0 },
      },
    );

    expect(displayed).toMatchObject({
      completed: 10,
      pending: 10,
      total: 20,
      status: 'pending',
    });
  });

  it('keeps imported completed on a cleared overview before that stage has live SSE', () => {
    const displayed = caseGenerationDisplayStep(
      undefined,
      {
        status: 'pending',
        completed: 0,
        total: 20,
        status_counts: { completed: 0, pending: 20, running: 0, failed: 0, canceled: 0 },
      },
      8,
    );

    expect(displayed).toMatchObject({
      completed: 8,
      pending: 12,
      total: 20,
      status: 'pending',
    });
  });

  it('clears execution progress only after the terminal-triggered overview and list requests both finish', () => {
    const base = {
      reconciliationToken: 2,
      lastReconciledToken: 1,
      expectedOverviewToken: 7,
      expectedListToken: 4,
      overviewExecutionRevision: 'exec-2',
      listExecutionRevision: 'exec-2',
    };

    expect(shouldReconcileCaseExecution({
      ...base,
      loadedOverviewToken: 6,
      loadedListToken: 4,
    })).toBe(false);
    expect(shouldReconcileCaseExecution({
      ...base,
      loadedOverviewToken: 7,
      loadedListToken: 3,
    })).toBe(false);
    expect(shouldReconcileCaseExecution({
      ...base,
      loadedOverviewToken: 7,
      loadedListToken: 4,
    })).toBe(true);
    expect(shouldReconcileCaseExecution({
      ...base,
      lastReconciledToken: 2,
      loadedOverviewToken: 7,
      loadedListToken: 4,
    })).toBe(false);
  });
});
