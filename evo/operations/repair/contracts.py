from __future__ import annotations

import math
from collections.abc import Mapping
from pathlib import PurePosixPath
from typing import Annotated, Any, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator


class ExperimentInput(BaseModel):
    model_config = ConfigDict(extra='forbid')

    name: str = Field(
        min_length=1, max_length=160, pattern=r'^[A-Za-z0-9][A-Za-z0-9._-]*$',
        description='Stable name of one complete independently runnable Demo scenario.',
    )
    payload: dict[str, Any] = Field(
        description='Complete JSON object for one Demo run; never one parameter to combine with other inputs.',
    )


class CodeSpan(BaseModel):
    model_config = ConfigDict(extra='forbid')

    path: str = Field(min_length=1, max_length=500)
    symbol: str = Field(min_length=1, max_length=500)


class RepairStep(BaseModel):
    model_config = ConfigDict(extra='forbid')

    kind: Literal['change', 'context']
    action: str = Field(min_length=1, max_length=700)
    requires: list[Annotated[str, Field(min_length=1, max_length=400)]] = Field(
        default_factory=list, max_length=8,
    )
    produces: list[Annotated[str, Field(min_length=1, max_length=400)]] = Field(
        default_factory=list, max_length=8,
    )

    @model_validator(mode='after')
    def reject_workspace_paths(self) -> Self:
        if 'source/' in self.action:
            raise ValueError('repair step paths must be repository-relative, without workspace source/ prefix')
        return self


class AgentTurn(BaseModel):
    model_config = ConfigDict(extra='forbid')

    action: Literal['search_web', 'read_web', 'investigate', 'experiment', 'revise_demo', 'stop']
    reason: str = Field(min_length=1, max_length=1400)
    question: str = Field(default='', max_length=2000)
    urls: list[str] = Field(default_factory=list, max_length=3)
    instruction: str = Field(default='', max_length=4000)
    repair_method: str = Field(default='', max_length=2400)
    success_metric: str = Field(default='', max_length=100)
    repair_steps: list[RepairStep] = Field(default_factory=list, max_length=12)
    demo_method: str = Field(default='', max_length=1600)
    inputs: list[ExperimentInput] = Field(default_factory=list, max_length=8)
    must_observe: list[str] = Field(default_factory=list, max_length=20)
    must_not_observe: list[str] = Field(default_factory=list, max_length=20)
    code_scope: list[CodeSpan] = Field(default_factory=list, max_length=20)
    repair_requirements: list[str] = Field(default_factory=list, max_length=20)
    demo_requirements: list[str] = Field(default_factory=list, max_length=20)
    live_urls: list[str] = Field(default_factory=list, max_length=4)
    stop_status: Literal['blocked', 'exhausted', 'failed'] = 'blocked'

    @model_validator(mode='after')
    def validate_action(self) -> Self:
        if self.action in {'search_web', 'read_web'} and not self.question.strip():
            raise ValueError(f'{self.action} requires question')
        if self.action == 'read_web' and not self.urls:
            raise ValueError('read_web requires urls')
        if self.action in {'investigate', 'revise_demo'} and not self.instruction.strip():
            raise ValueError(f'{self.action} requires instruction')
        if self.action == 'experiment':
            if not self.repair_method.strip() or not self.repair_steps or not self.demo_method.strip():
                raise ValueError('experiment requires repair_method, ordered repair_steps and demo_method')
            if not self.success_metric.strip():
                raise ValueError('experiment requires success_metric')
            if not self.inputs or not self.must_observe:
                raise ValueError('experiment requires inputs and must_observe')
            if not self.code_scope or not self.repair_requirements or not self.demo_requirements:
                raise ValueError(
                    'experiment requires code_scope, repair_requirements and demo_requirements'
                )
        return self


class AgentAssessment(BaseModel):
    model_config = ConfigDict(extra='forbid')

    verdict: Literal['supports', 'rejects', 'inconclusive']
    matched: list[str] = Field(default_factory=list)
    unmet: list[str] = Field(default_factory=list)
    unexpected: list[str] = Field(default_factory=list)
    reason: str = Field(min_length=1, max_length=4000)


class CausalCheck(BaseModel):
    model_config = ConfigDict(extra='forbid')

    role: Literal['new_dependency', 'removed_baseline']
    predicate: str = Field(min_length=1, max_length=500)
    producer: str = Field(min_length=1, max_length=700)
    producer_origin: Literal['external_precondition', 'repair_step', 'unknown']
    producer_step: int | None = Field(default=None, ge=1, le=12)
    consumer: str = Field(min_length=1, max_length=700)
    consumer_step: int | None = Field(default=None, ge=1, le=12)

    @model_validator(mode='after')
    def validate_producer(self) -> Self:
        if (self.producer_origin == 'repair_step') != (self.producer_step is not None):
            raise ValueError('producer_step is required only when producer_origin is repair_step')
        return self


