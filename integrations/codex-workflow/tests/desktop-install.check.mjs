import test from 'node:test';
import assert from 'node:assert/strict';
import path from 'node:path';
import { prepareInstallation, activateInstallation, disableInstallation } from '../desktop-runtime/installation.mjs';

function fixture() {
  const profile = '/fixture/profile', root = '/fixture/install';
  let current = null, running = false, failEnable = false, failPrepare = false;
  const configs = new Map(), fingerprints = new Map(), events = [];
  const ops = {
    registration: () => current && { installation: current, profile },
    config: file => { if (!configs.has(file)) throw new Error('unknown config'); return configs.get(file); },
    active: c => c.configFile === current,
    running: () => running,
    prepare: directory => {
      events.push('prepare');
      if (failPrepare) throw new Error('unsupported version');
      const c = { configFile: path.join(directory, 'runtime.json'), codexHome: profile, cliVersion: 'fixed', plugins: { native: 'original' } };
      configs.set(c.configFile, c); return c;
    },
    fingerprint: c => fingerprints.get(c.configFile),
    mark: (c, fingerprint) => fingerprints.set(c.configFile, fingerprint),
    removePrepared: directory => { events.push('remove prepared'); configs.delete(path.join(directory, 'runtime.json')); },
    enable: c => { events.push('enable '+c.configFile); if (failEnable === c.configFile) throw new Error('activation failed'); current = c.configFile; },
    disable: c => { events.push('disable '+c.configFile); assert.equal(current, c.configFile); current = null; },
  };
  const prepare = build => prepareInstallation({ root, profile, app: '/fixture/App.app', build }, ops);
  const activate = plan => activateInstallation(plan, profile, ops);
  return { profile, ops, events, configs, prepare, activate, current: () => current,
    setRunning: value => running = value, failEnable: file => failEnable = file, failPrepare: () => failPrepare = true };
}

test('first install activates only on commit; repeated install reuses the same release even while Desktop runs', () => {
  const f = fixture(), first = f.prepare('v1');
  assert.equal(f.current(), null);
  f.activate(first); f.setRunning(true);
  const repeated = f.prepare('v1'); f.activate(repeated);
  assert.equal(repeated.config, first.config);
  assert.equal(f.current(), first.config);
  assert.ok(!f.events.some(e => e.startsWith('disable')));
});

test('upgrade and disconnect refuse a running Desktop before changing existing activation', () => {
  const f = fixture(), old = f.prepare('v1'); f.activate(old); f.setRunning(true);
  assert.throws(() => f.prepare('v2'), /退出 Codex/);
  assert.throws(() => disableInstallation(f.profile, f.ops), /退出 Codex/);
  assert.equal(f.current(), old.config);
  assert.equal(f.configs.size, 1);
});

test('activation failure restores old release; successful upgrade and disable retain old files', () => {
  const f = fixture(), old = f.prepare('v1'); f.activate(old);
  const next = f.prepare('v2'); f.failEnable(next.config);
  assert.throws(() => f.activate(next), /activation failed/);
  assert.equal(f.current(), old.config);
  f.failEnable(false); f.activate(next);
  assert.equal(f.current(), next.config);
  disableInstallation(f.profile, f.ops);
  assert.equal(f.current(), null);
  assert.ok(f.configs.has(old.config) && f.configs.has(next.config));
});

test('failed preparation leaves old activation; concurrent changes and other profiles are rejected', () => {
  const f = fixture(), old = f.prepare('v1'); f.activate(old);
  f.failPrepare(); assert.throws(() => f.prepare('v2'), /unsupported/);
  assert.equal(f.current(), old.config);
  assert.throws(() => f.activate({ config: old.config, previous: null }), /changed during/);
  f.configs.get(old.config).codexHome = '/another-profile';
  assert.throws(() => disableInstallation(f.profile, f.ops), /another Codex profile/);
});

test('Desktop reopening after preparation blocks upgrade at activation too', () => {
  const f = fixture(), old = f.prepare('v1'); f.activate(old);
  const next = f.prepare('v2'); f.setRunning(true);
  assert.throws(() => f.activate(next), /退出 Codex/);
  assert.equal(f.current(), old.config);
});
