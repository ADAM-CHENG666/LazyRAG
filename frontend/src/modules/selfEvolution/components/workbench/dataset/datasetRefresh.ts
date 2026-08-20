import type { DatasetDraft, DatasetTab } from './types';

export const TERMINAL_STAGE_EVENTS = new Set([
  'step.finish',
  'checkpoint.continue',
  'done',
]);

export function draftAffectsTab(draft: DatasetDraft | undefined, tab: DatasetTab): boolean {
  if (!draft) return false;
  if (tab === 'materials') {
    return draft.kind === 'materials-config' || draft.kind === 'chunk-selection';
  }
  if (tab === 'topics') {
    return draft.kind === 'topic-names';
  }
  return draft.kind === 'generation-plan';
}

export function shouldPublishRefresh(
  previous: string | null | undefined,
  next: string | null,
): boolean {
  if (previous !== undefined && previous !== next) return true;
  return previous === undefined && next !== null;
}

export type RevisionRefreshAction = 'auto' | 'stale' | 'pending' | 'none';

/** Decide how a stage should react when its published revision changes. */
export function resolveRevisionRefreshAction(
  stageTab: DatasetTab,
  currentTab: DatasetTab,
  previous: string | null | undefined,
  next: string | null,
  draft: DatasetDraft | undefined,
): RevisionRefreshAction {
  if (!shouldPublishRefresh(previous, next)) {
    return 'none';
  }
  if (stageTab === currentTab) {
    return draftAffectsTab(draft, stageTab) ? 'stale' : 'auto';
  }
  return 'pending';
}
