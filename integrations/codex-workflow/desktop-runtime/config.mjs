import fs from 'node:fs';
import path from 'node:path';
import { execFileSync } from 'node:child_process';

export const configVariable = 'LAZYMIND_CODEX_DESKTOP_CONFIG';
export const kinds = ['codex_app', 'cua_repl'];
export const desktopEnvironment = [
  'CODEX_CLI_PATH', 'CODEX_APP_SERVER_FORCE_CLI', 'CODEX_APP_SERVER_WS_URL',
  'CODEX_APP_SERVER_USE_LOCAL_DAEMON', 'CODEX_APP_TOOLS_PIPE_PATH',
  'CODEX_TASK_WORKSPACE_VERIFYING_IDENTITY', 'BROWSER_USE_ACCESS_VERIFYING_IDENTITY',
];

export function privateDirectory(dir) {
  fs.mkdirSync(dir, { recursive: true, mode: 0o700 });
  const info = fs.lstatSync(dir);
  if (!info.isDirectory() || info.uid !== process.getuid() || (info.mode & 0o077)) {
    throw new Error(`Expected a private directory owned by this user: ${dir}`);
  }
}

export function loadConfig(file = process.env[configVariable]) {
  if (!file || !path.isAbsolute(file)) throw new Error(`${configVariable} must name an absolute config file`);
  const info = fs.lstatSync(file);
  if (!info.isFile() || info.uid !== process.getuid() || (info.mode & 0o077)) throw new Error('Runtime config must be a private owned file');
  const config = JSON.parse(fs.readFileSync(file, 'utf8'));
  if (config.version !== 1) throw new Error('Unsupported Desktop runtime config');
  for (const key of ['runtimeDir', 'bundleDir', 'cli', 'node', 'python', 'desktopExecutable', 'codexHome']) {
    if (typeof config[key] !== 'string' || !path.isAbsolute(config[key])) throw new Error(`Invalid ${key}`);
  }
  for (const kind of kinds) {
    if (!config.plugins?.[kind]?.command || !Array.isArray(config.plugins[kind].args)) throw new Error(`Missing original ${kind} configuration`);
  }
  // macOS sockaddr_un.sun_path has 104 bytes, including the trailing NUL.
  if (Buffer.byteLength(path.join(config.runtimeDir, 'bridge-2147483647', 'codex_app.sock')) >= 104) throw new Error('Runtime socket path is too long');
  privateDirectory(config.runtimeDir);
  return { ...config, configFile: file };
}

export function processIdentity(pid) {
  if (!Number.isSafeInteger(pid) || pid < 1) return null;
  try {
    return execFileSync('/bin/ps', ['-p', String(pid), '-o', 'lstart=,comm='], {
      encoding: 'utf8', timeout: 2000, stdio: ['ignore', 'pipe', 'ignore'],
    }).trim() || null;
  } catch { return null; }
}

export function atomicJSON(file, value) {
  const temporary = `${file}.${process.pid}.tmp`;
  fs.writeFileSync(temporary, JSON.stringify(value, null, 2) + '\n', { mode: 0o600 });
  fs.renameSync(temporary, file);
}

// Only operational fields are logged. Never log RPC bodies, tool results or identities.
export function logger(config, name) {
  const file = path.join(config.runtimeDir, `${name}.jsonl`);
  return (event, fields = {}) => {
    try {
      if (fs.existsSync(file) && fs.statSync(file).size > 2 * 1024 * 1024) fs.renameSync(file, `${file}.1`);
      fs.appendFileSync(file, JSON.stringify({ time: new Date().toISOString(), pid: process.pid, event, ...fields }) + '\n', { mode: 0o600 });
    } catch { /* A diagnostic write must not break the transport. */ }
  };
}
