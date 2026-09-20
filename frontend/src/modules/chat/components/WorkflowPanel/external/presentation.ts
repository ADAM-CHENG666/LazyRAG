import { createContext } from 'react';
import type { ExecutionActivity } from './useExecutionActivity';

/** Supplied only by the external run page. No subscriptions in the shared panel. */
export interface ExternalWorkflowPresentation {
  statusLabel?: string;
  activities: Record<string, ExecutionActivity>;
}
export const ExternalWorkflowPresentationContext = createContext(false);