class GuidanceCheck(BaseModel):
    model_config = ConfigDict(extra='forbid')

    guidance_index: int = Field(ge=1, le=20)
    guidance: str = Field(min_length=1, max_length=4000)
    satisfied: bool
    contradicted: bool
    evidence: str = Field(min_length=1, max_length=1000)

    @model_validator(mode='after')
    def reject_contradictory_verdict(self) -> Self:
        if self.satisfied and self.contradicted:
            raise ValueError('guidance cannot be both satisfied and contradicted')
        return self


class ExperimentReview(BaseModel):
    model_config = ConfigDict(extra='forbid')

    causal_checks: list[CausalCheck] = Field(min_length=1, max_length=8)
    demo_distinguishes_method: bool
    success_metric_matches_root_cause: bool
    repair_steps_unambiguous: bool
    repair_steps_minimal: bool
    guidance_checks: list[GuidanceCheck] = Field(default_factory=list, max_length=20)
    issues: list[Annotated[str, Field(min_length=1, max_length=500)]] = Field(
        default_factory=list, max_length=6,
    )
    reason: str = Field(min_length=1, max_length=1200)

    def causal_order_valid(self, step_count: int | None = None) -> bool:
        return all(
            item.role == 'removed_baseline' or (
                item.producer_origin != 'unknown'
                and (
                    item.producer_origin == 'external_precondition'
                    or item.consumer_step is None
                    or (
                        item.producer_step is not None
                        and item.producer_step <= item.consumer_step
                    )
                )
                and (step_count is None or (
                    (item.producer_step is None or item.producer_step <= step_count)
                    and (item.consumer_step is None or item.consumer_step <= step_count)
                ))
            )
            for item in self.causal_checks
        )

    def guidance_valid(self, guidance: list[str]) -> bool:
        expected = [str(item).strip() for item in guidance if str(item).strip()]
        if len(self.guidance_checks) != len(expected):
            return False
        checks = {item.guidance_index: item for item in self.guidance_checks}
        return len(checks) == len(expected) and all(
            (item := checks.get(index)) is not None
            and item.guidance == requirement
            and item.satisfied
            and not item.contradicted
            for index, requirement in enumerate(expected, 1)
        )

    @model_validator(mode='after')
    def validate_verdict(self) -> Self:
        accepted = all((
            self.causal_order_valid(), self.demo_distinguishes_method,
            self.success_metric_matches_root_cause,
            self.repair_steps_unambiguous, self.repair_steps_minimal,
        ))
        if not accepted and not self.issues:
            raise ValueError('a failed review check requires concrete issues')
        if accepted and all(item.satisfied and not item.contradicted for item in self.guidance_checks) and self.issues:
            raise ValueError('an accepted review must not contain placeholder issues')
        return self


class StopReview(BaseModel):
    model_config = ConfigDict(extra='forbid')

    requirements_resolved: bool
    in_scope_alternative_exists: bool
    terminal_justified: bool
    issues: list[Annotated[str, Field(min_length=1, max_length=500)]] = Field(
        default_factory=list, max_length=6,
    )
    reason: str = Field(min_length=1, max_length=1200)

    @model_validator(mode='after')
    def validate_verdict(self) -> Self:
        accepted = (
            self.requirements_resolved
            and not self.in_scope_alternative_exists
            and self.terminal_justified
        )
        if not accepted and not self.issues:
            raise ValueError('a rejected stop decision requires concrete issues')
        return self


class PatchReview(BaseModel):
    model_config = ConfigDict(extra='forbid')

    matches_verified_method: bool
    preserves_contracts_and_data_scope: bool
    minimal: bool
    issues: list[Annotated[str, Field(min_length=1, max_length=500)]] = Field(
        default_factory=list, max_length=6,
    )
    reason: str = Field(min_length=1, max_length=1200)

    @property
    def accepted(self) -> bool:
        return self.matches_verified_method and self.preserves_contracts_and_data_scope and self.minimal

    @model_validator(mode='after')
    def validate_verdict(self) -> Self:
        if self.accepted and self.issues:
            raise ValueError('an accepted patch review must not contain issues')
        if not self.accepted and not self.issues:
            raise ValueError('a rejected patch review requires concrete issues')
        return self


