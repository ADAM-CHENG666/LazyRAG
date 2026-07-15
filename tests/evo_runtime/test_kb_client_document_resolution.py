from __future__ import annotations

import sys
import types

from evo.operations.dataset import kb_client
from evo.operations.dataset.kb_client import KnowledgeBaseClient


class Config(dict):
    def __getitem__(self, key):
        return dict.get(self, key, '')


def test_get_document_uses_injected_document_first():
    document = object()

    assert KnowledgeBaseClient(document=document)._get_document() is document


def test_get_document_uses_injected_factory_before_runtime_defaults():
    document = object()
    calls = []
    client = KnowledgeBaseClient(document_factory=lambda: calls.append('called') or document)

    assert client._get_document() is document
    assert client._get_document() is document
    assert calls == ['called']


def test_get_document_builds_document_from_current_algorithm(monkeypatch):
    kb_client._DOCUMENTS.clear()
    calls = []
    config_mod = types.ModuleType('lazymind.config')
    config_mod.config = Config(algo_id='algo-1', agentic_kb_name='fallback', agentic_kb_url='http://old')
    build_mod = types.ModuleType('lazymind.parsing.service.build_document')

    def build_document(algo_id, *, serve=True):
        calls.append((algo_id, serve))
        return {'algo_id': algo_id}

    build_mod.build_document = build_document
    monkeypatch.setitem(sys.modules, 'lazymind', types.ModuleType('lazymind'))
    monkeypatch.setitem(sys.modules, 'lazymind.config', config_mod)
    monkeypatch.setitem(sys.modules, 'lazymind.parsing', types.ModuleType('lazymind.parsing'))
    monkeypatch.setitem(sys.modules, 'lazymind.parsing.service', types.ModuleType('lazymind.parsing.service'))
    monkeypatch.setitem(sys.modules, 'lazymind.parsing.service.build_document', build_mod)

    client = KnowledgeBaseClient()

    assert client._get_document() == {'algo_id': 'algo-1'}
    assert client._get_document() == {'algo_id': 'algo-1'}
    assert calls == [('algo-1', False)]
