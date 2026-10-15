"""Start/reuse one owned App Server. Never stop it when Desktop disconnects.

The kernel lock and process/socket checks come from the successful Desktop probe.
Python is used for flock; all WebSocket traffic uses the bundled Node transport.
"""
import fcntl
import json
import os
import pathlib
import stat
import subprocess
import sys
import time

CONFIG = pathlib.Path(os.environ['LAZYMIND_CODEX_DESKTOP_CONFIG'])
SETTINGS = json.loads(CONFIG.read_text())
ROOT = pathlib.Path(SETTINGS['runtimeDir'])
BUNDLE = pathlib.Path(SETTINGS['bundleDir'])
CLI = SETTINGS['cli']
NODE = SETTINGS['node']
os.umask(0o077)
DESKTOP_ENV = [
    'CODEX_CLI_PATH', 'CODEX_APP_SERVER_FORCE_CLI', 'CODEX_APP_SERVER_WS_URL',
    'CODEX_APP_SERVER_USE_LOCAL_DAEMON', 'CODEX_APP_TOOLS_PIPE_PATH',
    'CODEX_TASK_WORKSPACE_VERIFYING_IDENTITY', 'BROWSER_USE_ACCESS_VERIFYING_IDENTITY',
]


def log(event, **fields):
    file = ROOT / 'coordinator.jsonl'
    if file.exists() and file.stat().st_size > 2 * 1024 * 1024:
        file.replace(ROOT / 'coordinator.jsonl.1')
    with file.open('a') as stream:
        stream.write(json.dumps(dict(time=time.time(), event=event, **fields)) + '\n')


def identity(pid):
    result = subprocess.run(
        ['/bin/ps', '-p', str(pid), '-o', 'pid=,uid=,lstart=,command='],
        capture_output=True, text=True, check=True, timeout=3,
    )
    parts = result.stdout.strip().split(None, 7)
    if len(parts) != 8:
        raise RuntimeError('Backend process identity is unavailable')
    return dict(pid=int(parts[0]), uid=int(parts[1]), start=' '.join(parts[2:7]), command=parts[7])


def same(record):
    try:
        return bool(record) and identity(record['pid']) == record
    except subprocess.CalledProcessError:
        return False


def atomic(name, value):
    temporary = ROOT / (name + '.' + str(os.getpid()) + '.tmp')
    temporary.write_text(json.dumps(value, indent=2))
    temporary.chmod(0o600)
    temporary.replace(ROOT / name)


def toml(value):
    if isinstance(value, bool):
        return 'true' if value else 'false'
    if isinstance(value, str):
        return json.dumps(value)
    if isinstance(value, list):
        return '[' + ','.join(toml(item) for item in value) + ']'
    if isinstance(value, dict):
        return '{' + ','.join(json.dumps(key) + '=' + toml(item) for key, item in value.items() if item is not None) + '}'
    return str(value)


def argv():
    args = [CLI, '-c', 'features.code_mode_host=true', 'app-server', '--listen', 'unix://' + str(ROOT / 'daemon.sock')]
    for plugin in ['codex-app-tools@openai-bundled', 'unified-computer-use@openai-bundled']:
        args += ['-c', 'plugins.' + json.dumps(plugin) + '.enabled=false']
    for kind in ['codex_app', 'cua_repl']:
        # Preserve the original approval and tool policy; replace only transport.
        plugin = {
            **SETTINGS['plugins'][kind], 'command': NODE,
            'args': [str(BUNDLE / 'mcp-proxy.mjs'), kind], 'cwd': str(ROOT),
            'env': {'LAZYMIND_CODEX_DESKTOP_CONFIG': str(CONFIG)},
            'env_vars': [], 'enabled': True,
        }
        args += ['-c', 'mcp_servers.' + kind + '=' + toml(plugin)]
    return args


