from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import time
import urllib.error
import urllib.request
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from .experiment import content_ref, content_uri, write_json
from .source import source_hash


DEMO_ENTRY = Path('demo/run_demo.py')
_DEMO_BOOTSTRAP = r'''
import os
import runpy
import sys

_WRITE_FLAGS = os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND
_MUTATIONS = {
    'os.remove', 'os.rename', 'os.renames', 'os.replace', 'os.rmdir', 'os.mkdir',
    'os.chmod', 'os.chown', 'os.link', 'os.symlink', 'os.truncate',
}

def _guard(event, args):
    if event.startswith('socket.'):
        raise PermissionError('demo_network_blocked')
    if event in {'subprocess.Popen', 'os.system', 'os.posix_spawn', 'os.posix_spawnp'}:
        raise PermissionError('demo_process_spawn_blocked')
    if event in _MUTATIONS:
        raise PermissionError('demo_filesystem_write_blocked')
    if event == 'open' and len(args) > 2 and isinstance(args[2], int) and args[2] & _WRITE_FLAGS:
        raise PermissionError('demo_filesystem_write_blocked')

sys.addaudithook(_guard)
entry, input_path, *source_paths = sys.argv[1:]
sys.path[:0] = source_paths
sys.argv = [entry, '--input', input_path]
runpy.run_path(entry, run_name='__main__')
'''
ANSI = re.compile(r'\x1b\[[0-?]*[ -/]*[@-~]')
SECRET = re.compile(
    r"(?i)([\"']?(?:api[_-]?key|token|secret|password|authorization)[\"']?\s*[:=]\s*)"
    r"([\"']?)([^\"'\s,;}]+)(\2)"
)


def capture_live_probes(urls: Sequence[str], allowed_origins: Sequence[str], artifact_root: Path,
                        timeout_seconds: float = 5.0) -> dict[str, Any]:
    allowed = {_origin(value) for value in allowed_origins}
    results = []
    for raw_url in urls:
        url = str(raw_url).strip()
        if _origin(url) not in allowed:
            raise ValueError(f'demo_live_url_not_allowed:{url}')
        started = time.monotonic()
        status_code = None
        error_type = ''
        try:
            with urllib.request.urlopen(url, timeout=timeout_seconds) as response:
                status_code = int(response.status)
        except urllib.error.HTTPError as exc:
            status_code = int(exc.code)
        except (urllib.error.URLError, TimeoutError, ConnectionError, OSError) as exc:
            error_type = type(exc).__name__
        results.append({
            'url': url,
            'reachable': status_code is not None,
            'status_code': status_code,
            'error_type': error_type,
            'duration_ms': int((time.monotonic() - started) * 1000),
        })
    path = artifact_root / 'inputs' / 'live-probes.json'
    write_json(path, {'results': results})
    return {'results': results, 'ref': content_ref(path, artifact_root)}


def seal_demo(work_root: Path, artifact_root: Path, expected_source_hash: str) -> dict[str, Any]:
    if source_hash(work_root / 'source') != expected_source_hash:
        raise ValueError('source_changed')
    entry = work_root / DEMO_ENTRY
    if not entry.is_file():
        raise ValueError('demo_entry_missing')
    files = sorted(path for path in (work_root / 'demo').rglob('*') if path.is_file())
    if not files:
        raise ValueError('demo_empty')
    destination = artifact_root / 'demo'
    shutil.rmtree(destination, ignore_errors=True)
    shutil.copytree(work_root / 'demo', destination)
    digest = _demo_hash(work_root / 'demo')
    return {
        'sha256': digest,
        'demo_ref': {'uri': content_uri(destination, artifact_root), 'sha256': digest},
        'files': [path.relative_to(work_root).as_posix() for path in files],
    }


