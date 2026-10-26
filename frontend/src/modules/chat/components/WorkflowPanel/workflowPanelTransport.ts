import type { TabDef, WorkflowSession } from '@/modules/chat/store/workflowPanel';
import type { WorkflowActionIntent } from '@/modules/chat/utils/workflowControl';
import { resolveWorkflowContinueAction } from './workflowContinue';

/** Native chat still sends user messages. Presentation never depends on this transport. */
export function nativePanelCommands(options: {
  session: WorkflowSession;
  tab?: TabDef;
  latest(): { session?: WorkflowSession | null; running?: boolean };
  send?(text: string): void;
  stop(): void;
  setApproval(stepId: string, scope: 'step' | 'following'): Promise<unknown>;
  text(key: string): string;
}) {
  const { session, tab, text } = options;
  const current = () => {
    const latest = options.latest();
    if (!latest.session || latest.running || latest.session.session_id !== session.session_id
      || latest.session.state_version !== session.state_version) return false;
    const displayed = resolveWorkflowContinueAction(session, session.status, tab);
    const actual = resolveWorkflowContinueAction(latest.session, latest.session.status, tab);
    return Boolean(displayed && actual?.kind === displayed.kind && actual.stepId === displayed.stepId);
  };
  return async (intent: WorkflowActionIntent) => {
    switch (intent.kind) {
      case 'stop': options.stop(); return;
      case 'retry': options.send?.(text('chat.workflowRetry')); return;
      case 'rewind': options.send?.(`${text('chat.workflowRollbackPrefix')}${intent.stepId}`); return;
      case 'confirm':
      case 'confirm_and_continue':
        if (!current()) return;
        if (intent.preferenceScope && intent.stepId) await options.setApproval(intent.stepId, intent.preferenceScope);
        if (current()) options.send?.(text('chat.workflowContinue'));
        return;
      case 'continue':
      case 'resume':
        if (!current()) return;
        options.send?.(intent.completedStepId
          ? `${text('chat.workflowRollbackPrefix')}${intent.completedStepId}` : text('chat.workflowContinue'));
    }
  };
}
