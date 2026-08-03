from __future__ import annotations

import ast
import re
import shlex
import subprocess
import sys
from collections.abc import Mapping
from pathlib import Path, PurePosixPath
from typing import Any

from evo.artifact_runtime import record_event


DOMAIN_ROOTS = ('algorithm/lazymind/chat', 'algorithm/lazymind/parsing')
DEFAULT_BLOCKED_ROOTS = ('.git', 'data', 'evo', 'tests', 'algorithm/lazyllm')
DEFAULT_VERIFY = ('python -m compileall -q algorithm/lazymind/chat algorithm/lazymind/parsing',)
PATCH_BYTE_LIMIT = 64 * 1024
SECRET_LITERAL = re.compile(
    r'(?i)[\'"]?(api[_-]?key|token|secret|password|authorization)[\'"]?\s*[:=]\s*'
    r'([\'"]?)(?!<redacted>|unused\b|os\.getenv\b|getenv\b)[A-Za-z0-9._~+/=-]{8,}\2'
)


def pre_validate(root: Path, diff_info: Mapping[str, Any], plan: Mapping[str, Any],
                 analysis: Mapping[str, Any], policy: Mapping[str, Any],
                 attempt: int | None = None) -> dict[str, Any]:
    record_event('verify.pre_validation_started', status='started', attempt=attempt)
    diff, files = str(diff_info.get('diff') or ''), list(diff_info.get('files') or ())
    checks = [
        _scope_check(files, plan, policy),
        _hardcode_check(diff, analysis, plan),
        _safety_check(diff, policy),
        _python_change_check(root, diff, files),
    ]
    failed = next((check for check in checks if check['status'] != 'passed'), None)
    commands = {'status': 'skipped', 'reason': '', 'results': []}
    if failed is None:
        commands = _run_verification(root, policy, attempt)
        failed = commands if commands['status'] != 'passed' else None
    status, reason = ('passed', '') if failed is None else ('failed', str(failed['reason']))
    record_event(
        'verify.pre_validation_completed',
        status='completed' if status == 'passed' else 'failed',
        attempt=attempt,
        data={'outcome': status, 'reason': reason},
    )
    return {
        'status': status,
        'reason': reason,
        'checks': checks,
        'commands': commands['results'],
    }


def _scope_check(files: list[str], plan: Mapping[str, Any], policy: Mapping[str, Any]) -> dict[str, Any]:
    method = plan.get('method') if isinstance(plan.get('method'), Mapping) else {}
    scope_paths = {
        _relative_path(item.get('path'))
        for item in method.get('code_scope') or ()
        if isinstance(item, Mapping) and _relative_path(item.get('path'))
    }
    scope = repair_scope(policy.get('allowed_roots'), policy.get('blocked_roots'))
    allowed, blocked = scope['allowed_roots'], scope['blocked_roots']
    violations = []
    for raw in files:
        path = _relative_path(raw)
        in_allowed = any(path == root or path.startswith(f'{root}/') for root in allowed)
        in_blocked = any(path == root or path.startswith(f'{root}/') for root in blocked)
        if not path or not in_allowed or in_blocked or path not in scope_paths:
            violations.append(str(raw))
    reason = 'diff_scope_violation' if violations or not files else ''
    return {
        'status': 'failed' if reason else 'passed',
        'reason': reason,
        'violations': violations,
        'allowed_roots': allowed,
        'code_scope': sorted(scope_paths),
    }


def repair_scope(raw_allowed: object, raw_blocked: object) -> dict[str, list[str]]:
    return {
        'allowed_roots': _roots(raw_allowed, DOMAIN_ROOTS),
        'blocked_roots': _roots(raw_blocked, DEFAULT_BLOCKED_ROOTS),
    }


def inside_repair_scope(spans: list[Mapping[str, str]], raw_allowed: object,
                        raw_blocked: object) -> bool:
    scope = repair_scope(raw_allowed, raw_blocked)
    allowed, blocked = scope['allowed_roots'], scope['blocked_roots']
    return bool(allowed) and all(
        any(span['path'] == root or span['path'].startswith(f'{root}/') for root in allowed)
        and not any(span['path'] == root or span['path'].startswith(f'{root}/') for root in blocked)
        for span in spans
    )