def demo_readiness(work_root: Path, spec: Mapping[str, Any], inputs: Sequence[Mapping[str, Any]],
                   sealed_demo: Mapping[str, Any], opencode_report: Mapping[str, Any], remaining_runs: int
                   ) -> dict[str, str]:
    expected = spec.get('expected') if isinstance(spec.get('expected'), Mapping) else {}
    required_inputs = {str(item.get('name') or '') for item in spec.get('inputs') or () if isinstance(item, Mapping)}
    actual_inputs = {str(item.get('name') or '') for item in inputs}
    reasons = []
    if not spec.get('demo_method'):
        reasons.append('spec_demo_method_missing')
    if not required_inputs or not required_inputs.issubset(actual_inputs):
        reasons.append('spec_inputs_missing')
    if not list(expected.get('must_observe') or ()):
        reasons.append('must_observe_missing')
    if not (work_root / DEMO_ENTRY).is_file() or not sealed_demo.get('sha256'):
        reasons.append('demo_not_sealed')
    if opencode_report.get('status') != 'completed':
        reasons.append('opencode_report_invalid')
    if remaining_runs < len(inputs):
        reasons.append('demo_run_budget_exhausted')
    demo_text = '\n'.join(
        path.read_text(encoding='utf-8', errors='replace')
        for path in (work_root / 'demo').rglob('*.py')
        if path.is_file()
    )
    if spec.get('live_urls') and '_repair_live_probes' not in demo_text:
        reasons.append('demo_does_not_consume_live_probes')
    return {'status': 'not_ready', 'reason': reasons[0]} if reasons else {'status': 'ready', 'reason': ''}


def run_demo(work_root: Path, artifact_root: Path, inputs: Sequence[Mapping[str, Any]], *, attempt: int,
             timeout_seconds: float, output_limit: int, expected_source_hash: str,
             expected_demo_hash: str) -> tuple[dict[str, Any], ...]:
    records = []
    deadline = time.monotonic() + max(0.0, timeout_seconds)
    for item in inputs:
        name = str(item.get('name') or '')
        failure = _integrity_failure(work_root, item, expected_source_hash, expected_demo_hash)
        if failure:
            record = _failed_record(attempt, name, failure)
            _write_run_record(artifact_root, record)
            records.append(record)
            break
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            record = _failed_record(attempt, name, 'timeout')
            _write_run_record(artifact_root, record)
            records.append(record)
            break
        record = _run_one_demo(
            work_root, artifact_root, item, attempt=attempt,
            timeout_seconds=remaining, output_limit=output_limit,
        )
        failure = _integrity_failure(work_root, item, expected_source_hash, expected_demo_hash)
        if failure:
            record = {**record, 'status': failure, 'output_ref': None, '_output': None}
            (artifact_root / 'outputs' / f'run-{attempt:02d}-{name}.json').unlink(missing_ok=True)
            _write_run_record(artifact_root, record)
        records.append(record)
        if failure:
            break
    return tuple(records)


def _run_one_demo(work_root: Path, artifact_root: Path, item: Mapping[str, Any], *, attempt: int,
                  timeout_seconds: float, output_limit: int) -> dict[str, Any]:
    name = str(item['name'])
    prefix = f'run-{attempt:02d}-{name}'
    stdout_raw = work_root / 'logs' / 'runner' / f'{prefix}.stdout.raw'
    stderr_raw = work_root / 'logs' / 'runner' / f'{prefix}.stderr.raw'
    command = [
        sys.executable, '-I', '-c', _DEMO_BOOTSTRAP,
        str(DEMO_ENTRY), str(item['path']),
        str(work_root / 'source'),
        str(work_root / 'source' / 'algorithm'),
        str(work_root / 'source' / 'algorithm' / 'lazyllm'),
    ]
    env = {
        'PATH': os.environ.get('PATH', ''),
        'LANG': os.environ.get('LANG', 'C.UTF-8'),
        'PYTHONNOUSERSITE': '1',
        'PYTHONDONTWRITEBYTECODE': '1',
        'PYTHONPATH': os.pathsep.join((
            str(work_root / 'source'),
            str(work_root / 'source' / 'algorithm'),
            str(work_root / 'source' / 'algorithm' / 'lazyllm'),
        )),
    }
    started = time.monotonic()
    with stdout_raw.open('wb') as stdout, stderr_raw.open('wb') as stderr:
        process = subprocess.Popen(command, cwd=str(work_root), env=env, stdout=stdout, stderr=stderr,
                                   start_new_session=True)
        timed_out = False
        try:
            exit_code = process.wait(timeout=timeout_seconds)
        except subprocess.TimeoutExpired:
            timed_out = True
            _terminate(process)
            exit_code = -1
    duration_ms = int((time.monotonic() - started) * 1000)
    stdout_bytes, stderr_bytes = stdout_raw.read_bytes(), stderr_raw.read_bytes()
    stdout_text, stdout_truncated = _sanitize_log(stdout_bytes, output_limit)
    stderr_text, stderr_truncated = _sanitize_log(stderr_bytes, output_limit)
    stdout_path = artifact_root / 'logs' / 'runner' / f'{prefix}.stdout.log'
    stderr_path = artifact_root / 'logs' / 'runner' / f'{prefix}.stderr.log'
    stdout_path.write_text(stdout_text, encoding='utf-8')
    stderr_path.write_text(stderr_text, encoding='utf-8')
    payload: dict[str, Any] | None = None
    try:
        candidate = json.loads(stdout_text)
        if isinstance(candidate, dict):
            payload = candidate
    except json.JSONDecodeError:
        pass
    oversized = len(stdout_bytes) > output_limit or len(stderr_bytes) > output_limit
    status = (
        'timeout' if timed_out else
        'output_too_large' if oversized else
        'nonzero_exit' if exit_code else
        'invalid_output' if payload is None else
        'completed'
    )
    output_ref = None
    if payload is not None:
        output_path = artifact_root / 'outputs' / f'{prefix}.json'
        write_json(output_path, payload)
        output_ref = content_ref(output_path, artifact_root)
    record = {
        'attempt': attempt,
        'input_name': name,
        'status': status,
        'exit_code': exit_code,
        'duration_ms': duration_ms,
        'output_ref': output_ref,
        'stdout_ref': content_ref(stdout_path, artifact_root),
        'stderr_ref': content_ref(stderr_path, artifact_root),
        'stdout_truncated': stdout_truncated,
        'stderr_truncated': stderr_truncated,
    }
    _write_run_record(artifact_root, record)
    stdout_raw.unlink(missing_ok=True)
    stderr_raw.unlink(missing_ok=True)
    return {**record, '_output': payload}


