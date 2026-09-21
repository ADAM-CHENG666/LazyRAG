import type { RunLink } from './protocol'

/** A is an opaque host agent handle. The shared runtime never inspects its SDK. */
export interface RuntimeAdapter<A> {
  id(agent: A): string
  parent(agent: A): A | undefined
  isLive(agent: A): boolean
  isRunning(agent: A): boolean
  history(agent: A): readonly HostEvent[]
  canReturnResult(agent: A): boolean
  goal?(agent: A): { id: string; createdAt: number } | undefined
  suspendGoal?(agent: A, input: { runId: string; goalId?: string; continuation: string }): void
  resumeGoal?(agent: A, runId: string): void
  resolve(sessionId: string): Promise<{ agent: A } | { error: string }>
  /** Resolves after host admission, not after execution. Throws if admission is uncertain. */
  prompt(agent: A, input: { actionId: string; message: string }, signal: AbortSignal): Promise<number>
  cancel(agent: A): void | Promise<void>
  eventSeq(agent: A): number
  /** Exact durable input event sequence, or zero if no evidence was found. Never implies safe retry. */
  reconcile(sessionId: string, actionId: string, signal: AbortSignal): Promise<number>
  warn(message: string): void
}

export interface HostEvent {
  seq: number
  time: number
  user: boolean
  run?: RunLink | null
}

export interface HostInput { user: boolean; requestId?: string }

export interface ToolCall<A> {
  agent?: A
  /** Normalized Workflow operation; null for ordinary host tools. */
  operation: string | null
  arguments?: unknown
  returnsResult?: boolean
  signal: AbortSignal
  callId: string
  nested?: boolean
}

export interface UIAdapter {
  openPanel(input: { hostSessionId: string; runId: string; url: string }): void
}