def _hardcode_check(diff: str, analysis: Mapping[str, Any], plan: Mapping[str, Any]) -> dict[str, Any]:
    categories = analysis.get('categories') if isinstance(analysis.get('categories'), Mapping) else {}
    category = categories.get(plan.get('category_id')) if isinstance(categories, Mapping) else {}
    cases = category.get('cases') if isinstance(category, Mapping) and isinstance(category.get('cases'), Mapping) else {}
    forbidden = {str(item) for pair in cases.items() for item in pair if str(item)}
    added = '\n'.join(line[1:] for line in diff.splitlines() if line.startswith('+') and not line.startswith('+++'))
    hits = sorted(item for item in forbidden if item in added)
    return {
        'status': 'failed' if hits else 'passed',
        'reason': 'hard_coded_case_or_trace_id' if hits else '',
        'hits': hits,
    }


def _safety_check(diff: str, policy: Mapping[str, Any]) -> dict[str, Any]:
    raw_limit = policy.get('max_patch_bytes', PATCH_BYTE_LIMIT)
    limit = raw_limit if isinstance(raw_limit, int) and not isinstance(raw_limit, bool) else PATCH_BYTE_LIMIT
    limit = min(max(limit, 4096), 2 * 1024 * 1024)
    size = len(diff.encode('utf-8'))
    added = '\n'.join(line[1:] for line in diff.splitlines() if line.startswith('+') and not line.startswith('+++'))
    secrets = sorted({match.group(1).lower() for match in SECRET_LITERAL.finditer(added)})
    reason = 'empty_diff' if not diff.strip() else 'patch_too_large' if size > limit else 'secret_literal_in_patch' if secrets else ''
    return {
        'status': 'failed' if reason else 'passed',
        'reason': reason,
        'bytes': size,
        'limit': limit,
        'secret_keys': secrets,
    }


def _python_change_check(root: Path, diff: str, files: list[str]) -> dict[str, Any]:
    python_files = [path for path in files if path.endswith('.py')]
    if not python_files:
        return {'status': 'failed', 'reason': 'no_python_change', 'files': files}
    changed = []
    for relative in python_files:
        try:
            current = ast.parse((root / relative).read_text(encoding='utf-8'), filename=relative)
        except (OSError, SyntaxError) as exc:
            return {'status': 'failed', 'reason': 'python_ast_parse_failed', 'error_type': type(exc).__name__}
        baseline = _git_show(root, relative)
        if baseline is None:
            changed.append(relative)
            continue
        try:
            previous = ast.parse(baseline, filename=relative)
        except SyntaxError:
            changed.append(relative)
            continue
        if ast.dump(current, include_attributes=False) != ast.dump(previous, include_attributes=False):
            changed.append(relative)
    reason = '' if changed and diff.strip() else 'ast_unchanged'
    return {'status': 'passed' if not reason else 'failed', 'reason': reason, 'files': changed}


def _run_verification(root: Path, policy: Mapping[str, Any], attempt: int | None) -> dict[str, Any]:
    raw = policy.get('verification_commands')
    commands = raw if isinstance(raw, (list, tuple)) else DEFAULT_VERIFY if raw in (None, '') else (raw,)
    results = []
    for item in commands:
        command = shlex.split(item) if isinstance(item, str) else [str(part) for part in item]
        if not command:
            continue
        if command[0] == 'python':
            command[0] = sys.executable
        record_event('verify.command_started', status='started', attempt=attempt, command=' '.join(command[:4]))
        try:
            done = subprocess.run(command, cwd=str(root), capture_output=True, text=True,
                                  timeout=120, check=False)
            result = {'command': command, 'returncode': done.returncode,
                      'stdout': done.stdout[-2000:], 'stderr': done.stderr[-2000:]}
        except Exception as exc:
            result = {'command': command, 'returncode': None, 'stdout': '', 'stderr': str(exc),
                      'error_type': type(exc).__name__}
        results.append(result)
        if result['returncode'] != 0:
            return {'status': 'failed', 'reason': 'verification_command_failed', 'results': results}
    return {'status': 'passed', 'reason': '', 'results': results}


def _git_show(root: Path, path: str) -> str | None:
    result = subprocess.run(
        ['git', '-c', f'safe.directory={root}', '-C', str(root), 'show', f'HEAD:{path}'],
        capture_output=True, text=True, timeout=60, check=False,
    )
    return result.stdout if result.returncode == 0 else None


def _relative_path(value: object) -> str:
    raw = str(value or '').strip()
    parts = raw.strip('/').split('/')
    if not raw or raw.startswith('/') or '\\' in raw or any(part in {'', '.', '..'} for part in parts):
        return ''
    return PurePosixPath(raw).as_posix()


def _roots(value: object, default: tuple[str, ...]) -> list[str]:
    rows = value if isinstance(value, (list, tuple)) else default
    return list(dict.fromkeys(path for item in rows if (path := _relative_path(item))))