def _failed_record(attempt: int, name: str, status: str) -> dict[str, Any]:
    return {
        'attempt': attempt, 'input_name': name, 'status': status, 'exit_code': -1,
        'duration_ms': 0, 'output_ref': None, 'stdout_ref': None, 'stderr_ref': None,
        'stdout_truncated': False, 'stderr_truncated': False,
    }


def _write_run_record(artifact_root: Path, record: Mapping[str, Any]) -> None:
    name = Path(str(record.get('input_name') or 'unknown')).name
    attempt = int(record.get('attempt') or 0)
    write_json(
        artifact_root / 'logs' / 'runner' / f'run-{attempt:02d}-{name}.json',
        {key: value for key, value in record.items() if key != '_output'},
    )


def _terminate(process: subprocess.Popen[Any]) -> None:
    if process.poll() is not None:
        return
    try:
        os.killpg(os.getpgid(process.pid), signal.SIGTERM)
        process.wait(timeout=2)
    except (OSError, subprocess.TimeoutExpired):
        try:
            os.killpg(os.getpgid(process.pid), signal.SIGKILL)
        except OSError:
            process.kill()
        process.wait(timeout=2)


def _sanitize_log(raw: bytes, limit: int) -> tuple[str, bool]:
    text = ANSI.sub('', raw.decode('utf-8', errors='replace'))
    text = ''.join(char for char in text if char in '\n\r\t' or ord(char) >= 32)
    text = SECRET.sub(r'\1\2<redacted>\4', text)
    encoded = text.encode('utf-8')
    if len(encoded) <= limit:
        return text, False
    half = max(1, limit // 2)
    clipped = encoded[:half] + b'\n...<truncated>...\n' + encoded[-half:]
    return clipped.decode('utf-8', errors='replace'), True


def _integrity_failure(work_root: Path, item: Mapping[str, Any], expected_source_hash: str,
                       expected_demo_hash: str) -> str:
    if source_hash(work_root / 'source') != expected_source_hash:
        return 'source_changed'
    if _demo_hash(work_root / 'demo') != expected_demo_hash:
        return 'demo_changed'
    ref = item.get('ref') if isinstance(item.get('ref'), Mapping) else {}
    path = Path(str(item.get('path') or ''))
    if not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != ref.get('sha256'):
        return 'input_changed'
    return ''


def _demo_hash(root: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(item for item in root.rglob('*') if item.is_file()):
        digest.update(path.relative_to(root).as_posix().encode())
        digest.update(b'\0')
        digest.update(path.read_bytes())
    return digest.hexdigest()


def _origin(value: str) -> str:
    parsed = urlsplit(str(value).strip())
    if parsed.scheme not in {'http', 'https'} or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError(f'demo_live_url_invalid:{value}')
    port = parsed.port or (443 if parsed.scheme == 'https' else 80)
    return f'{parsed.scheme}://{parsed.hostname}:{port}'