def validate_analysis(value: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(value, Mapping) or set(value) != {'source_hash', 'all_case_metric_averages', 'categories'}:
        raise ValueError('phase1_input_fields_invalid')
    source_hash = str(value.get('source_hash') or '')
    if len(source_hash) != 64 or any(char not in '0123456789abcdef' for char in source_hash):
        raise ValueError('source_hash_invalid')
    all_metrics = _metric_map(value.get('all_case_metric_averages'))
    raw_categories = value.get('categories')
    if not isinstance(raw_categories, Mapping) or not raw_categories:
        raise ValueError('categories_empty')
    categories = {}
    seen_cases = set()
    for raw_id, raw_category in raw_categories.items():
        category_id = str(raw_id or '').strip()
        if not category_id or category_id != raw_id or not isinstance(raw_category, Mapping):
            raise ValueError('category_invalid')
        expected = {'metric_averages', 'all_case_average_drop', 'code_span', 'analysis', 'cases'}
        if set(raw_category) != expected:
            raise ValueError('category_fields_invalid')
        metrics = _metric_map(raw_category.get('metric_averages'))
        if set(metrics) != set(all_metrics):
            raise ValueError('category_metric_keys_mismatch')
        drop = raw_category.get('all_case_average_drop')
        if (isinstance(drop, bool) or not isinstance(drop, (int, float))
                or not math.isfinite(drop) or not 0 <= drop <= 1):
            raise ValueError('category_drop_invalid')
        spans = [_code_span(item) for item in raw_category.get('code_span') or ()]
        analysis = str(raw_category.get('analysis') or '').strip()
        cases = raw_category.get('cases')
        if not spans or not analysis or not isinstance(cases, Mapping) or not cases:
            raise ValueError('category_root_cause_incomplete')
        normalized_cases = {}
        for raw_case_id, raw_trace_id in cases.items():
            case_id, trace_id = str(raw_case_id or '').strip(), str(raw_trace_id or '').strip()
            if not case_id or not trace_id or case_id in seen_cases:
                raise ValueError('category_cases_invalid')
            seen_cases.add(case_id)
            normalized_cases[case_id] = trace_id
        categories[category_id] = {
            'metric_averages': metrics,
            'all_case_average_drop': float(drop),
            'code_span': spans,
            'analysis': analysis,
            'cases': normalized_cases,
        }
    return {'source_hash': source_hash, 'all_case_metric_averages': all_metrics, 'categories': categories}


def select_category(categories: Mapping[str, Mapping[str, Any]]) -> tuple[str, Mapping[str, Any]]:
    category_id = min(categories, key=lambda item: (-float(categories[item]['all_case_average_drop']), item))
    return category_id, categories[category_id]


def build_supported_plan(category_id: str, phase1: Mapping[str, Any]) -> dict[str, Any]:
    method = phase1.get('method')
    validation = phase1.get('demo_validation')
    if not isinstance(method, Mapping) or set(method) != {
        'summary', 'success_metric', 'steps', 'code_scope', 'requirements',
    }:
        raise ValueError('method_invalid')
    summary = str(method.get('summary') or '').strip()
    success_metric = str(method.get('success_metric') or '').strip()
    steps = [RepairStep.model_validate(item).model_dump() for item in method.get('steps') or ()]
    scope = [_code_span(item) for item in method.get('code_scope') or ()]
    requirements = [str(item).strip() for item in method.get('requirements') or () if str(item).strip()]
    if not summary or not success_metric or not steps or not scope or not requirements:
        raise ValueError('method_incomplete')
    if not isinstance(validation, Mapping) or set(validation) != {
        'verdict', 'reason', 'spec_ref', 'demo_ref', 'result_ref', 'journal_ref',
    }:
        raise ValueError('demo_validation_invalid')
    if validation.get('verdict') != 'supports' or not str(validation.get('reason') or '').strip():
        raise ValueError('demo_not_supported')
    for name in ('spec_ref', 'demo_ref', 'result_ref', 'journal_ref'):
        _validate_content_ref(validation[name])
    return {
        'id': 'repair.plan',
        'status': 'planned',
        'category_id': category_id,
        'method': {
            'summary': summary,
            'success_metric': success_metric,
            'steps': steps,
            'code_scope': scope,
            'requirements': requirements,
        },
        'demo_validation': dict(validation),
    }


def _metric_map(value: object) -> dict[str, float]:
    if not isinstance(value, Mapping) or not value:
        raise ValueError('metrics_invalid')
    result = {}
    for raw_name, raw_score in value.items():
        name = str(raw_name or '').strip()
        if (not name or name != raw_name or isinstance(raw_score, bool)
                or not isinstance(raw_score, (int, float)) or not math.isfinite(raw_score)
                or not 0 <= raw_score <= 1):
            raise ValueError('metric_invalid')
        result[name] = float(raw_score)
    return result


def _code_span(value: object) -> dict[str, str]:
    if not isinstance(value, Mapping) or set(value) != {'path', 'symbol'}:
        raise ValueError('code_span_invalid')
    path, symbol = str(value.get('path') or '').strip(), str(value.get('symbol') or '').strip()
    parts = PurePosixPath(path).parts
    if not path or not symbol or path.startswith('/') or '\\' in path or any(part in {'', '.', '..'} for part in parts):
        raise ValueError('code_span_invalid')
    return {'path': path, 'symbol': symbol}


def _validate_content_ref(value: object) -> None:
    if not isinstance(value, Mapping) or set(value) != {'uri', 'sha256'}:
        raise ValueError('content_ref_invalid')
    uri, digest = str(value.get('uri') or '').strip(), str(value.get('sha256') or '')
    if not uri or len(digest) != 64 or any(char not in '0123456789abcdef' for char in digest):
        raise ValueError('content_ref_invalid')
