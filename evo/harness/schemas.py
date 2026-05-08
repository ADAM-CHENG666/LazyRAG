from __future__ import annotations

from evo.domain import EDGE_IDS

_CLAIMS_EDGE_ID = 'gt_text_to_gt_answer'
SCORE_EDGE_IDS = tuple(edge_id for edge_id in EDGE_IDS if edge_id != _CLAIMS_EDGE_ID)

INDEXER: dict = {
    'type': 'object',
    'required': ['hypotheses'],
    'properties': {
        'hypotheses': {'type': 'array'},
        'cross_step_narrative': {'type': 'string'},
        'open_questions': {'type': 'array'},
    },
}
RESEARCHER: dict = {
    'type': 'object',
    'required': ['verdict', 'refined_claim'],
    'properties': {
        'hypothesis_id': {'type': 'string'},
        'verdict': {'type': 'string', 'enum': ['confirmed', 'refuted', 'inconclusive']},
        'confidence': {'type': 'number'},
        'refined_claim': {'type': 'string', 'minLength': 1},
        'evidence_handles': {'type': 'array', 'items': {'type': 'string'}},
        'suggested_action': {'type': 'string'},
        'reasoning': {'type': 'string'},
    },
}
CRITIC: dict = {
    'type': 'object',
    'required': ['verdict'],
    'properties': {
        'verdict': {'type': 'string', 'minLength': 1},
        'approved_confidence': {'type': ['number', 'null']},
        'challenges': {'type': 'array'},
    },
}
_ACTION_ITEM: dict = {
    'type': 'object',
    'required': ['id', 'title', 'suggested_changes', 'code_map_target', 'priority'],
    'properties': {
        'id': {'type': 'string', 'minLength': 1},
        'finding_id': {'type': 'string'},
        'hypothesis_id': {'type': 'string'},
        'hypothesis_category': {'type': 'string'},
        'title': {'type': 'string', 'minLength': 1},
        'rationale': {'type': 'string'},
        'suggested_changes': {'type': 'string', 'minLength': 1},
        'code_map_target': {'type': 'string', 'minLength': 1},
        'target_step': {'type': 'string'},
        'target_line': {'type': 'integer'},
        'priority': {'type': 'string', 'enum': ['P0', 'P1', 'P2']},
        'expected_impact_metric': {'type': 'string'},
        'expected_direction': {'type': 'string', 'enum': ['+', '-']},
        'confidence': {'type': 'number'},
        'evidence_handles': {'type': 'array', 'items': {'type': 'string'}},
    },
}
_GAP_HYPOTHESIS_ITEM: dict = {
    'type': 'object',
    'required': ['id', 'claim', 'category'],
    'properties': {
        'id': {'type': 'string', 'minLength': 1},
        'claim': {'type': 'string', 'minLength': 1},
        'category': {
            'type': 'string',
            'enum': [
                'retrieval_miss',
                'rerank_failure',
                'generation_drift',
                'score_anomaly',
                'score_scale_mismatch',
                'code_issue',
            ],
        },
        'investigation_paths': {'type': 'array', 'items': {'type': 'string'}},
    },
}
SYNTHESIZER: dict = {
    'type': 'object',
    'required': ['summary', 'actions'],
    'properties': {
        'summary': {'type': 'string', 'minLength': 1},
        'guidance': {'type': 'string'},
        'actions': {'type': 'array', 'items': _ACTION_ITEM},
        'open_gaps': {'type': 'array', 'items': {'type': 'string'}},
        'gap_hypotheses': {'type': 'array', 'items': _GAP_HYPOTHESIS_ITEM},
    },
}
CONDUCTOR: dict = {
    'type': 'object',
    'required': ['actions', 'done'],
    'properties': {'actions': {'type': 'array'}, 'done': {'type': 'boolean'}, 'rationale': {'type': 'string'}},
}
ACTION_VERIFIER: dict = {
    'type': 'object',
    'required': ['validity_score'],
    'properties': {
        'validity_score': {'type': 'number'},
        'supporting_evidence': {'type': 'array'},
        'contradicting_evidence': {'type': 'array'},
        'notes': {'type': 'array'},
    },
}
EVAL_QC: dict = {
    'type': 'object',
    'required': ['edges', 'summary_reason'],
    'properties': {
        'edges': {
            'type': 'array',
            'minItems': len(EDGE_IDS),
            'maxItems': len(EDGE_IDS),
            'items': {
                'oneOf': [
                    {
                        'type': 'object',
                        'required': ['id', 'score', 'reason'],
                        'properties': {
                            'id': {
                                'type': 'string',
                                'enum': [edge_id for edge_id in EDGE_IDS if edge_id != _CLAIMS_EDGE_ID],
                            },
                            'score': {'type': 'number', 'minimum': 0, 'maximum': 1},
                            'reason': {'type': 'string', 'minLength': 1},
                        },
                        'additionalProperties': False,
                    },
                    {
                        'type': 'object',
                        'required': ['id', 'claims', 'reason'],
                        'properties': {
                            'id': {'const': _CLAIMS_EDGE_ID},
                            'claims': {
                                'type': 'array',
                                'minItems': 1,
                                'items': {
                                    'type': 'object',
                                    'required': ['text', 'score'],
                                    'properties': {
                                        'id': {'type': 'string', 'minLength': 1},
                                        'text': {'type': 'string', 'minLength': 1},
                                        'score': {'type': 'number', 'minimum': 0, 'maximum': 1},
                                        'evidence': {'type': 'string'},
                                    },
                                    'additionalProperties': False,
                                },
                            },
                            'reason': {'type': 'string', 'minLength': 1},
                        },
                        'additionalProperties': False,
                    },
                ],
            },
            'allOf': [
                {
                    'contains': {
                        'type': 'object',
                        'required': ['id'],
                        'properties': {'id': {'const': edge_id}},
                    },
                    'minContains': 1,
                    'maxContains': 1,
                }
                for edge_id in EDGE_IDS
            ],
        },
        'summary_reason': {'type': 'string'},
    },
    'additionalProperties': False,
}
EVAL_QC_SPLIT: dict = {
    'type': 'object',
    'required': ['claims'],
    'properties': {
        'claims': {
            'type': 'array',
            'minItems': 1,
            'items': {
                'type': 'object',
                'required': ['id', 'text'],
                'properties': {
                    'id': {'type': 'string', 'minLength': 1},
                    'text': {'type': 'string', 'minLength': 1},
                },
                'additionalProperties': False,
            },
        },
    },
    'additionalProperties': False,
}
EVAL_QC_WITH_CLAIMS: dict = {
    'type': 'object',
    'required': ['edges', 'claims_judgment', 'summary_reason'],
    'properties': {
        'edges': {
            'type': 'array',
            'minItems': len(SCORE_EDGE_IDS),
            'maxItems': len(SCORE_EDGE_IDS),
            'items': {
                'type': 'object',
                'required': ['id', 'score', 'reason'],
                'properties': {
                    'id': {'type': 'string', 'enum': list(SCORE_EDGE_IDS)},
                    'score': {'type': 'number', 'minimum': 0, 'maximum': 1},
                    'reason': {'type': 'string', 'minLength': 1},
                },
                'additionalProperties': False,
            },
            'allOf': [
                {
                    'contains': {
                        'type': 'object',
                        'required': ['id'],
                        'properties': {'id': {'const': edge_id}},
                    },
                    'minContains': 1,
                    'maxContains': 1,
                }
                for edge_id in SCORE_EDGE_IDS
            ],
        },
        'claims_judgment': {
            'type': 'array',
            'minItems': 1,
            'items': {
                'type': 'object',
                'required': ['id', 'text', 'score', 'evidence'],
                'properties': {
                    'id': {'type': 'string', 'minLength': 1},
                    'text': {'type': 'string', 'minLength': 1},
                    'score': {'type': 'number', 'minimum': 0, 'maximum': 1},
                    'evidence': {'type': 'string'},
                },
                'additionalProperties': False,
            },
        },
        'summary_reason': {'type': 'string'},
    },
    'additionalProperties': False,
}
SCHEMAS: dict[str, dict] = {
    'indexer': INDEXER,
    'researcher': RESEARCHER,
    'critic': CRITIC,
    'synthesizer': SYNTHESIZER,
    'conductor': CONDUCTOR,
    'action_verifier': ACTION_VERIFIER,
    'eval_qc': EVAL_QC,
    'eval_qc_split': EVAL_QC_SPLIT,
    'eval_qc_with_claims': EVAL_QC_WITH_CLAIMS,
}
__all__ = [
    'SCHEMAS',
    'INDEXER',
    'RESEARCHER',
    'CRITIC',
    'SYNTHESIZER',
    'CONDUCTOR',
    'ACTION_VERIFIER',
    'EVAL_QC',
    'EVAL_QC_SPLIT',
    'EVAL_QC_WITH_CLAIMS',
]
