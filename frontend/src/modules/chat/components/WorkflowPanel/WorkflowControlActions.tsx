import { useState } from 'react';
import { useTranslation } from 'react-i18next';
import type { WorkflowPanelControlContext } from '@/modules/chat/components/WorkflowPanel';
import { deliveryPending, type WorkflowActionIntent, type WorkflowControlView } from '@/modules/chat/utils/workflowControl';

export function WorkflowControlActions({ context, control, act }: {
  context: WorkflowPanelControlContext;
  control: WorkflowControlView;
  act(intent: WorkflowActionIntent): Promise<void>;
}) {
  const { t } = useTranslation();
  const [skipApproval, setSkipApproval] = useState(false);
  const available = new Set(control.available_actions);
  const review = control.reviews.find(item => item.status === 'pending' && context.stepIds.includes(item.step_id));
  const otherReview = !review && control.reviews.find(item => item.status === 'pending');
  const deliveryBusy = deliveryPending(control);
  const latest = context.session.steps?.filter(step => step.step_id === context.stepId && step.validity !== 'stale')
    .sort((a, b) => b.attempt - a.attempt)[0];
  const canRetry = !!latest && ['failed', 'interrupted', 'cancelled', 'canceled'].includes(latest.status);
  const canRewind = latest?.status === 'succeeded';
  const showConfirmContinue = !!review && available.has('confirm_and_continue');
  // Confirm-only is the fallback when the host cannot be resumed from this panel.
  const showConfirmOnly = !!review && available.has('confirm') && !showConfirmContinue;
  const perform = (intent: WorkflowActionIntent, flush = true) => { void context.runAction(() => act(intent), flush); };
  const button = (label: string, intent: WorkflowActionIntent, enabled: boolean, tone = 'secondary', flush = true) =>
    <button type='button' className={`workflow-panel__action-btn workflow-panel__action-btn--${tone}`}
      disabled={context.pending || !enabled} onClick={() => perform(intent, flush)}>{label}</button>;
  return <>
    {available.has('save') && context.dirty && button(t('chat.workflowControlSave'), { kind: 'save' }, true)}
    {showConfirmOnly && button(t('chat.workflowControlConfirm'), { kind: 'confirm', review }, true)}
    {showConfirmContinue && <>
      <label className='workflow-panel__footer-skip-approval'>
        <input type='checkbox' checked={skipApproval} disabled={context.pending || deliveryBusy}
          onChange={event => setSkipApproval(event.target.checked)} />
        {t('chat.workflowSkipThisApproval')}
      </label>
      {button(t('chat.workflowControlConfirmContinue'), {
        kind: 'confirm_and_continue', review, preferenceScope: skipApproval ? 'step' : undefined,
      }, !deliveryBusy, 'primary')}
    </>}
    {otherReview && <span role='status'>{t('chat.workflowControlOtherReview', { step: otherReview.step_id })}</span>}
    {!review && available.has('continue') && button(t('chat.workflowContinue'), { kind: 'continue' }, !deliveryBusy, 'primary')}
    {available.has('retry') && canRetry && button(t('chat.workflowRetry'), { kind: 'retry', stepId: context.stepId }, !!context.stepId && !deliveryBusy)}
    {available.has('rewind') && canRewind && button(t(review ? 'chat.workflowControlRegenerate' : 'chat.workflowControlRewind'), { kind: 'rewind', stepId: context.stepId }, !!context.stepId && !deliveryBusy)}
    {available.has('stop') && button(t('chat.workflowStop'), { kind: 'stop' }, true, 'danger', false)}
    {available.has('resume') && button(t('chat.workflowControlResume'), { kind: 'resume' }, !deliveryBusy, 'primary', false)}
  </>;
}
