import { mkdtemp, writeFile, chmod, rm } from 'node:fs/promises'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { afterEach, expect, it } from 'vitest'
import { sharedEndpoint, registrationName } from '../src/runtime'

const directories: string[] = []
afterEach(async () => { for (const dir of directories.splice(0)) await rm(dir, { recursive: true, force: true }) })
it('selects native only from the matching private profile registration', async () => {
  const profile = await mkdtemp(join(tmpdir(), 'lm-runtime-mode-')); directories.push(profile)
  expect(await sharedEndpoint(profile)).toBeUndefined()
  const file = join(profile, registrationName)
  const endpoint = 'ws+unix://localhost/private/tmp/shared.sock:/rpc'
  await writeFile(file, JSON.stringify({ version: 1, profile, app_server_url: endpoint }), { mode: 0o600 })
  expect(await sharedEndpoint(profile)).toBe(endpoint)
  await writeFile(file, JSON.stringify({ version: 1, profile: '/other', app_server_url: endpoint }))
  await expect(sharedEndpoint(profile)).rejects.toThrow('profile')
  await writeFile(file, JSON.stringify({ version: 1, profile, app_server_url: 'ws://127.0.0.1:9999' }))
  await expect(sharedEndpoint(profile)).rejects.toThrow('local')
  if (process.platform !== 'win32') {
    await chmod(file, 0o644)
    await expect(sharedEndpoint(profile)).rejects.toThrow('private')
  }
})
