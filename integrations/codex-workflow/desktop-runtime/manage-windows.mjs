import fs from 'node:fs';
import path from 'node:path';
import { native, checkPrivate, desktopRunning } from './platform.mjs';
import { atomicJSON } from './config.mjs';
import { registerWorkflow, unregisterWorkflow, registrationPath, workflowRegistration } from './workflow-registration.mjs';
import { restoreGeneratedPaths } from './restore-paths.mjs';

// User environment only: retain original icon, executable and signature.
export function manageWindows(action, config, ops = {}) {
  const callNative = ops.native ?? native;
  const register = ops.register ?? registerWorkflow;
  const unregister = ops.unregister ?? unregisterWorkflow;
  const restore = ops.restore ?? restoreGeneratedPaths;
  const running = ops.running ?? desktopRunning;
  const desired = { CODEX_CLI_PATH: config.launcher, CODEX_APP_SERVER_FORCE_CLI: '1',
    CODEX_APP_SERVER_WS_URL: null, CODEX_APP_SERVER_USE_LOCAL_DAEMON: null };
  const stateFile = path.join(path.dirname(config.configFile), 'activation.json');
  const state = fs.existsSync(stateFile) ? (checkPrivate(stateFile), JSON.parse(fs.readFileSync(stateFile, 'utf8'))) : null;
  if (state && state.config !== config.configFile) throw new Error('Activation belongs to another installation');
  const current = () => callNative('environment-read', {}, config.helper);
  const set = snapshot => callNative('environment-write', snapshot, config.helper);
  const desiredSnapshot = { values: desired, types: Object.fromEntries(Object.entries(desired).filter(([,v]) => v !== null).map(([key]) => [key, 1])) };
  const matches = (a,b) => Object.keys(desired).every(key => a.values[key] === b.values[key] &&
    (a.values[key] === null || a.types[key] === b.types[key]));
  if (action === 'plan') return console.log(JSON.stringify({ environment: desired, before: current(),
    workflowRegistration: { path: registrationPath(config), value: workflowRegistration(config) } }));
  if (action === 'status') return console.log(JSON.stringify({ enabled: !!state, environmentMatches: matches(current(), desiredSnapshot) }));
  if (action === 'enable') {
    if (state) {
      if (!matches(current(), desiredSnapshot)) throw new Error('Startup environment changed externally');
      register(config);
    } else {
      const before = current();
      atomicJSON(stateFile, { config: config.configFile, before });
      let registered = false;
      try { register(config); registered = true; set(desiredSnapshot); }
      catch (error) {
        // Keep recovery evidence when restore itself fails.
        set(before);
        if (registered) unregister(config);
        fs.unlinkSync(stateFile); throw error;
      }
    }
    console.log('Shared mode configured. Reopen Codex from its original icon; if Explorer retains the old environment, sign out and back in once.');
  } else if (action === 'disable') {
    if (state) {
      if (running(config)) throw new Error('Quit Codex normally before disconnecting');
      const now = current();
      if (!matches(now, desiredSnapshot) && !matches(now, state.before)) throw new Error('Startup environment changed externally; refusing overwrite');
      set(state.before); unregister(config); restore(config, config.launcher); fs.unlinkSync(stateFile);
    }
    console.log('Original user environment restored; backend retained.');
  } else throw new Error('Unsupported Windows activation operation');
}
