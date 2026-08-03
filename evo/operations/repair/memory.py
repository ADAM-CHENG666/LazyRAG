from __future__ import annotations

import hashlib
import json
import os
import shutil
import tempfile
from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .source import ALGORITHM_APP, copy_source, source_hash, source_root


class WorkMemory:
    """Append-only Phase-1 history backed by a disposable working directory."""

    def __init__(
        self,
        *,
        target: Mapping[str, Any],
        guidance: Sequence[str],
        scope: Mapping[str, Any],
        work_root: Path,
        artifact_root: Path,
        previous_attempts: tuple[Path, ...],
        source_digest: str,
    ) -> None:
        self.target = dict(target)
        self.guidance = [str(item).strip() for item in guidance if str(item).strip()]
        self.scope = dict(scope)
        self.work_root = work_root
        self.artifact_root = artifact_root
        self.previous_attempts = previous_attempts
        self.source_digest = source_digest
        self._records = _load_records((*previous_attempts, artifact_root))
        self._sequence = len(_load_journal(artifact_root))

    @classmethod
    def create(
        cls,
        run_id: str,
        target: Mapping[str, Any],
        policy: Mapping[str, Any],
        source_dir: Path,
        expected_source_hash: str,
        scope: Mapping[str, Any],
    ) -> WorkMemory:
        source = source_root(source_dir)
        if not (source / ALGORITHM_APP).is_file():
            raise ValueError('candidate_source_invalid')
        if source_hash(source) != expected_source_hash:
            raise ValueError('source_hash_mismatch')

        base = Path(
            str(policy.get('phase1_artifact_dir') or '')
            or Path(os.getenv('LAZYMIND_EVO_BASE_DIR') or '/var/lib/lazymind/evo')
            / 'artifacts' / 'repair' / 'phase1'
        ).resolve()
        category_id = _safe_segment(str(target.get('category_id') or ''))
        parent = base / _safe_segment(run_id) / category_id / _target_hash(target)
        parent.mkdir(parents=True, exist_ok=True)
        previous = tuple(
            path for path in sorted(parent.glob('attempt-*'), key=lambda item: item.stat().st_mtime)
            if (path / 'checkpoint.complete').is_file()
        )
        artifact_root = Path(tempfile.mkdtemp(prefix='attempt-', dir=parent)).resolve()
        workspace_key = hashlib.sha256(str(parent).encode('utf-8')).hexdigest()[:20]
        work_root = (Path('/tmp') / f'lazyrag-repair-phase1-{workspace_key}').resolve()
        try:
            # An OpenCode session retains its cwd. Recreate the same path for
            # every attempt of one run/category/target, then restore checkpoint.
            shutil.rmtree(work_root, ignore_errors=True)
            for name in ('source', 'work', 'memory', 'logs'):
                (work_root / name).mkdir(parents=True, exist_ok=True)
            for name in ('events', 'runs', 'web', 'opencode/calls', 'opencode/reports', 'checkpoint'):
                (artifact_root / name).mkdir(parents=True, exist_ok=True)
            copy_source(source, work_root / 'source')
            if previous:
                saved_work = previous[-1] / 'checkpoint' / 'work'
                if saved_work.is_dir():
                    shutil.copytree(saved_work, work_root / 'work', dirs_exist_ok=True)
            metadata = {
                'source_dir': str(source),
                'source_hash': expected_source_hash,
                'category_id': category_id,
                'target_hash': _target_hash(target),
                'resumed_from': previous[-1].name if previous else '',
            }
            write_json(artifact_root / 'metadata.json', metadata)
            write_json(work_root / 'memory' / 'metadata.json', metadata)
            return cls(
                target=target,
                guidance=policy.get('user_guidance') or (),
                scope=scope,
                work_root=work_root,
                artifact_root=artifact_root,
                previous_attempts=previous,
                source_digest=expected_source_hash,
            )
        except Exception:
            cleanup_workdir(work_root)
            shutil.rmtree(artifact_root, ignore_errors=True)
            raise

    @property
    def restored_session(self) -> dict[str, Any]:
        if not self.previous_attempts:
            return {}
        return _read_json(self.previous_attempts[-1] / 'checkpoint' / 'session.json')

    def record(self, event: str, summary: str, data: Mapping[str, Any]) -> dict[str, str]:
        self._sequence += 1
        event_path = self.artifact_root / 'events' / f'{self._sequence:04d}-{_safe_segment(event)}.json'
        payload = {
            'time': datetime.now(timezone.utc).isoformat(),
            'event': event,
            'summary': str(summary).strip()[:2000],
            'data': dict(data),
        }
        write_json(event_path, payload)
        ref = content_ref(event_path, self.artifact_root)
        journal_record = {
            'time': payload['time'],
            'event': event,
            'summary': payload['summary'],
            'file': event_path.relative_to(self.artifact_root).as_posix(),
            'ref': ref,
        }
        with (self.artifact_root / 'journal.jsonl').open('a', encoding='utf-8') as stream:
            stream.write(json.dumps(journal_record, ensure_ascii=False, sort_keys=True) + '\n')
        self._records.append(payload)
        return ref

    def context(self, counters: Mapping[str, int], budget: Mapping[str, int]) -> dict[str, Any]:
        recent = _recent_records(self._records, 36_000)
        return {
            'target': self.target,
            'user_guidance': self.guidance,
            'repair_scope': self.scope,
            'workspace': {
                'source': 'source/ (read-only projection)',
                'work': 'work/ (all experiments and Demo files)',
            },
            # The short history prevents an old result from disappearing merely
            # because the loop took more turns. Detailed data is retained only
            # for the newest useful observations within a fixed prompt budget.
            'history': [
                {
                    'event': item.get('event'),
                    'summary': str(item.get('summary') or '')[:500],
                }
                for item in self._records[-80:]
            ],
            'recent_observations': recent,
            'evidence_refs': self.evidence_refs(),
            'work_files': [
                path.relative_to(self.work_root).as_posix()
                for path in sorted((self.work_root / 'work').rglob('*'))
                if path.is_file()
            ][-80:],
            'budget': dict(budget),
            'used': dict(counters),
        }

    def write_context(self, counters: Mapping[str, int], budget: Mapping[str, int]) -> Path:
        path = self.work_root / 'memory' / 'context.json'
        write_json(path, self.context(counters, budget))
        return path

    def checkpoint(self, session_id: str, calls: int) -> dict[str, str]:
        destination = self.artifact_root / 'checkpoint' / 'work'
        shutil.rmtree(destination, ignore_errors=True)
        shutil.copytree(self.work_root / 'work', destination)
        write_json(
            self.artifact_root / 'checkpoint' / 'session.json',
            {'session_id': str(session_id), 'calls': int(calls)},
        )
        write_json(self.artifact_root / 'checkpoint.complete', {'completed': True})
        return directory_ref(destination, self.artifact_root)

    def evidence_refs(self) -> list[dict[str, str]]:
        result = []
        for record in self._records:
            if record.get('event') not in {'command.result', 'http.result'}:
                continue
            data = record.get('data') if isinstance(record.get('data'), Mapping) else {}
            if data.get('status') != 'completed':
                continue
            ref = data.get('result_ref') if isinstance(data.get('result_ref'), Mapping) else None
            if ref:
                result.append(dict(ref))
        return result[-8:]

    def completion_gaps(self, proposal: Mapping[str, Any]) -> list[str]:
        """Return explicit user tool commitments that have no recorded observation.

        The Agent remains free to design its investigation. This only prevents a
        finish action from claiming completion after the user explicitly required
        a web search/page read that never happened.
        """
        guidance = '\n'.join(self.guidance).casefold()
        proposal_text = json.dumps(proposal, ensure_ascii=False, default=str).casefold()
        if '如果采用 opensearch' in guidance and 'opensearch' not in proposal_text:
            return []
        gaps = []
        if _explicitly_requires(guidance, ('联网搜索', 'web search', 'search the web')):
            searched = any(
                record.get('event') == 'web.search'
                and isinstance(record.get('data'), Mapping)
                and record['data'].get('status') == 'completed'
                and bool(record['data'].get('results'))
                for record in self._records
            )
            if not searched:
                gaps.append('required_web_search_missing')
        if _explicitly_requires(
            guidance,
            ('读取网页', '网页读取', '读取一份', '阅读官方', 'read web', 'read page'),
        ):
            page_read = any(
                record.get('event') == 'web.read'
                and isinstance(record.get('data'), Mapping)
                and any(
                    isinstance(page, Mapping)
                    and page.get('status') == 'readable'
                    and isinstance(page.get('content_ref'), Mapping)
                    for page in record['data'].get('pages') or ()
                )
                for record in self._records
            )
            if not page_read:
                gaps.append('required_web_page_read_missing')
        return gaps

    def consecutive_failures(self, event: str) -> int:
        count = 0
        for record in reversed(self._records):
            current = record.get('event')
            if current in {'agent.decision', 'action.rejected'}:
                continue
            if current != event:
                break
            data = record.get('data') if isinstance(record.get('data'), Mapping) else {}
            if data.get('status') != 'failed':
                break
            count += 1
        return count

    def known_urls(self) -> set[str]:
        urls = {
            str(item.get('url') or '').strip()
            for record in self._records
            if record.get('event') == 'web.search'
            for item in (
                (record.get('data') or {}).get('results')
                if isinstance(record.get('data'), Mapping) else ()
            ) or ()
            if isinstance(item, Mapping) and str(item.get('url') or '').strip()
        }
        for guidance in self.guidance:
            urls.update(
                token.rstrip('.,;，。；')
                for token in guidance.split()
                if token.startswith(('http://', 'https://'))
            )
        return urls

    def read_urls(self) -> set[str]:
        return {
            str(page.get('url') or '').strip()
            for record in self._records
            if record.get('event') == 'web.read'
            for page in (
                (record.get('data') or {}).get('pages')
                if isinstance(record.get('data'), Mapping) else ()
            ) or ()
            if isinstance(page, Mapping) and str(page.get('url') or '').strip()
        }

    def journal_ref(self) -> dict[str, str]:
        path = self.artifact_root / 'journal.jsonl'
        if not path.is_file():
            path.write_text('', encoding='utf-8')
        return content_ref(path, self.artifact_root)

    def close(self) -> None:
        cleanup_workdir(self.work_root)

    def restore_source(self) -> None:
        metadata = _read_json(self.artifact_root / 'metadata.json')
        origin = source_root(metadata.get('source_dir'))
        shutil.rmtree(self.work_root / 'source', ignore_errors=True)
        copy_source(origin, self.work_root / 'source')
        if source_hash(self.work_root / 'source') != self.source_digest:
            raise ValueError('source_restore_failed')


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True, default=str) + '\n',
        encoding='utf-8',
    )


