import { execFileSync } from 'node:child_process';
import { copyFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';

const directory = fileURLToPath(new URL('..', import.meta.url));
execFileSync(fileURLToPath(new URL('../node_modules/.bin/tsdown', import.meta.url)), [
  ...['adapter', 'mcp-proxy', 'health', 'prepare', 'manage'].map(name => `desktop-runtime/${name}.mjs`),
  '--format', 'esm', '--platform', 'node', '--out-dir', 'dist/desktop', '--no-dts',
], { cwd: directory, stdio: 'inherit' });
copyFileSync(new URL('../desktop-runtime/coordinator.py', import.meta.url), new URL('../dist/desktop/coordinator.py', import.meta.url));
