"""Live Stage C smoke tests for eval_qc.

This file is intended to stay in the repository as a real-model smoke suite.
It checks that the eval_qc Stage C path can call the live chat service and
return the structured contract: non-empty ``summary_reason``, exactly the five
``EDGE_IDS``, numeric scores in ``[0, 1]``, and non-empty reasons. It does not
assert thresholds, pass/fail decisions, or scoring quality.

Its defaults are conservative so it remains CI-friendly:

- skipped unless ``EVO_RUN_LIVE_STAGE_C=1``
- calls ``EVO_CHAT_BASE_URL`` over HTTP, defaulting to ``http://chat:8046``
- sends ``EVO_CHAT_INTERNAL_TOKEN`` as ``X-Internal-Token`` for internal auth
- uses a pytest temporary base dir for run artifacts
- does not emit HTML unless explicitly requested via pytest CLI
- does not print model outputs unless ``EVO_STAGE_C_SMOKE_DEBUG=1``

Default non-live check:

  .venv/bin/python -m pytest -q tests/evo -k eval_qc

Recommended local live usage:

  EVO_CHAT_BASE_URL=http://localhost:8046 \\
  EVO_CHAT_INTERNAL_TOKEN=dev-internal-service-token \\
  EVO_RUN_LIVE_STAGE_C=1 \\
    .venv/bin/python -m pytest -q tests/evo/test_eval_qc_stage_c_smoke.py

Debug usage:

  EVO_CHAT_BASE_URL=http://localhost:8046 \\
  EVO_CHAT_INTERNAL_TOKEN=dev-internal-service-token \\
  EVO_RUN_LIVE_STAGE_C=1 \\
  EVO_STAGE_C_SMOKE_DEBUG=1 \\
    .venv/bin/python -m pytest -vv -s --log-cli-level=INFO \\
    tests/evo/test_eval_qc_stage_c_smoke.py

Optional HTML report:

  EVO_CHAT_BASE_URL=http://localhost:8046 \\
  EVO_CHAT_INTERNAL_TOKEN=dev-internal-service-token \\
  EVO_RUN_LIVE_STAGE_C=1 \\
    .venv/bin/python -m pytest -vv \\
    tests/evo/test_eval_qc_stage_c_smoke.py \\
    --html=report.html --self-contained-html
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any

import pytest

from evo.agents.eval_qc import run_eval_qc
from evo.domain import EDGE_IDS
from evo.main import default_llm_provider
from evo.runtime.config import load_config
from evo.runtime.session import AnalysisSession, create_session, session_scope

_FIXTURE_PATH = Path(__file__).parent / 'fixtures' / 'eval_qc_stage_c_smoke.json'
_RUN_LIVE = os.getenv('EVO_RUN_LIVE_STAGE_C') == '1'
_DEBUG = os.getenv('EVO_STAGE_C_SMOKE_DEBUG') == '1'

pytestmark = pytest.mark.skipif(
    not _RUN_LIVE,
    reason='Set EVO_RUN_LIVE_STAGE_C=1 to run live Stage C smoke tests.',
)

_log = logging.getLogger(__name__)


def _load_cases() -> list[dict[str, Any]]:
    return json.loads(_FIXTURE_PATH.read_text(encoding='utf-8'))


def _load_case(case_id: str) -> dict[str, Any]:
    for case in _load_cases():
        if case['case_id'] == case_id:
            return case
    raise KeyError(f'fixture case not found: {case_id}')


def _build_session(tmp_path: Path) -> AnalysisSession:
    cfg = load_config(base_dir=tmp_path / 'evo')
    return create_session(cfg, llm_provider=default_llm_provider(cfg))


def _summarize_payload(case: dict[str, Any]) -> str:
    return (
        f'query_chars={len(case["query"])} '
        f'gt_answer_chars={len(case["gt_answer"])} '
        f'gt_text_items={len(case["gt_text"])} '
        f'gt_text_chars={sum(len(str(item)) for item in case["gt_text"])} '
        f'key_points={len(case["key_points"])}'
    )


def _format_edges(parsed: dict[str, Any]) -> str:
    parts: list[str] = []
    for item in parsed.get('edges', []):
        edge_id = str(item.get('id', '')).strip()
        score = float(item.get('score', 0.0))
        reason = str(item.get('reason', '')).strip()
        parts.append(f'- {edge_id}: score={score:.3f} reason={reason}')
    return '\n'.join(parts) if parts else '<no edges>'


def _read_raw_outputs(raw_dir: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    for path in sorted(raw_dir.glob('eval_qc*.txt')):
        out[path.name] = path.read_text(encoding='utf-8').strip()
    return out


def _format_telemetry(session: AnalysisSession) -> str:
    if not session.telemetry.history:
        return '<empty>'
    return '\n'.join(
        f'- {event.type}: {json.dumps(event.payload, ensure_ascii=False, default=str)}'
        for event in session.telemetry.history
    )


def _find_llm_calls(session: AnalysisSession) -> list[dict[str, Any]]:
    return [
        event.payload for event in session.telemetry.history
        if event.type == 'llm_call'
    ]


def _build_diagnostics(
    case: dict[str, Any],
    session: AnalysisSession,
    parsed: dict[str, Any],
    raw_dir: Path,
) -> str:
    raw_outputs = _read_raw_outputs(raw_dir)
    raw_names = ', '.join(raw_outputs) if raw_outputs else '<none>'
    llm_calls = _find_llm_calls(session)
    llm_summary = json.dumps(llm_calls, ensure_ascii=False, default=str) if llm_calls else '[]'
    lines = [
        f'case_id={case["case_id"]}',
        f'notes={case.get("notes", "")}',
        f'run_id={session.run_id}',
        f'payload_stats={_summarize_payload(case)}',
        f'raw_dir={raw_dir}',
        f'raw_files={raw_names}',
        f'llm_calls={llm_summary}',
        f'summary_reason={parsed.get("summary_reason", "")}',
        'edges:',
        _format_edges(parsed),
    ]
    return '\n'.join(lines)


def _emit_debug_details(
    case: dict[str, Any],
    session: AnalysisSession,
    parsed: dict[str, Any],
    raw_dir: Path,
) -> None:
    if not _DEBUG:
        return
    _log.info('Stage C smoke diagnostics\n%s', _build_diagnostics(case, session, parsed, raw_dir))
    raw_outputs = _read_raw_outputs(raw_dir)
    if raw_outputs:
        for name, text in raw_outputs.items():
            _log.info('Raw output %s\n%s', name, text)
    _log.info('Telemetry\n%s', _format_telemetry(session))


def _assert_edge_contract(parsed: dict[str, Any], *, diagnostics: str) -> dict[str, dict[str, Any]]:
    assert isinstance(parsed, dict), diagnostics
    assert isinstance(parsed.get('summary_reason'), str), diagnostics
    assert parsed['summary_reason'].strip(), diagnostics

    edges = parsed.get('edges')
    assert isinstance(edges, list), diagnostics
    assert len(edges) == len(EDGE_IDS), diagnostics

    by_id: dict[str, dict[str, Any]] = {}
    for item in edges:
        assert isinstance(item, dict), diagnostics
        edge_id = str(item.get('id', '')).strip()
        assert edge_id in EDGE_IDS, diagnostics
        assert edge_id not in by_id, diagnostics
        score = item.get('score')
        assert isinstance(score, (int, float)), diagnostics
        assert 0.0 <= float(score) <= 1.0, diagnostics
        reason = str(item.get('reason', '')).strip()
        assert reason, diagnostics
        if edge_id == 'gt_text_to_gt_answer' and 'claims' in item:
            claims = item['claims']
            assert isinstance(claims, list) and claims, diagnostics
            for claim in claims:
                assert isinstance(claim, dict), diagnostics
                assert isinstance(claim.get('text'), str) and claim['text'].strip(), diagnostics
                assert isinstance(claim.get('supported'), bool), diagnostics
                assert isinstance(claim.get('evidence'), str) and claim['evidence'].strip(), diagnostics
        by_id[edge_id] = item

    assert set(by_id) == set(EDGE_IDS), diagnostics
    return by_id


def _run_and_assert_contract(case: dict[str, Any], tmp_path: Path) -> None:
    session = _build_session(tmp_path)
    raw_dir = session.config.storage.runs_dir / session.run_id / 'raw'
    payload = {
        'query': case['query'],
        'gt_answer': case['gt_answer'],
        'gt_text': case['gt_text'],
        'key_points': case['key_points'],
    }

    with session_scope(session):
        parsed = run_eval_qc(session, payload)

    _emit_debug_details(case, session, parsed, raw_dir)
    diagnostics = _build_diagnostics(case, session, parsed, raw_dir)
    _assert_edge_contract(parsed, diagnostics=diagnostics)


def test_stage_c_live_returns_valid_contract(tmp_path: Path) -> None:
    _run_and_assert_contract(_load_case('stage_c_contract_minimal_001'), tmp_path)


def test_stage_c_live_handles_marginal_input(tmp_path: Path) -> None:
    _run_and_assert_contract(_load_case('stage_c_contract_marginal_001'), tmp_path)


def main() -> int:
    args = [__file__, '-vv']
    if _DEBUG:
        args.extend(['-s', '--log-cli-level=INFO'])
    return pytest.main(args)


if __name__ == '__main__':
    raise SystemExit(main())
