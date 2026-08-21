import { describe, expect, it } from 'vitest';
import { resolveCurrentStageStatus } from '../../../frontend/src/modules/selfEvolution/shared/stageStatus.ts';

describe('top-level Dataset status', () => {
  it('uses the current /steps status instead of a historical paused event', () => {
    expect(resolveCurrentStageStatus('running', 'paused', false, 'pending')).toBe('running');
  });
});