def content_ref(path: Path, artifact_root: Path) -> dict[str, str]:
    return {
        'uri': content_uri(path, artifact_root),
        'sha256': hashlib.sha256(path.read_bytes()).hexdigest(),
    }


def directory_ref(path: Path, artifact_root: Path) -> dict[str, str]:
    digest = hashlib.sha256()
    for item in sorted(candidate for candidate in path.rglob('*') if candidate.is_file()):
        digest.update(item.relative_to(path).as_posix().encode())
        digest.update(b'\0')
        digest.update(item.read_bytes())
    return {'uri': content_uri(path, artifact_root), 'sha256': digest.hexdigest()}


def content_uri(path: Path, artifact_root: Path) -> str:
    relative = path.relative_to(artifact_root).as_posix()
    identity = '/'.join(artifact_root.parts[-4:])
    return f'phase1://{identity}/{relative}'


def cleanup_workdir(work_root: Path | None) -> None:
    if work_root is not None and work_root.name.startswith('lazyrag-repair-phase1-'):
        shutil.rmtree(work_root, ignore_errors=True)


def _target_hash(target: Mapping[str, Any]) -> str:
    stable = {
        'category_id': target.get('category_id'),
        'source_hash': target.get('source_hash'),
        'category': target.get('category'),
    }
    return hashlib.sha256(
        json.dumps(stable, ensure_ascii=False, sort_keys=True, default=str).encode()
    ).hexdigest()[:24]


