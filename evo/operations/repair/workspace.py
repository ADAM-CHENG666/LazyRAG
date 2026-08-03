from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from evo.operations.public_contracts import clean_text as _text

from .source import ALGORITHM_APP, copy_source, source_hash, source_root



def workspace_path(policy: Mapping[str, Any], plan: Mapping[str, Any]) -> Path:
    base = (Path(os.getenv('LAZYMIND_EVO_BASE_DIR') or '/var/lib/lazymind/evo') / 'work' / 'repair').resolve()
    namespace = ''.join(
        ch if ch.isalnum() or ch in '._-' else '_'
        for ch in _text(policy.get('workspace_namespace') or policy.get('thread_id') or 'shared')
    ).strip('._-') or 'shared'
    digest = hashlib.sha256(json.dumps(
        {'category_id': plan.get('category_id'), 'method': plan.get('method')},
        sort_keys=True,
        default=str,
    ).encode()).hexdigest()[:16]
    if policy.get('candidate_workdir'):
        workspace = Path(_text(policy['candidate_workdir'])).resolve()
        expected = base / namespace / digest / 'candidate'
        if workspace != expected:
            raise RuntimeError(f'candidate workspace must match managed repair candidate path: {expected}')
        return workspace
    return base / namespace / digest / 'candidate'


def prepare_workspace(source: Path, workspace: Path, objective_hash: str = '') -> None:
    if not (source / ALGORITHM_APP).exists():
        raise RuntimeError(f'candidate source is not LazyRAG algorithm dir: {source}')
    source, workspace = source.resolve(), workspace.resolve()
    if source == workspace or source in workspace.parents or workspace in source.parents:
        raise RuntimeError(f'candidate workspace must be outside source tree: {workspace}')
    fingerprint = source_fingerprint(source)
    if objective_hash:
        fingerprint['objective_hash'] = objective_hash
    if workspace.exists() and workspace_fingerprint(workspace) != fingerprint:
        shutil.rmtree(workspace)
    created = not workspace.exists()
    if created:
        copy_source(source, workspace)
    elif (workspace / '.git').exists():
        reset_workspace(workspace)
    if not (workspace / ALGORITHM_APP).exists():
        raise RuntimeError(f'candidate workspace is not LazyRAG algorithm dir: {workspace}')
    _ensure_git(workspace, created)
    _write_workspace_fingerprint(workspace, fingerprint)
    reset_workspace(workspace)


def algorithm_source_root(value: Any) -> Path:
    return source_root(_text(value))


def reset_workspace(workspace: Path) -> None:
    git(workspace, 'reset', '--hard', 'HEAD')
    git(workspace, 'clean', '-fd', '-e', '.evo_repair_logs', '--', '.')


def workspace_diff(workspace: Path) -> dict[str, Any]:
    untracked = [path for path in git(workspace, 'ls-files', '--others', '--exclude-standard').splitlines()
                 if path and path != 'opencode.json'
                 and not path.startswith('.evo_repair_logs/') and not path.endswith('.pyc')]
    if untracked:
        git(workspace, 'add', '-N', '--', *untracked)
    return {
        'diff': git(workspace, 'diff', '--', strip=False),
        'files': git(workspace, 'diff', '--name-only').splitlines(),
    }


def git(workspace: Path, *args: str, strip: bool = True) -> str:
    result = subprocess.run(['git', '-c', f'safe.directory={workspace}', '-C', str(workspace), *args],
                            capture_output=True, text=True, timeout=60, check=False)
    if result.returncode:
        raise RuntimeError((result.stderr or result.stdout).strip())
    return result.stdout.strip() if strip else result.stdout


def source_fingerprint(source: Path) -> dict[str, str]:
    return {'source_dir': str(source), 'source_hash': source_hash(source)}


def workspace_fingerprint(workspace: Path) -> dict[str, str]:
    try:
        value = json.loads((workspace / '.git' / 'evo_repair_source.json').read_text(encoding='utf-8'))
        return value if isinstance(value, dict) else {}
    except (FileNotFoundError, json.JSONDecodeError, TypeError):
        return {}


def _ensure_git(workspace: Path, created: bool) -> None:
    if not (workspace / '.git').exists():
        git(workspace, 'init')
    if _git_code(workspace, 'rev-parse', '--verify', 'HEAD'):
        git(workspace, 'add', '.')
        git(workspace, '-c', 'user.email=evo@example.local', '-c', 'user.name=evo', 'commit', '-m', 'baseline')
    elif _git_code(workspace, 'diff', '--quiet', '--') and created:
        git(workspace, 'add', '.')
        git(workspace, '-c', 'user.email=evo@example.local', '-c', 'user.name=evo', 'commit', '-m', 'repair baseline')
    elif _git_code(workspace, 'diff', '--quiet', '--'):
        raise RuntimeError(f'existing repair workspace has dirty tracked files: {workspace}')


def _git_code(workspace: Path, *args: str) -> int:
    return subprocess.run(['git', '-c', f'safe.directory={workspace}', '-C', str(workspace), *args],
                          capture_output=True, text=True, timeout=60, check=False).returncode


def _write_workspace_fingerprint(workspace: Path, value: Mapping[str, str]) -> None:
    (workspace / '.git' / 'evo_repair_source.json').write_text(
        json.dumps(dict(value), sort_keys=True),
        encoding='utf-8',
    )
