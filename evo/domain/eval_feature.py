from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class EdgeSpec:
    """A directed edge in the eval-set quality-check graph.

    The judging convention is always: anchor is the reference, target is the item
    being judged against that reference.
    """

    id: str
    anchor: str
    target: str
    definition: str


EDGE_SPECS: tuple[EdgeSpec, ...] = (
    EdgeSpec(
        id='query_to_gt_answer',
        anchor='query',
        target='gt_answer',
        definition='以 query 为锚，判断 gt_answer 是否直接回答问题并覆盖 query 的核心意图。',
    ),
    EdgeSpec(
        id='query_to_gt_text',
        anchor='query',
        target='gt_text',
        definition='以 query 为锚，判断 gt_text 是否提供回答 query 所需的关键依据或相关信息。',
    ),
    EdgeSpec(
        id='query_to_key_points',
        anchor='query',
        target='key_points',
        definition='以 query 为锚，判断 key_points 是否覆盖回答 query 应包含的核心要点。',
    ),
    EdgeSpec(
        id='gt_text_to_gt_answer',
        anchor='gt_text',
        target='gt_answer',
        definition='以 gt_text 为锚，判断 gt_answer 的每个主要结论是否都能被 gt_text 明确支持或直接概括。不得根据常识、背景知识或主观推测补全 gt_text 中没有的信息。',
    ),
    EdgeSpec(
        id='gt_answer_to_key_points',
        anchor='gt_answer',
        target='key_points',
        definition='以 gt_answer 为锚，判断 key_points 是否提炼了答案中的核心信息，而不是遗漏主要结论。',
    ),
)

EDGE_IDS: tuple[str, ...] = tuple(spec.id for spec in EDGE_SPECS)
EDGE_BY_ID: dict[str, EdgeSpec] = {spec.id: spec for spec in EDGE_SPECS}


@dataclass
class EdgeResult:
    score: float
    reason: str
    threshold: float
    passed: bool


@dataclass
class EvalFeature:
    dataset_id: str

    # A-stage outputs
    a_report_bad: bool
    a_report_bad_tags: list[str] = field(default_factory=list)
    a_severity: str | None = None

    # B-stage outputs
    b_reject_tags: list[str] = field(default_factory=list)
    b_reject_reason: str = ''

    # C-stage outputs
    c_edge_results: dict[str, EdgeResult] = field(default_factory=dict)
    c_summary_reason: str = ''

    # Aggregated decision
    qc_passed: bool = True

    # Reserved fields
    ambiguous_query: bool | None = None
    llm_raw: dict | None = None
