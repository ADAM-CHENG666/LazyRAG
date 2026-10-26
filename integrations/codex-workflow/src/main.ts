import { readFile, writeFile, rename } from 'node:fs/promises'
import { parseArgs } from 'node:util'
import { randomUUID } from 'node:crypto'
import { join, dirname } from 'node:path'
import { setTimeout as delay } from 'node:timers/promises'
import { createDispatcher } from '../../workflow-agent-core/src/dispatcher'
import { HostBridge } from '../../workflow-agent-core/src/transport'
import { dispatcherLock, DispatcherBusy } from '../../workflow-agent-core/node/runtime-lock'
import { CodexAdapter } from './adapter'
import { CodexNative } from './native'
import { NativeReceipts } from './receipts'
import { presentationLoop, type PresentationStatus } from './presentation'
import { cleanupCodexPresentations } from './presentation'

async function main() {
  const { values } = parseArgs({ options: {
    'pairing-file': { type: 'string' }, 'bridge-url': { type: 'string', default: 'http://127.0.0.1:19091' },
    'cdp-port': { type: 'string' }, 'parent-pid': { type: 'string' },
    'cleanup-only': { type: 'boolean', default: false },
  } })
  const path = values['pairing-file']
  if (!path) throw new Error('pairing-file is required')
  const pairing = JSON.parse(await readFile(path, 'utf8'))
  if (pairing.provider !== 'codex' || !pairing.enabled && !values['cleanup-only']) throw new Error('Codex pairing is disabled')
  if (values['cleanup-only']) {
    const deadline = Date.now()+3000
    let release: (() => Promise<void>) | undefined
    while (!release) {
      try {release=await dispatcherLock(path,randomUUID())}
      catch(error) {if(!(error instanceof DispatcherBusy)||Date.now()>=deadline)throw error;await delay(100)}
    }
    try { await cleanupCodexPresentations(Number(values['cdp-port']),AbortSignal.timeout(2000)) }
    finally { await release() }
    return
  }
  const native = new CodexNative(Number(values['cdp-port']))
  const lifetime = new AbortController(), signal = lifetime.signal
  const stop = () => lifetime.abort()
  process.once('SIGINT', stop); process.once('SIGTERM', stop)
  const instance = randomUUID(), unlock = await dispatcherLock(path, instance)
  const bridge = new HostBridge(values['bridge-url']!, pairing)
  const adapter = new CodexAdapter(native, new NativeReceipts(join(dirname(path), pairing.connector_id + '.receipts')), bridge, signal)
  let presentation: PresentationStatus = { status: 'starting', checked_at: Date.now() }
  const lookup = async (session: string) => {
    const response = await fetch(new URL('/v1/codex/presentation?session_id=' + encodeURIComponent(session), values['bridge-url']!), {
      signal: AbortSignal.any([signal, AbortSignal.timeout(15000)]), redirect: 'error',
      headers: { Authorization: `Bearer ${pairing.token}`, 'X-LazyMind-Connector-Id': pairing.connector_id },
    })
    if (!response.ok) throw Object.assign(new Error('Codex presentation lookup unavailable'), {status:response.status})
    return response.json() as Promise<{session_id:string;run_id:string}>
  }
  const parent = Number(values['parent-pid'])
  if (!Number.isSafeInteger(parent) || parent < 1) throw new Error('parent-pid is required')
  const timer = setInterval(async () => {
    try {
      process.kill(parent, 0)
      const latest = JSON.parse(await readFile(path, 'utf8'))
      if (!latest.enabled || latest.token !== pairing.token) stop()
    } catch { stop() }
  }, 1000)
  async function heartbeat() {
    const file = path + '.codex-status.json', temp = file + '.' + instance
    while (!signal.aborted) {
      let ready = false, issue = 'host_unavailable'
      try { ready = (await native.probe(signal)).ready === true; issue = ready ? '' : 'capability_unavailable' } catch { issue = 'cdp_unavailable' }
      try {
        await writeFile(temp, JSON.stringify({ connector_id: pairing.connector_id, checked_at: Date.now(), ready, issue, presentation }), { mode: 0o600 })
        await rename(temp, file)
      } catch {}
      try { await delay(2000, undefined, { signal }) } catch { break }
    }
  }
  try { await Promise.all([heartbeat(), createDispatcher(adapter, adapter.host, bridge, instance, signal).poll(),
    presentationLoop(Number(values['cdp-port']), lookup, values['bridge-url']!, signal, value => { presentation = value })]) }
  finally { stop(); clearInterval(timer); await unlock(); process.off('SIGINT', stop); process.off('SIGTERM', stop) }
}
main().catch(error => { console.error(String(error)); process.exitCode = 1 })
