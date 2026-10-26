import { object } from '../../workflow-agent-core/src/protocol'
import type { NativeTurn } from './native'
/** Only native MCP receipts after the latest user input establish execution ownership. */
export function workflowTurnOwner(turn: NativeTurn): string | undefined {
  let run: string | undefined
  for (const value of turn.items ?? []) {
    const item = object(value)
    if (!item) continue
    if (item.type === 'userMessage') { run = undefined; continue }
    if (item.type !== 'mcpToolCall' || !['lazymind', 'lazymind-workflow'].includes(String(item.server))
      || !['workflow.start', 'workflow.step.begin', 'workflow.step.claim', 'workflow.step.resume', 'workflow.step.complete'].includes(String(item.tool))
      || item.status !== 'completed' || item.error) continue
    const result = object(item.result), fields = object(result?.structuredContent)
    if (!fields || result?.isError === true) continue
    const state = object(fields.state), execution = object(fields.execution)
    const id = fields.session_id ?? state?.session_id
    if (typeof id !== 'string' || !id) continue
    // An error or a read of another run never claims a turn. Acquisitions must
    // have a real execution, and their argument/result session must agree.
    if (item.tool !== 'workflow.start' && object(item.arguments)?.session_id !== id) continue
    if (['workflow.step.begin', 'workflow.step.claim', 'workflow.step.resume'].includes(String(item.tool)) && typeof execution?.execution_id !== 'string') continue
    run = id
  }
  return run
}
