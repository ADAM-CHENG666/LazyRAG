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

  it('shows the generation-plan pause notice only on the first-entry quota gate', () => {
    expect(shouldShowGenerationPlanPause('paused', 0, 0)).toBe(true);
    expect(shouldShowGenerationPlanPause('paused', 5, 5)).toBe(true);
    expect(shouldShowGenerationPlanPause('paused', null, 0)).toBe(true);
    expect(shouldShowGenerationPlanPause('paused', 59, 0)).toBe(false);
    expect(shouldShowGenerationPlanPause('paused', 6, 5)).toBe(false);
    expect(shouldShowGenerationPlanPause('running', 0, 0)).toBe(false);
    expect(shouldShowGenerationPlanPause(undefined, 0, 0)).toBe(false);

    const source = readFrontendFile(
      'src/modules/selfEvolution/components/workbench/dataset/CasesStage.tsx',
    );
    expect(source).toContain('overview.data?.stages.generate.completed');
    expect(source).toContain('importedCompleted');
  });

  it('reopens the Dataset stream once when an external action starts a new execution round', () => {
    expect(shouldResumeDatasetStream(3, 3)).toBe(false);
    expect(shouldResumeDatasetStream(3, 4)).toBe(true);
  });

  it('keeps overview reloads independent from list refreshes', () => {
    for (const file of ['MaterialsStage.tsx', 'TopicsStage.tsx', 'CasesStage.tsx']) {
      const source = readFrontendFile(
        `src/modules/selfEvolution/components/workbench/dataset/${file}`,
      );

      expect(source).toContain('fetchOverview,\n    overviewToken,');
      expect(source).not.toContain('refreshToken + overviewToken');
    }
  });

  it('loads topic options only when the generated case plan is visible', () => {
    const source = readFrontendFile(
      'src/modules/selfEvolution/components/workbench/dataset/CaseDetailDrawer.tsx',
    );
    const loadStart = source.indexOf('const load = useCallback');
    const loadEnd = source.indexOf('useEffect(() => {', loadStart);
    const detailLoad = source.slice(loadStart, loadEnd);

    expect(detailLoad).not.toContain('topic-options');
    expect(source).toContain('const loadTopicOptions = useCallback');
    expect(source).toContain('if (stage !== "plan" || !detail || detail.source === "imported") return;');
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

  it('keeps Dataset nav status owned by the Workbench /steps snapshot', () => {
    const controller = readFrontendFile(
      'src/modules/selfEvolution/hooks/useSelfEvolutionPageController.tsx',
    );
    expect(controller).toContain('deriveDatasetView');
    expect(controller).toContain('onDatasetStepsSnapshot:');
    expect(controller).toContain('datasetStageStatuses');
    expect(controller).not.toContain('onDatasetExecutionSettled:');

    const writeApplied = controller.indexOf('onDatasetWriteApplied:');
    const writeBlock = controller.slice(writeApplied, writeApplied + 1200);
    expect(writeBlock).toContain('waitForSubscribableThreadStep');
    expect(writeBlock).toContain('subscribeNextStepWithEventsFirst');

    const workspace = readFrontendFile(
      'src/modules/selfEvolution/components/workbench/dataset/DatasetWorkspace.tsx',
    );
    expect(workspace).toContain('onStepsSnapshot');
    expect(workspace).toContain('stageStatuses');
    expect(workspace).not.toContain('onExecutionSettled');

    const stagesHook = readFrontendFile(
      'src/modules/selfEvolution/components/workbench/dataset/useDatasetStages.ts',
    );
    expect(stagesHook).toContain('onStepsSnapshot');
    expect(stagesHook).not.toContain('setStatuses');
  });

  it('aggregates top-level Dataset status from /steps only, not /dataset/result', () => {
    const controller = readFrontendFile(
      'src/modules/selfEvolution/hooks/useSelfEvolutionPageController.tsx',
    );
    expect(controller).toContain('deriveDatasetTopStatus(threadStepList.steps)');
    expect(controller).not.toContain('datasetFinalizationStep(threadStepList.steps)');
    expect(controller).toContain(
      'buildThreadStepStatusByStage(threadStepList, threadFlowStatus, datasetTopStatus)',
    );
    const workflow = readFrontendFile(
      'src/modules/selfEvolution/shared/datasetWorkflowStatus.ts',
    );
    expect(workflow).toContain('isRunning(step.status)');
    expect(workflow).toContain("return 'done'");
    expect(workflow).toContain("return 'waiting'");
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

  it('loads more cases inside the scrolling table, not from a footer outside it', () => {
    const source = readFrontendFile(
      'src/modules/selfEvolution/components/workbench/dataset/CasesStage.tsx',
    );
    const wrap = source.indexOf('className="dataset-table-wrap"');
    const sentinel = source.indexOf('<ScrollSentinel');
    const wrapClose = source.indexOf('</div>\n      </section>', wrap);

    expect(wrap).toBeGreaterThan(-1);
    expect(sentinel).toBeGreaterThan(wrap);
    expect(sentinel).toBeLessThan(wrapClose);
    expect(source).toContain('rootRef={listRef}');
  });
});
