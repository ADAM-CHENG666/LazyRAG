from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType


def _load_prompt_stability() -> ModuleType:
    root = Path(__file__).resolve().parent.parent.parent
    path = root / 'lazyrag-workbench' / 'scripts' / 'eval_qc' / 'eval_qc_prompt_stability.py'
    spec = importlib.util.spec_from_file_location('eval_qc_prompt_stability', path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_prompt_stability_invalid_claim_score_is_isolated_from_scores() -> None:
    mod = _load_prompt_stability()
    edge_ids = ('gt_text_to_gt_answer',)
    results = [
        {
            'repeat_index': 0,
            'case_index': 0,
            'case_id': 'case_bad_claim_score',
            'dataset_id': 'case_bad_claim_score',
            'expected_edges': {'gt_text_to_gt_answer': False},
            'ok': False,
            'error': (
                'Invalid eval_qc output: '
                'edges[0].claims[1].score must be a finite number in [0,1]'
            ),
            'llm_output': {
                'summary_reason': 'bad claim score',
                'edges': [
                    {
                        'id': 'gt_text_to_gt_answer',
                        'score': 0.95,
                        'reason': 'model score typo',
                        'claims': [
                            {'text': 'supported', 'score': 0.95},
                            {'text': 'typo', 'score': 'not_a_score'},
                        ],
                    },
                ],
            },
        },
        {
            'repeat_index': 0,
            'case_index': 1,
            'case_id': 'case_good_claim',
            'dataset_id': 'case_good_claim',
            'expected_edges': {'gt_text_to_gt_answer': True},
            'ok': True,
            'error': None,
            'llm_output': {
                'summary_reason': 'good claim scores',
                'edges': [
                    {
                        'id': 'gt_text_to_gt_answer',
                        'score': 0.10,
                        'reason': 'score should be recomputed from claims scores',
                        'claims': [
                            {'text': 'supported', 'score': 0.95},
                        ],
                    },
                ],
            },
        },
    ]

    summary = mod.build_summary(results, edge_ids)

    edge = summary['edges']['gt_text_to_gt_answer']
    assert edge['invalid_claim_score_count'] == 1
    assert edge['missing_scores'] == 1
    assert edge['fail_count'] == 0
    assert edge['pass_count'] == 1
    assert edge['pass_mean'] == 0.95
    assert edge['fail_mean'] is None

    observation = edge['observations'][0]
    assert observation['score'] is None
    assert observation['invalid_claim_score'] is True
    assert observation['claim_validation_errors'] == [
        'claims[1].score must be a finite number in [0,1]',
    ]

    output_errors = mod._validate_eval_qc_output(results[0]['llm_output'], edge_ids)
    assert 'edges[0].claims[1].score must be a finite number in [0,1]' in output_errors
