import { afterEach, expect, it, vi } from 'vitest';
const patch = vi.hoisted(() => vi.fn());
vi.mock('@/modules/chat/utils/request', () => ({
  WorkflowSessionApi: () => ({ patchSlotItem: patch }),
}));
import { draftStore, useWorkflowStore } from './workflowPanel';

afterEach(() => { vi.useRealTimers(); vi.restoreAllMocks(); patch.mockReset(); localStorage.clear(); });

it('keeps a failed background save available for an explicit retry', async () => {
  vi.useFakeTimers();
  vi.spyOn(useWorkflowStore.getState(), 'patchSlotItemValue').mockImplementation(patch);
  patch.mockRejectedValueOnce(new Error('offline'));
  draftStore.setDraft('background-test', 'prompt', 0, { text: 'Unsaved' }, -1);
  await vi.advanceTimersByTimeAsync(65000);
  expect(patch).toHaveBeenCalledTimes(1);
  expect(draftStore.getLocalDraft('background-test', 'prompt', 0)).toEqual({ text: 'Unsaved' });
  patch.mockResolvedValueOnce({});
  await draftStore.flushDraft('background-test', 'prompt', 0);
  expect(draftStore.getLocalDraft('background-test', 'prompt', 0)).toBeNull();
});

it('retains a manual draft across failed saves and never flushes it on a timer', async () => {
  vi.useFakeTimers();
  vi.spyOn(useWorkflowStore.getState(), 'patchSlotItemValue').mockImplementation(patch);
  draftStore.setDraft('edit-test', 'prompt', 0, { text: 'My changes' }, -1, 1, 0, true);
  await vi.advanceTimersByTimeAsync(65000);
  expect(patch).not.toHaveBeenCalled();
  patch.mockRejectedValueOnce(new Error('save rejected'));
  expect(await draftStore.flushDraft('edit-test', 'prompt', 0)).toBe(false);
  expect(draftStore.getLocalDraft('edit-test', 'prompt', 0)).toEqual({ text: 'My changes' });
  patch.mockResolvedValueOnce({});
  await draftStore.flushDraft('edit-test', 'prompt', 0);
  expect(patch).toHaveBeenLastCalledWith('edit-test', 'prompt', -1, { text: 'My changes' }, undefined, 'checkpoint', 1, 0);
  expect(draftStore.getLocalDraft('edit-test', 'prompt', 0)).toBeNull();
});
