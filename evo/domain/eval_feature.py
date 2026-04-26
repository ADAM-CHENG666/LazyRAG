from __future__ import annotations

from dataclasses import dataclass, field


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
