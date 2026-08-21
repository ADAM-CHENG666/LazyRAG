import { describe, expect, it } from 'vitest';
import {
  draftAffectsTab,
  executionImpactTabs,
  resolveRevisionRefreshAction,
  shouldShowGenerationPlanPause,
  shouldResumeDatasetStream,
  shouldPublishRefresh,
} from '../../../frontend/src/modules/selfEvolution/components/workbench/dataset/datasetRefresh.ts';
import { readFrontendFile } from '../setup.js';

describe('dataset refresh policy', () => {
  it('detects revision transitions', () => {
    expect(shouldPublishRefresh(undefined, null)).toBe(false);
    expect(shouldPublishRefresh(null, 'topics-v1')).toBe(true);
    expect(shouldPublishRefresh('topics-v1', 'topics-v2')).toBe(true);
  });

  it('auto-refreshes the active tab when revision changes and no draft blocks it', () => {
    expect(resolveRevisionRefreshAction('topics', 'topics', null, 'topics-v1', undefined)).toBe('auto');
  });

  it('shows stale banner when a draft would be lost', () => {
    expect(
      resolveRevisionRefreshAction('materials', 'materials', 'm-v1', 'm-v2', {
        kind: 'materials-config',
        revision: 'm-v1',
        changes: {},
      }),
    ).toBe('stale');
  });

  it('defers refresh for inactive tabs until the user switches', () => {
    expect(resolveRevisionRefreshAction('topics', 'materials', null, 'topics-v1', undefined)).toBe('pending');
  });

  it('maps draft kinds to tabs', () => {
    expect(draftAffectsTab({ kind: 'topic-names', revision: 't-v1', names: {} }, 'topics')).toBe(true);
    expect(draftAffectsTab({ kind: 'topic-names', revision: 't-v1', names: {} }, 'materials')).toBe(false);
  });

  it('clears execution progress for the changed stage and every downstream stage', () => {
    expect(executionImpactTabs('materials-config')).toEqual(['materials', 'topics', 'cases']);
    expect(executionImpactTabs('chunk-selection')).toEqual(['materials', 'topics', 'cases']);
    expect(executionImpactTabs('topic-names')).toEqual(['topics', 'cases']);
    expect(executionImpactTabs('generation-plan')).toEqual(['cases']);
  });

  it('shows the generation-plan pause notice only while case generation is paused', () => {
    expect(shouldShowGenerationPlanPause('paused')).toBe(true);
    expect(shouldShowGenerationPlanPause('running')).toBe(false);
    expect(shouldShowGenerationPlanPause(undefined)).toBe(false);
  });

  it('reopens the Dataset stream once when an external action starts a new execution round', () => {
    expect(shouldResumeDatasetStream(3, 3)).toBe(false);
    expect(shouldResumeDatasetStream(3, 4)).toBe(true);
  });

  it('notifies the Dataset workspace when the continue request is accepted', () => {
    const source = readFrontendFile(
      'src/modules/selfEvolution/hooks/useSelfEvolutionPageController.tsx',
    );
    const start = source.indexOf('const continueThreadExecution = async');
    const end = source.indexOf('const onContinueCheckpoint', start);
    const continuation = source.slice(start, end);
    const requestAccepted = continuation.indexOf('/continue`');
    const notification = continuation.indexOf('setDatasetExecutionResumeToken');

    expect(requestAccepted).toBeGreaterThan(-1);
    expect(notification).toBeGreaterThan(requestAccepted);
  });

  it('notifies the Dataset workspace when a message starts the next step', () => {
    const source = readFrontendFile(
      'src/modules/selfEvolution/hooks/useSelfEvolutionPageController.tsx',
    );
    const start = source.indexOf('const onSend = async');
    const end = source.indexOf('const continueThreadExecution = async', start);
    const onSend = source.slice(start, end);

    expect(onSend.match(/subscribePendingNextStepRun\(/g)?.length).toBe(2);
    expect(onSend.match(/setDatasetExecutionResumeToken/g)?.length).toBe(2);
  });
});
