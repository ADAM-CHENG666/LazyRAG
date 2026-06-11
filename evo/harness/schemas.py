from __future__ import annotations

from evo.domain import LOGIC_IDS

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
# eval_qc stage 1 (extract): decompose the query into unit/core/qualifier.
EVAL_QC_EXTRACT: dict = {
    'type': 'object',
    'required': ['query'],
    'properties': {
        'query': {
            'type': 'object',
            'required': ['core', 'qualifier'],
            'properties': {
                'unit': {'type': 'string'},
                'core': {'type': 'string', 'minLength': 1},
                'qualifier': {'type': 'string'},
            },
            'additionalProperties': False,
        },
    },
    'additionalProperties': False,
}
# eval_qc stage 2 (judge): one flat judgment per logic_id, each falling into one bucket.
EVAL_QC_JUDGE: dict = {
    'type': 'object',
    'required': ['judgments', 'summary_reason'],
    'additionalProperties': False,
    'properties': {
        'judgments': {
            'type': 'array',
            'minItems': len(LOGIC_IDS),
            'maxItems': len(LOGIC_IDS),
            'items': {
                'type': 'object',
                'required': ['logic_id', 'reason', 'level'],
                'additionalProperties': False,
                'properties': {
                    'logic_id': {'type': 'string', 'enum': list(LOGIC_IDS)},
                    'reason': {'type': 'string', 'minLength': 1},
                    'level': {'enum': [0.1, 0.3, 0.6, 0.9]},
                },
            },
            'allOf': [
                {
                    'contains': {
                        'type': 'object',
                        'required': ['logic_id'],
                        'properties': {'logic_id': {'const': logic_id}},
                    },
                    'minContains': 1,
                    'maxContains': 1,
                }
                for logic_id in LOGIC_IDS
            ],
        },
        'summary_reason': {'type': 'string'},
    },
}
SCHEMAS: dict[str, dict] = {
    'indexer': INDEXER,
    'researcher': RESEARCHER,
    'critic': CRITIC,
    'synthesizer': SYNTHESIZER,
    'conductor': CONDUCTOR,
    'action_verifier': ACTION_VERIFIER,
    'eval_qc_extract': EVAL_QC_EXTRACT,
    'eval_qc_judge': EVAL_QC_JUDGE,
}
__all__ = ['SCHEMAS', 'INDEXER', 'RESEARCHER', 'CRITIC', 'SYNTHESIZER', 'CONDUCTOR', 'ACTION_VERIFIER',
           'EVAL_QC_EXTRACT', 'EVAL_QC_JUDGE']