def socket_identity():
    endpoint = ROOT / 'daemon.sock'
    try:
        entry = endpoint.lstat()
    except FileNotFoundError:
        return None
    if entry.st_uid != os.getuid():
        raise RuntimeError('Endpoint owner differs from current user')
    link = [entry.st_dev, entry.st_ino]
    if stat.S_ISLNK(entry.st_mode):
        target = endpoint.resolve(strict=False)
        native = pathlib.Path('/private/tmp') / ('codex-daemon-' + str(os.getuid()))
        if target.parent != native:
            raise RuntimeError('Unexpected native socket target directory')
        parent = native.stat()
        if parent.st_uid != os.getuid() or parent.st_mode & 0o022:
            raise RuntimeError('Unsafe native socket directory owner/mode')
    elif stat.S_ISSOCK(entry.st_mode):
        target = endpoint
    else:
        raise RuntimeError('Endpoint is neither Unix socket nor native socket link')
    try:
        socket = target.lstat()
    except FileNotFoundError:
        return {'endpoint': link, 'target': str(target), 'socket': None}
    if not stat.S_ISSOCK(socket.st_mode) or socket.st_uid != os.getuid() or socket.st_mode & 0o077:
        raise RuntimeError('Unsafe target socket type, owner or mode')
    return {'endpoint': link, 'target': str(target), 'socket': [socket.st_dev, socket.st_ino]}


def ready(record):
    if not same(record['process']):
        raise RuntimeError('Owned backend identity changed')
    if not record.get('socket') or record['socket'] != socket_identity():
        raise RuntimeError('Backend socket identity changed; refusing takeover')
    result = subprocess.run(
        ['/usr/sbin/lsof', '-a', '-p', str(record['process']['pid']), '-U', '-Fn'],
        capture_output=True, text=True, timeout=3,
    )
    if 'n' + record['socket']['target'] not in result.stdout.splitlines():
        raise RuntimeError('Socket listener does not belong to recorded backend')
    subprocess.run([NODE, str(BUNDLE / 'health.mjs')], check=True, capture_output=True, text=True, timeout=8)
    return record


def remove_stale_endpoint(old):
    socket = socket_identity()
    if not socket:
        return
    if (not old or not old.get('socket')
            or any(old['socket'].get(key) != socket.get(key) for key in ['endpoint', 'target'])
            or (socket['socket'] is not None and old['socket'].get('socket') != socket['socket'])):
        raise RuntimeError('Unknown socket; refusing removal or connection')
    # A dead recorded owner is necessary, and no process may still hold the socket.
    if socket['socket'] is not None:
        result = subprocess.run(['/usr/sbin/lsof', '-t', socket['target']], capture_output=True, text=True, timeout=3)
        if result.returncode != 1 or result.stdout.strip():
            raise RuntimeError('Stale endpoint may still be held; refusing removal')
        pathlib.Path(socket['target']).unlink()
    if (ROOT / 'daemon.sock').is_symlink():
        (ROOT / 'daemon.sock').unlink()


def start():
    env = os.environ.copy()
    for key in DESKTOP_ENV:
        env.pop(key, None)
    env.update(CODEX_CLI_PATH=CLI, CODEX_HOME=SETTINGS['codexHome'], LAZYMIND_CODEX_DESKTOP_CONFIG=str(CONFIG))
    with (ROOT / 'daemon.log').open('ab', buffering=0) as output:
        process = subprocess.Popen(argv(), env=env, stdin=subprocess.DEVNULL, stdout=output, stderr=output,
                                   start_new_session=True, close_fds=True)
    record = {'process': identity(process.pid), 'socket': None}
    atomic('backend.json', record)
    log('spawn', pid=process.pid)
    try:
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            if process.poll() is not None:
                raise RuntimeError('Backend exited during startup; see daemon.log')
            socket = socket_identity()
            if socket and socket['socket'] is not None:
                record['socket'] = socket
                atomic('backend.json', record)
                ready(record)
                log('ready', pid=process.pid)
                return record
            time.sleep(.1)
        raise TimeoutError('Backend readiness timed out (15s); no private fallback')
    except Exception:
        # Readiness failure releases only the process created in this attempt.
        if same(record['process']):
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                pass  # Keep ownership evidence; never kill an unverified replacement.
        raise


def ensure():
    # flock is released by the kernel on crash, including before publishing ownership.
    with (ROOT / 'startup.lock').open('a') as lock:
        os.set_inheritable(lock.fileno(), False)
        deadline = time.monotonic() + 20
        while True:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() > deadline:
                    raise TimeoutError('Backend startup lock timed out (20s)')
                time.sleep(.1)
        owner = ROOT / 'backend.json'
        old = json.loads(owner.read_text()) if owner.exists() else None
        if old and same(old['process']):
            ready(old)
            log('reuse', pid=old['process']['pid'])
            return old
        remove_stale_endpoint(old)
        return start()


if __name__ == '__main__':
    try:
        print(json.dumps(ensure()))
    except Exception as error:
        log('failure', error=str(error))
        print('Shared backend unavailable: ' + str(error), file=sys.stderr)
        sys.exit(1)
