import type { TabDef, WorkflowSession } from '@/modules/chat/store/workflowPanel';
import { resolvePendingApprovalStep } from './workflowApproval';

/** Select the Continue command from normalized Panel capabilities. */
export function resolvePanelContinueAction(
  control: { continuation: string; available_actions: string[]; admission?: { reason?: string } },
  completedStep?: string,
): 'continue' | 'resume' | 'rewind' | undefined {
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
  if (!['waiting', 'stopped'].includes(displayStatus) || session.projection?.completed) return undefined;
  if (displayStatus === 'stopped') return { kind: 'resume', stepId: session.current_step_id ?? '' };
  const nodes = session.projection?.nodes ?? {};
  if (Object.values(nodes).some((node) => node.validity !== 'stale'
    && ['pending', 'queued', 'claimed', 'running', 'failed'].includes(node.execution))) return undefined;
  const interruptedStep = (session.projection?.current ?? []).find((id) =>
    nodes[id]?.validity !== 'stale' && ['interrupted', 'cancelled', 'canceled'].includes(nodes[id]?.execution),
  );
  if (interruptedStep) return { kind: 'resume', stepId: interruptedStep };
  // Once execution has yielded with a ready successor, users can explicitly
  // continue on either surface. An initial dispatch is not a checkpoint.
  const succeeded = (session.steps ?? []).some(step => step.validity !== 'stale' && step.status === 'succeeded');
  return succeeded && (session.projection?.ready?.length ?? 0) > 0
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
