from __future__ import annotations

from pathlib import Path

import pytest


_TEST_ROOT = Path(__file__).parent
_LAYERS = {'unit', 'runtime', 'integration'}


def pytest_configure(config: pytest.Config) -> None:
    for layer in sorted(_LAYERS):
        config.addinivalue_line('markers', f'projection_{layer}: ProjectionService {layer} tests')


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    for item in items:
        try:
            layer = item.path.relative_to(_TEST_ROOT).parts[0]
        except ValueError:
            continue
        if layer in _LAYERS:
            item.add_marker(f'projection_{layer}')
