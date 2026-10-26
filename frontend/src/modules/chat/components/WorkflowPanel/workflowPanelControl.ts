import type { TabDef, WorkflowSession } from '@/modules/chat/store/workflowPanel';
import { deliveryPending, type WorkflowActionKind, type WorkflowControlView, type WorkflowReview, type WorkflowActionIntent } from '@/modules/chat/utils/workflowControl';
import { resolvePendingApprovalStep } from './workflowApproval';
import { resolveCompletedContinueStep, resolvePanelContinueAction, resolveWorkflowContinueAction } from './workflowContinue';

/** UI facts, independent of where the executor runs or how commands are sent. */
export interface PanelControlState {
  status: WorkflowSession['status'];
  review?: Pick<WorkflowReview, 'step_id'> & { confirmation: Pick<WorkflowActionIntent, 'review' | 'stepId'> };
  continuation: string;
  available_actions: string[];
  admission?: { reason?: string };
  busy: boolean;
  deliveryBusy: boolean;
}

/** Adapt the two existing API formats once, before selecting any UI actions. */
export function panelControlState(session: WorkflowSession, autoRunning: boolean, tab?: TabDef, control?: WorkflowControlView, canStop = true): PanelControlState {
  if (control) {
    const review = control.reviews.find(item => item.status === 'pending');
    const status = control.continuation === 'stopped' ? 'stopped'
      : control.active_executions > 0 ? 'active'
      : review ? 'waiting'
      : control.continuation === 'completed' ? 'completed'
      : control.continuation === 'failed' ? 'failed' : 'waiting';
    return { status, review: review ? { step_id: review.step_id, confirmation: { review } } : undefined, continuation: control.continuation, available_actions: control.available_actions,
      admission: control.admission, busy: status === 'active', deliveryBusy: deliveryPending(control) };
  }
  const status = autoRunning ? 'active' : session.status;
  const stepId = resolvePendingApprovalStep(session, status);
  const continuation = resolveWorkflowContinueAction(session, status, tab);
  const available_actions: WorkflowActionKind[] = ['save', 'retry', 'rewind'];
  if (canStop) available_actions.push('stop');
  if (stepId) available_actions.push('confirm_and_continue');
  else if (continuation) available_actions.push(continuation.kind === 'completed' ? 'rewind' : status === 'stopped' ? 'resume' : 'continue');
  return { status, review: stepId ? { step_id: stepId, confirmation: { stepId } } : undefined,
    continuation: status === 'stopped' ? 'stopped' : stepId ? 'awaiting_user' : status,
    available_actions, busy: status === 'active', deliveryBusy: false };
}

/** Both surfaces render exactly these rules; capabilities never depend on provider. */
export function panelControlActions(state: PanelControlState, session: WorkflowSession, tab?: TabDef) {
  const supports = (kind: string) => state.available_actions.includes(kind);
  const completedStepId = resolveCompletedContinueStep({ status: state.status }, tab);
  const continueKind = resolvePanelContinueAction(state, completedStepId);
  const review = !state.busy && state.status === 'waiting' && state.review
    && (supports('confirm_and_continue') || supports('confirm')) ? state.review : undefined;
  const canContinue = !state.busy && !review && Boolean(continueKind);
  const effectivePast = new Set(session.projection?.past ?? []);
  const rollbackSteps = ['completed', 'failed'].includes(state.status) && supports('rewind')
    ? (session.steps ?? []).filter((step, index, all) => effectivePast.has(step.step_id) && step.validity !== 'stale'
      && all.findIndex(candidate => candidate.step_id === step.step_id && candidate.validity !== 'stale') === index) : [];
  return { review, canContinue, continueKind, completedStepId, rollbackSteps,
    canStop: state.status === 'active' && supports('stop'),
    canRetry: state.status === 'failed' && supports('retry'),
    approvalKind: supports('confirm_and_continue') ? 'confirm_and_continue' as const : 'confirm' as const,
    continueLabel: review ? 'chat.workflowContinueExecution'
      : state.status === 'waiting' && resolveWorkflowContinueAction(session, state.status, tab)?.kind !== 'resume'
        ? 'chat.workflowSaveAndContinue' : 'chat.workflowContinue',
  };
}
