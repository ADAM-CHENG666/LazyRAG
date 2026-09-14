import { fireEvent, render, screen } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';
import type { WorkflowSession } from '@/modules/chat/store/workflowPanel';
import type { WorkflowControlView } from '@/modules/chat/utils/workflowControl';
import { WorkflowControlActions } from './WorkflowControlActions';

vi.mock('react-i18next', () => ({
  useTranslation: () => ({
    t: (key: string) => key,
  }),
}));

const review = { id: 'review-1', step_id: 'script', execution_id: 'attempt-1', status: 'pending' as const, version: 1, manifest_hash: 'hash-1' };

function control(overrides: Partial<WorkflowControlView> = {}): WorkflowControlView {
  return {
    protocol: 'workflow.control.v1', session_id: 'run-1', state_version: 3, continuation: 'awaiting_user',
    admission: { can_begin: false }, active_executions: 0, active_execution_ids: [], binding: { bound: true, generation: 1 },
    reviews: [review], delivery: null,
    available_actions: ['save', 'confirm', 'confirm_and_continue', 'retry', 'rewind', 'stop'],
    ...overrides,
  };
}

function session(status: string): WorkflowSession {
  return {
    steps: [{ step_id: 'script', attempt: 1, status, validity: 'effective' }],
  } as WorkflowSession;
}

function renderActions(options?: { dirty?: boolean; stepStatus?: string; control?: WorkflowControlView }) {
  const act = vi.fn(async () => undefined);
  const runAction = vi.fn(async (action: () => Promise<void>) => { await action(); });
  render(<WorkflowControlActions
    control={options?.control ?? control()}
    act={act}
    context={{
      session: session(options?.stepStatus ?? 'succeeded'),
      stepId: 'script',
      stepIds: ['script'],
      pending: false,
      dirty: options?.dirty,
      runAction,
    }}
  />);
  return { act, runAction };
}

describe('WorkflowControlActions review footer', () => {
  it('keeps confirm-and-continue, regenerate, and stop during a succeeded review', () => {
    renderActions();
    expect(screen.getByRole('button', { name: 'chat.workflowControlConfirmContinue' })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'chat.workflowControlRegenerate' })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'chat.workflowStop' })).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'chat.workflowControlConfirm' })).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'chat.workflowControlSave' })).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'chat.workflowRetry' })).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'chat.workflowSkipThisApproval' })).not.toBeInTheDocument();
  });

  it('shows save only while the current tab has unsaved edits', () => {
    renderActions({ dirty: true });
    expect(screen.getByRole('button', { name: 'chat.workflowControlSave' })).toBeInTheDocument();
  });

  it('falls back to confirm-only when the host cannot continue from the panel', () => {
    renderActions({
      control: control({ available_actions: ['save', 'confirm', 'retry', 'rewind', 'stop'] }),
    });
    expect(screen.getByRole('button', { name: 'chat.workflowControlConfirm' })).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'chat.workflowControlConfirmContinue' })).not.toBeInTheDocument();
  });

  it('shows retry instead of regenerate after the current step failed', () => {
    renderActions({ stepStatus: 'failed' });
    expect(screen.getByRole('button', { name: 'chat.workflowRetry' })).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'chat.workflowControlRegenerate' })).not.toBeInTheDocument();
  });

  it('sends skip-approval as confirm-and-continue with a step preference', async () => {
    const { act } = renderActions();
    fireEvent.click(screen.getByRole('checkbox', { name: 'chat.workflowSkipThisApproval' }));
    fireEvent.click(screen.getByRole('button', { name: 'chat.workflowControlConfirmContinue' }));
    expect(act).toHaveBeenCalledWith({
      kind: 'confirm_and_continue',
      review,
      preferenceScope: 'step',
    });
  });

  it('rewinds the current succeeded step from regenerate', async () => {
    const { act } = renderActions();
    fireEvent.click(screen.getByRole('button', { name: 'chat.workflowControlRegenerate' }));
    expect(act).toHaveBeenCalledWith({ kind: 'rewind', stepId: 'script' });
  });
});
