import { describe, it, expect, vi } from 'vitest'
import { mkdtemp } from 'node:fs/promises'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { CodexAdapter } from '../src/adapter'
import { NativeReceipts } from '../src/receipts'
import { selectCodexTarget } from '../src/native'
const thread = '01a12f01-e372-7ef3-aaae-4a0a295d6700'
const claim = (kind = 'continue') => ({ action: { id: 'action-1', session_id: 'run-1', native_session_id: thread,
  kind, status: 'dispatching', created_at: '2026-10-26T00:00:10Z' }, control: { session_id: 'run-1', continuation: kind === 'cancel' ? 'stopped' : 'continue',
  binding: { bound: true, driver_session_id: thread, provider: 'codex' } } }) as any
async function fixture() {
 const native = { read: vi.fn().mockResolvedValue({ id: thread, active: false, turns: [] }),
   start: vi.fn().mockResolvedValue({ accepted: true, turnId: 'turn-new' }), interrupt: vi.fn().mockResolvedValue({ interruptedTurnId: 'turn-owned' }) }
 const receipts = new NativeReceipts(await mkdtemp(join(tmpdir(), 'codex-cdp-receipts-')))
 const bridge = { action: vi.fn() } as any
 return { native, receipts, bridge, adapter: new CodexAdapter(native, receipts, bridge, new AbortController().signal) }
}
describe('CDP Workflow admission and interruption', () => {
 it('sends one notification to the bound chat and saves its actual turn receipt', async () => {
  const {adapter,native,receipts}=await fixture();adapter.host.cacheClaim(claim())
  await expect(adapter.prompt(thread,{actionId:'action-1',message:'continue'},new AbortController().signal)).resolves.toBe(0)
  expect(native.start).toHaveBeenCalledWith(thread,expect.any(String),'[LazyMind action_id=action-1]\ncontinue',expect.any(AbortSignal))
  expect(await receipts.read('action-1')).toMatchObject({status:'accepted',turnId:'turn-new',threadId:thread})
 })
 it('does not send when the target becomes busy', async()=>{
  const {adapter,native}=await fixture();adapter.host.cacheClaim(claim());native.read.mockResolvedValue({id:thread,active:true,turns:[]})
  await expect(adapter.prompt(thread,{actionId:'action-1',message:'continue'},new AbortController().signal)).rejects.toThrow('busy')
  expect(native.start).not.toHaveBeenCalled()
 })
 it('does not resend a write with an uncertain outcome',async()=>{
  const {adapter,native}=await fixture();native.start.mockRejectedValue(new Error('CDP disconnected'))
  adapter.host.cacheClaim(claim());await expect(adapter.prompt(thread,{actionId:'action-1',message:'continue'},new AbortController().signal)).rejects.toThrow()
  adapter.host.cacheClaim(claim());await expect(adapter.prompt(thread,{actionId:'action-1',message:'continue'},new AbortController().signal)).rejects.toThrow('already attempted')
  expect(native.start).toHaveBeenCalledTimes(1)
 })
 it('interrupts the exact Workflow-owned turn',async()=>{
  const {adapter,native,bridge}=await fixture();const action=claim('cancel');bridge.action.mockResolvedValue(action)
  native.read.mockResolvedValue({id:thread,active:true,turns:[{id:'turn-owned',status:'inProgress',startedAt:Date.parse('2026-10-26T00:00:00Z'),items:[{
   type:'mcpToolCall',server:'lazymind',tool:'workflow.start',status:'completed',result:{structuredContent:{session_id:'run-1'}}
  }]}]})
  await adapter.host.cancelSession(thread,action)
  expect(native.interrupt).toHaveBeenCalledWith(thread,'turn-owned',expect.any(AbortSignal))
 })
 it('refuses Stop against a new user turn or work started after Stop',async()=>{
  const {adapter,native}=await fixture()
  for(const items of [[{type:'userMessage',content:[{type:'text',text:'another task'}]}],[{type:'mcpToolCall',server:'lazymind',tool:'workflow.start',status:'completed',result:{structuredContent:{session_id:'run-1'}}}]]) {
   native.read.mockResolvedValue({id:thread,active:true,turns:[{id:'new',status:'inProgress',startedAt:Date.parse('2026-10-26T00:00:20Z'),items}]})
   await expect(adapter.host.cancelSession(thread,claim('cancel'))).rejects.toThrow('not the target')
  }
  expect(native.interrupt).not.toHaveBeenCalled()
 })
 it('selects the Codex renderer and rejects ambiguity',()=>{
  expect(selectCodexTarget([{type:'page',url:'app://-/index.html'},{type:'page',url:'https://example.test'}]).url).toBe('app://-/index.html')
  expect(()=>selectCodexTarget([{type:'page',url:'app://-/index.html'},{type:'page',url:'app://-/index.html'}])).toThrow('one Codex')
 })
})
