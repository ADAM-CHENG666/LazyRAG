import { describe, expect, it } from 'vitest';
import {
  draftAffectsTab,
  resolveRevisionRefreshAction,
  shouldPublishRefresh,
} from '../../../frontend/src/modules/selfEvolution/components/workbench/dataset/datasetRefresh.ts';

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
});
