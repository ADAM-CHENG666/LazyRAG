from __future__ import annotations


CANONICAL_LAYOUT_TYPE_IDS = (
    'text', 'heading', 'paragraph', 'table', 'formula', 'figure', 'code', 'list', 'unknown',
)
_LAYOUT_TYPE_ALIASES = {
    'content': 'text',
    'header': 'heading',
    'title': 'heading',
    'equation': 'formula',
    'image': 'figure',
    'code_block': 'code',
}


def canonical_layout_type(value: object) -> str:
    raw = str(value or '').strip().lower()
    normalized = _LAYOUT_TYPE_ALIASES.get(raw, raw)
    return normalized if normalized in CANONICAL_LAYOUT_TYPE_IDS else 'unknown'


def validate_layout_types(values: list[str]) -> list[str]:
    invalid = [value for value in values if value not in CANONICAL_LAYOUT_TYPE_IDS]
    if invalid:
        raise ValueError('allowed_types contains an unsupported standard layout type')
    return values
