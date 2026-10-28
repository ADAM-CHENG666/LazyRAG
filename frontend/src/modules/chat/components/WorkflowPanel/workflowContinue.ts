import type { TabDef, WorkflowSession } from '@/modules/chat/store/workflowPanel';
import { resolvePendingApprovalStep } from './workflowApproval';

/** Resolve the permitted protocol command after the shared UI checkpoint check. */
export function resolveExternalContinueAction(
  control: { continuation: string; available_actions: string[]; admission?: { reason?: string }; active_executions?: number },
  completedStep?: string,
): 'continue' | 'resume' | 'rewind' | undefined {
  if ((control.active_executions ?? 0) > 0) return undefined;
  const action = control.continuation === 'stopped' ? 'resume'
    : completedStep && control.admission?.reason !== 'edits_pending_continue' ? 'rewind' : 'continue';
  return control.available_actions.includes(action) ? action : undefined;
}

export type WorkflowContinueAction = { kind: 'approval' | 'resume' | 'completed' | 'continue'; stepId: string };

/** Waiting alone is not a checkpoint: automatic dispatch also passes through it. */
export function resolveWorkflowContinueAction(
  session: Pick<WorkflowSession, 'status' | 'current_step_id' | 'steps' | 'projection'>,
  displayStatus: string,
  activeTab?: TabDef,
): WorkflowContinueAction | undefined {
  if (displayStatus === 'active') return undefined;
  const completedStep = resolveCompletedContinueStep(session, activeTab);
  if (completedStep) return { kind: 'completed', stepId: completedStep };
  const approvalStep = resolvePendingApprovalStep(session, displayStatus);
  if (approvalStep) return { kind: 'approval', stepId: approvalStep };
  // Main labels an idle driver that did not advance as failed/no_advance.
  // A ready successor is still recoverable without retrying successful work.
  const yielded = displayStatus === 'waiting' || displayStatus === 'failed' && session.projection?.display?.reason === 'no_advance';
  if ((!yielded && displayStatus !== 'stopped') || session.projection?.completed) return undefined;
  const nodes = session.projection?.nodes ?? {};
  if (Object.values(nodes).some((node) => node.validity !== 'stale'
    && ['pending', 'queued', 'claimed', 'running', 'failed'].includes(node.execution))) return undefined;
  const interruptedStep = (session.projection?.current ?? []).find((id) =>
    nodes[id]?.validity !== 'stale' && ['interrupted', 'cancelled', 'canceled'].includes(nodes[id]?.execution),
  );
  if (displayStatus === 'stopped') {
    // A confirmed pause can occur before dispatch or between successful steps.
    const stepId = interruptedStep ?? session.projection?.ready?.[0];
    if (stepId) return { kind: 'resume', stepId };
  }
  if (interruptedStep) return { kind: 'resume', stepId: interruptedStep };
  const succeeded = (session.steps ?? []).some(step => step.validity !== 'stale' && step.status === 'succeeded');
  const ready = (session.projection?.ready ?? []).some(id => nodes[id]?.validity !== 'stale' && nodes[id]?.execution === 'none');
  return yielded && succeeded && ready
    ? { kind: 'continue', stepId: session.current_step_id ?? '' } : undefined;
}

export function resolveCompletedContinueStep(
  session: Pick<WorkflowSession, 'status'>,
  activeTab?: TabDef,
): string | undefined {
  if (session.status !== 'completed') return undefined;
  const stepId = activeTab?.completed_continue_step?.trim();
  return stepId || undefined;
}