def _safe_segment(value: str) -> str:
    text = ''.join(char if char.isalnum() or char in '._-' else '_' for char in value.strip())
    text = text.strip('._-')
    if not text or text in {'.', '..'}:
        raise ValueError('unsafe_artifact_segment')
    return text[:160]


def _load_records(attempts: Sequence[Path]) -> list[dict[str, Any]]:
    records = []
    for attempt in attempts:
        for item in _load_journal(attempt):
            path = attempt / str(item.get('file') or '')
            value = _read_json(path)
            if value:
                records.append(value)
    return records[-80:]


def _load_journal(attempt: Path) -> list[dict[str, Any]]:
    path = attempt / 'journal.jsonl'
    if not path.is_file():
        return []
    result = []
    for line in path.read_text(encoding='utf-8').splitlines():
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            result.append(value)
    return result


def _read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding='utf-8'))
    except (OSError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def _compact_record(record: Mapping[str, Any]) -> dict[str, Any]:
    data = record.get('data') if isinstance(record.get('data'), Mapping) else {}
    event = str(record.get('event') or '')
    if event == 'opencode.result':
        report = data.get('report') if isinstance(data.get('report'), Mapping) else {}
        data = {
            'status': data.get('status'),
            'reason': data.get('reason'),
            'summary': report.get('summary'),
            'changed_files': data.get('changed_files'),
            'suggested_commands': report.get('suggested_commands'),
            'artifacts': data.get('artifacts'),
        }
    elif event == 'command.result':
        data = {
            key: data.get(key)
            for key in (
                'status', 'command', 'expected_result', 'exit_code', 'duration_ms',
                'changed_files', 'output', 'stdout_excerpt', 'stderr_excerpt', 'result_ref',
            )
        }
    elif event == 'web.search':
        data = {
            'query': data.get('query'),
            'status': data.get('status'),
            'results': list(data.get('results') or ())[:8],
            'failures': data.get('failures'),
        }
    elif event == 'web.read':
        data = {
            'question': data.get('question'),
            'pages': list(data.get('pages') or ())[:3],
        }
    elif event == 'http.result':
        data = {
            key: data.get(key)
            for key in (
                'status', 'url', 'method', 'status_code', 'body_excerpt',
                'error_type', 'duration_ms', 'result_ref',
            )
        }
    text = json.dumps(data, ensure_ascii=False, sort_keys=True, default=str)
    return {
        'event': event,
        'summary': record.get('summary'),
        'data': json.loads(text) if len(text) <= 7000 else {'excerpt': text[:7000] + '…'},
    }


def _recent_records(records: Sequence[Mapping[str, Any]], limit: int) -> list[dict[str, Any]]:
    selected: list[dict[str, Any]] = []
    used = 0
    for record in reversed(records):
        compact = _compact_record(record)
        size = len(json.dumps(compact, ensure_ascii=False, default=str))
        if selected and used + size > limit:
            break
        selected.append(compact)
        used += size
    return list(reversed(selected))


def _explicitly_requires(text: str, phrases: Sequence[str]) -> bool:
    if not any(phrase in text for phrase in phrases):
        return False
    return not any(
        marker in text
        for marker in ('不要联网', '禁止联网', '无需联网', '不需要联网', 'do not search the web')
    )
