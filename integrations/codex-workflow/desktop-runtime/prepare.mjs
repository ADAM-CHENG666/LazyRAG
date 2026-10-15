import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import crypto from 'node:crypto';
import { fileURLToPath } from 'node:url';
import { parseArgs } from 'node:util';
import { execFileSync } from 'node:child_process';
import { atomicJSON, privateDirectory, loadConfig } from './config.mjs';

const { values } = parseArgs({ options: {
  output: { type: 'string' }, app: { type: 'string', default: '/Applications/ChatGPT.app' },
  'codex-home': { type: 'string', default: process.env.CODEX_HOME ?? path.join(os.homedir(), '.codex') },
  python: { type: 'string', default: '/usr/bin/python3' },
  'runtime-dir': { type: 'string' }, 'desktop-plugin': { type: 'string' }, 'browser-plugin': { type: 'string' },
} });
if (process.platform !== 'darwin') throw new Error('Desktop shared runtime currently supports macOS only');
if (!values.output) throw new Error('Use --output <new private installation directory>; prepare does not enable shared mode');
const output = path.resolve(values.output);
const configFile = path.join(output, 'runtime.json');
if (fs.existsSync(output)) throw new Error('Output already exists; use a new directory to preserve any active installation');
const sourceBundle = path.dirname(fileURLToPath(import.meta.url));
for (const file of ['adapter.mjs', 'mcp-proxy.mjs', 'health.mjs', 'coordinator.py', 'manage.mjs']) {
  fs.accessSync(path.join(sourceBundle, file), fs.constants.R_OK);
}
const app = fs.realpathSync(values.app);
const resources = path.join(app, 'Contents/Resources');
const cli = path.join(resources, 'codex-cli/CodexCLI.app/Contents/MacOS/codex');
const node = path.join(resources, 'cua_node/bin/node');
const python = path.resolve(values.python);
const codexHome = fs.realpathSync(values['codex-home']);
const plist = key => execFileSync('/usr/libexec/PlistBuddy', ['-c', `Print :${key}`, path.join(app, 'Contents/Info.plist')], { encoding: 'utf8' }).trim();
const desktopExecutable = path.join(app, 'Contents/MacOS', plist('CFBundleExecutable'));
const cliVersion = execFileSync(cli, ['--version'], { encoding: 'utf8', timeout: 5000 }).trim().replace(/^codex-cli\s+/, '');
if (cliVersion !== '0.162.0-alpha.17.2') throw new Error(`CLI ${cliVersion} has not been validated for this runtime`);
execFileSync(python, ['-c', 'import fcntl, json, pathlib, subprocess'], { timeout: 5000 });
execFileSync(node, ['-e', 'if (typeof process.execve !== "function") process.exit(1)'], { timeout: 5000 });

const sourceManifests = {};
function originalPlugin(kind, pluginName, explicit) {
  let file = explicit && path.resolve(explicit);
  if (!file) {
    const cache = path.join(codexHome, 'plugins/cache/openai-bundled', pluginName);
    const candidates = fs.readdirSync(cache).map(version => path.join(cache, version, '.mcp.json')).filter(file => fs.existsSync(file));
    if (candidates.length !== 1) throw new Error(`Select the active ${pluginName} manifest explicitly; found ${candidates.length} cached versions`);
    file = candidates[0];
  }
  const plugin = JSON.parse(fs.readFileSync(file, 'utf8')).mcpServers?.[kind];
  sourceManifests[kind] = file;
  if (!plugin || !Array.isArray(plugin.args) || !path.isAbsolute(plugin.command)) throw new Error(`Expected the installed, resolved ${kind} manifest`);
  const cwd = plugin.cwd ?? path.dirname(file);
  fs.accessSync(plugin.command, fs.constants.X_OK);
  if (kind === 'codex_app') fs.accessSync(path.join(cwd, 'server.mjs'), fs.constants.R_OK);
  else {
    if (fs.realpathSync(plugin.command) !== fs.realpathSync(node)) throw new Error('CUA must use the original App bundled Node');
    if (!plugin.args[0] || !path.isAbsolute(plugin.args[0])) throw new Error('CUA entrypoint is not resolved');
    fs.accessSync(plugin.args[0], fs.constants.R_OK);
  }
  const env = { ...plugin.env };
  for (const key of ['CODEX_APP_TOOLS_PIPE_PATH', 'CODEX_TASK_WORKSPACE_VERIFYING_IDENTITY', 'BROWSER_USE_ACCESS_VERIFYING_IDENTITY']) delete env[key];
  return { ...plugin, cwd, env };
}

const plugins = {
  codex_app: originalPlugin('codex_app', 'codex-app-tools', values['desktop-plugin']),
  cua_repl: originalPlugin('cua_repl', 'unified-computer-use', values['browser-plugin']),
};
// A short, profile-specific location avoids macOS Unix socket path limits.
const key = crypto.createHash('sha256').update(output + '\0' + codexHome).digest('hex').slice(0, 16);
const runtimeDir = values['runtime-dir'] ? path.resolve(values['runtime-dir']) : `/private/tmp/lm-codex-${process.getuid()}/${key}`;
privateDirectory(path.dirname(runtimeDir));
privateDirectory(output);
const bundleDir = path.join(output, 'runtime');
privateDirectory(bundleDir);
for (const file of fs.readdirSync(sourceBundle).filter(file => file.endsWith('.mjs') || file === 'coordinator.py')) {
  fs.copyFileSync(path.join(sourceBundle, file), path.join(bundleDir, file));
}
atomicJSON(configFile, {
  version: 1, bundleDir, runtimeDir, cli, node, python, codexHome, desktopExecutable,
  cliVersion, desktopVersion: plist('CFBundleShortVersionString'), plugins, sourceManifests,
});
loadConfig(configFile);
const quote = text => "'" + text.replaceAll("'", "'\\''") + "'";
const launcher = `#!/bin/sh\nexport LAZYMIND_CODEX_DESKTOP_CONFIG=${quote(configFile)}\nexec ${quote(node)} ${quote(path.join(bundleDir, 'adapter.mjs'))} "$@"\n`;
fs.writeFileSync(path.join(output, 'launch-adapter'), launcher, { mode: 0o700 });
process.stdout.write(JSON.stringify({ config: configFile, launcher: path.join(output, 'launch-adapter'), runtimeDir,
  endpoint: `ws+unix://localhost${runtimeDir}/daemon.sock:/rpc`,
  next: [node, path.join(bundleDir, 'manage.mjs'), 'enable', '--config', configFile],
  enabled: false,
}, null, 2) + '\n');
