import { expect, it } from 'vitest';
import { controlStatusKey } from './loadRun';
import type { WorkflowControlView } from '@/modules/chat/utils/workflowControl';

it.each([
 ['binding_required', 0, 'workflowControlBindingStatus'],
 ['awaiting_executor', 1, 'workflowControlExecutorStatus'],
 ['draining', 1, 'workflowControlDrainingStatus'],
 ['awaiting_user', 0, 'workflowControlReviewStatus'],
 ['continue', 0, 'workflowControlHostStatus'],
 ['continue', 1, 'workflowStatusRunning'],
 ['stopped', 0, 'workflowStatusStopped'],
 ['completed', 0, 'workflowStatusDone'],
 ['failed', 0, 'workflowStatusFailed'],
])('distinguishes %s from pending execution', (continuation, count, key) => {
 expect(controlStatusKey({ continuation, active_executions: count } as WorkflowControlView)).toBe(`chat.${key}`);
});
