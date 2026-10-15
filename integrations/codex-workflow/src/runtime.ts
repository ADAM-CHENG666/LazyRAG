import { lstat, readFile } from 'node:fs/promises'
import { join } from 'node:path'
import { AppServer, schemaVersion, unixEndpoint } from './app-server'
import { CodexAdapter } from './adapter'
import { NativeCodexAdapter, NativeReceipts } from './native-adapter'
import type { HostTransport } from '../../workflow-agent-core/src/transport'

export const registrationName = 'lazymind-shared-server.json'

/** Absence means legacy queue. Invalid or unavailable shared mode never falls back. */
export async function sharedEndpoint(profile: string): Promise<string | undefined> {
  const file = join(profile, registrationName)
  let info
  try { info = await lstat(file) }
  catch (error) { if ((error as NodeJS.ErrnoException).code === 'ENOENT') return undefined; throw error }
  if (!info.isFile() || process.platform !== 'win32' && ((info.mode & 0o077) !== 0 || info.uid !== process.getuid!())) {
    throw new Error('Shared server registration must be a private file owned by this user')
  }
  const value = JSON.parse(await readFile(file, 'utf8'))
  if (value.version !== 1 || value.profile !== profile || typeof value.app_server_url !== 'string') {
    throw new Error('Shared server registration does not match this Codex profile')
  }
  unixEndpoint(value.app_server_url)
  return value.app_server_url
}

export async function openRuntime(input: {
  binary: string; profile: string; pairingFile: string; connectorId: string
  bridge: HostTransport; lifetime: AbortController; endpoint?: string | null
}) {
  const endpoint = input.endpoint === undefined ? await sharedEndpoint(input.profile) : input.endpoint
  if (!endpoint) return { mode: 'queue' as const, adapter: new CodexAdapter(input.binary, input.profile), host: undefined, close: () => {} }
  const { client, identity } = await AppServer.connect(endpoint, {
    expectedHome: input.profile, experimental: true, signal: input.lifetime.signal,
  })
  if (!identity.userAgent.includes(schemaVersion)) {
    client.close()
    throw new Error(`Shared App Server must use validated CLI ${schemaVersion}`)
  }
  // A disconnected worker releases its lock and is restarted by its MCP supervisor.
  // Persistent receipts and Core claims prevent resending uncertain operations.
  const disconnected = () => input.lifetime.abort(new Error('Shared App Server disconnected; no queue fallback'))
  client.on('disconnected', disconnected)
  const adapter = new NativeCodexAdapter(client, input.bridge,
    new NativeReceipts(`${input.pairingFile}.native-receipts`), input.lifetime.signal, input.connectorId)
  return { mode: 'native' as const, adapter, host: adapter.host,
    close: () => { client.off('disconnected', disconnected); client.close() } }
}
