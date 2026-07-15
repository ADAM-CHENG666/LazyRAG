from __future__ import annotations

import hashlib
import inspect
import json
import re
from collections.abc import Callable, Mapping
from typing import Any, TypeVar

from json_repair import repair_json

DEFAULT_LLM_JSON_TIMEOUT_SECONDS = 180
T = TypeVar('T')


def call_json(
    llm: Callable[..., Any],
    prompt: str,
    validate: Callable[[Mapping[str, Any]], T],
    *,
    max_retries: int = 3,
) -> T:
    """Call an LLM in JSON mode, recover common wrappers, then validate its contract."""
    if max_retries < 1:
        raise ValueError('max_retries must be at least 1')

    diagnostics: list[dict[str, Any]] = []
    last_error: Exception | None = None
    for attempt in range(1, max_retries + 1):
        raw = None
        try:
            raw = _invoke(llm, prompt)
            return validate(_json_object(raw))
        except Exception as exc:
            last_error = exc
            diagnostics.append(_diagnostic(attempt, raw, exc))
    raise ValueError(
        f'LLM JSON call failed after {max_retries} attempts: {last_error}; diagnostics={diagnostics}'
    ) from last_error


def _invoke(llm: Callable[..., Any], prompt: str) -> Any:
    kwargs = {
        'stream': False,
        'response_format': {'type': 'json_object'},
        'timeout': DEFAULT_LLM_JSON_TIMEOUT_SECONDS,
    }
    try:
        parameters = inspect.signature(llm).parameters.values()
    except (TypeError, ValueError):
        return llm(prompt, **kwargs)
    if any(parameter.kind is inspect.Parameter.VAR_KEYWORD for parameter in parameters):
        return llm(prompt, **kwargs)
    accepted = {parameter.name for parameter in parameters}
    return llm(prompt, **{name: value for name, value in kwargs.items() if name in accepted})


def _json_object(raw: Any) -> Mapping[str, Any]:
    if isinstance(raw, Mapping):
        return raw
    text = re.sub(r'<think>.*?</think>', '', str(raw), flags=re.DOTALL).strip()
    fenced = re.search(r'```(?:json)?\s*(\{.*\})\s*```', text, flags=re.DOTALL)
    if fenced:
        text = fenced.group(1)
    else:
        start = text.find('{')
        end = text.rfind('}')
        if start >= 0 and end > start:
            text = text[start:end + 1]
    try:
        value = json.loads(text)
    except json.JSONDecodeError:
        value = repair_json(text, return_objects=True)
    if not isinstance(value, Mapping):
        raise ValueError(f'LLM response JSON must be an object, got {type(value).__name__}')
    return value


def _diagnostic(attempt: int, raw: Any, error: Exception) -> dict[str, Any]:
    text = _text(raw)
    return {
        'attempt': attempt,
        'response_type': type(raw).__name__,
        'response_chars': len(text),
        'response_sha256': hashlib.sha256(text.encode('utf-8')).hexdigest()[:16],
        'error': str(error),
    }


def _text(value: Any) -> str:
    if isinstance(value, str):
        return value
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
    except (TypeError, ValueError):
        return repr(value)
