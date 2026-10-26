import {afterEach,describe,it,expect,vi} from 'vitest'
import {codexOperation,selectCodexTarget} from '../src/native'
afterEach(()=>vi.unstubAllGlobals())
describe('Codex renderer native API mapping',()=>{
 it('discovers the manager held by a desktop signal client ref',async()=>{
  const manager={getHostId:()=> 'local',getConversation:vi.fn(),startTurn:vi.fn(),interruptConversation:vi.fn()}
  const client={node:{familyBindings:new Map([['managers',new Map([['local',{value:{atom:{},store:{},get:()=>manager}}]])]])}}
  vi.stubGlobal('__lazymindCodexManager',undefined)
  vi.stubGlobal('__codexRoot',{_internalRoot:{current:{memoizedState:{memoizedState:{current:client}}}}})
  vi.stubGlobal('document',{querySelectorAll:()=>[]})
  vi.stubGlobal('Node',class {})
  expect(await codexOperation({kind:'probe'})).toEqual({ready:true})
  expect(manager.startTurn).not.toHaveBeenCalled()
 })
 it('selects the main chat when the avatar overlay shares its entry point',()=>{
  const main={type:'page',url:'app://-/index.html',id:'main'}
  const overlay={type:'page',url:'app://-/index.html?initialRoute=%2Favatar-overlay',id:'avatar'}
  expect(selectCodexTarget([overlay,main])).toBe(main)
  expect(()=>selectCodexTarget([overlay])).toThrow()
  expect(()=>selectCodexTarget([main,{...main,id:'another'}])).toThrow()
 })
 it('uses the desktop admission path and accepts its native turnId receipt',async()=>{
  const manager={getHostId:()=> 'local',getConversation:()=>({id:'thread',turns:[]}),startTurn:vi.fn().mockResolvedValue({turnId:'real-turn'}),interruptConversation:vi.fn()}
  vi.stubGlobal('__lazymindCodexManager',manager)
  expect(await codexOperation({kind:'start',threadId:'thread',clientId:'client',message:'continue'})).toEqual({accepted:true,turnId:'real-turn'})
  expect(manager.startTurn).toHaveBeenCalledWith('thread',expect.objectContaining({request:expect.objectContaining({threadId:'thread',clientUserMessageId:'client'}),context:expect.objectContaining({inheritThreadSettings:true,useUserMessageAdmission:false})}))
 })
 it('passes the expected turn to the native interrupt method',async()=>{
  const manager={getHostId:()=> 'local',getConversation:()=>({id:'thread',turns:[{turnId:'target',status:'inProgress',items:[]}]}),startTurn:vi.fn(),interruptConversation:vi.fn().mockResolvedValue('target')}
  vi.stubGlobal('__lazymindCodexManager',manager)
  expect(await codexOperation({kind:'interrupt',threadId:'thread',turnId:'target'})).toEqual({interruptedTurnId:'target'})
  expect(manager.interruptConversation).toHaveBeenCalledWith('thread','user-stop','target')
 })
})
