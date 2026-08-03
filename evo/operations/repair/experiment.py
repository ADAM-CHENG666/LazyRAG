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


def create_experiment(run_id: str, category_id: str, input_hash: str, source_dir: Path,
                      expected_source_hash: str, policy: Mapping[str, Any]) -> tuple[Path, Path]:
    source = source_root(source_dir)
    if not (source / ALGORITHM_APP).is_file():
        raise ValueError('candidate_source_invalid')
    if source_hash(source) != expected_source_hash:
        raise ValueError('source_hash_mismatch')
    work_root = Path(tempfile.mkdtemp(prefix='lazyrag-repair-phase1-', dir='/tmp')).resolve()
    artifact_root: Path | None = None
    try:
        artifact_base = Path(
            str(policy.get('phase1_artifact_dir') or '')
            or Path(os.getenv('LAZYMIND_EVO_BASE_DIR') or '/var/lib/lazymind/evo') / 'artifacts' / 'repair' / 'phase1'
        ).resolve()
        artifact_parent = artifact_base / _safe_segment(run_id) / _safe_segment(category_id) / _safe_segment(input_hash)
        artifact_parent.mkdir(parents=True, exist_ok=True)
        artifact_root = Path(tempfile.mkdtemp(prefix='attempt-', dir=artifact_parent)).resolve()
        for name in ('demo', 'inputs', 'outputs', 'web', 'logs/runner', 'opencode/reports', 'opencode/calls'):
            (work_root / name).mkdir(parents=True, exist_ok=True)
            (artifact_root / name).mkdir(parents=True, exist_ok=True)
        copy_source(source, work_root / 'source')
        metadata = {'source_dir': str(source), 'source_hash': expected_source_hash, 'input_hash': input_hash}
        write_json(work_root / 'logs/experiment.json', metadata)
        write_json(artifact_root / 'logs/experiment.json', metadata)
        return work_root, artifact_root
    except Exception:
        cleanup_experiment_workdir(work_root)
        if artifact_root is not None:
            shutil.rmtree(artifact_root, ignore_errors=True)
        raise


def save_experiment_spec(artifact_root: Path, spec: Mapping[str, Any]) -> dict[str, str]:
    path = artifact_root / 'spec.json'
    write_json(path, dict(spec))
    return content_ref(path, artifact_root)


def materialize_inputs(work_root: Path, artifact_root: Path,
                       inputs: Sequence[Mapping[str, Any]]) -> tuple[dict[str, Any], ...]:
    records: list[dict[str, Any]] = []
    seen: set[str] = set()
    for raw in inputs:
        raw_name = str(raw.get('name') or '').strip()
        if not raw_name:
            raise ValueError('experiment_input_name_invalid')
        name = _safe_segment(raw_name)
        if name in seen:
            raise ValueError('experiment_input_name_invalid')
        if 'payload' not in raw:
            raise ValueError(f'experiment_input_payload_missing:{name}')
        seen.add(name)
        work_path = work_root / 'inputs' / f'{name}.json'
        artifact_path = artifact_root / 'inputs' / f'{name}.json'
        write_json(work_path, raw['payload'])
        shutil.copy2(work_path, artifact_path)
        records.append({'name': name, 'path': str(work_path), 'ref': content_ref(artifact_path, artifact_root)})
    if not records:
        raise ValueError('experiment_inputs_empty')
    return tuple(records)


def append_journal(artifact_root: Path, event: str, data: Mapping[str, Any]) -> dict[str, str]:
    path = artifact_root / 'logs' / 'journal.jsonl'
    record = {'time': datetime.now(timezone.utc).isoformat(), 'event': event, **dict(data)}
    with path.open('a', encoding='utf-8') as stream:
        stream.write(json.dumps(record, ensure_ascii=False, sort_keys=True, default=str) + '\n')
    return content_ref(path, artifact_root)


def cleanup_experiment_workdir(work_root: Path | None) -> None:
    if work_root is not None and work_root.name.startswith('lazyrag-repair-phase1-'):
        shutil.rmtree(work_root, ignore_errors=True)


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True, default=str) + '\n',
                    encoding='utf-8')


def content_ref(path: Path, artifact_root: Path) -> dict[str, str]:
    return {'uri': content_uri(path, artifact_root), 'sha256': hashlib.sha256(path.read_bytes()).hexdigest()}


def content_uri(path: Path, artifact_root: Path) -> str:
    relative = path.relative_to(artifact_root).as_posix()
    identity = '/'.join(artifact_root.parts[-4:])
    return f'phase1://{identity}/{relative}'


def _safe_segment(value: str) -> str:
    text = ''.join(char if char.isalnum() or char in '._-' else '_' for char in value.strip())
    text = text.strip('._-')
    if not text or text in {'.', '..'}:
        raise ValueError('unsafe_artifact_segment')
    return text[:160]
