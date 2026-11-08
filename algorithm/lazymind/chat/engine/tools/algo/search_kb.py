from typing import Any, Callable, List, Optional

from lazyllm import Document, parallel
from lazyllm.tools.rag import Reranker, Retriever
from lazyllm.tools.rag.rank_fusion.reciprocal_rank_fusion import RRFFusion

from lazymind.chat.engine.tools.algo.kb_adaptive_topk import AdaptiveKComponent
from lazymind.chat.engine.tools.algo.kb_context_expansion import ContextExpansionComponent
from lazymind.config import config as _cfg
from lazymind.chat.engine.tools.infra.kb_ownership import filter_active_nodes


def _adaptive_get_token_len(n: Any) -> int:
    txt = getattr(n, 'text', '') or ''
    return max(1, len(txt) // 4)


def _pass_through_rerank(nodes):
    for node in nodes or []:
        if getattr(node, 'relevance_score', None) is None:
            node.relevance_score = getattr(node, 'score', None) or getattr(node, 'similarity_score', None) or 0.0
    return nodes


DOCUMENT = Document(url=f'{_cfg["agentic_kb_url"]}/_call', name=_cfg['algo_id'])

_adaptive_k = AdaptiveKComponent(
    bias=2, gap_tau=0.2, get_token_len=_adaptive_get_token_len, max_tokens=2048,
)
_ctx_expand = ContextExpansionComponent(
    document=DOCUMENT, token_budget=1500, score_decay=0.97, max_seeds=1,
)


def _search_text(
    expanded: str,
    retrieve_fn: Callable[[str], Any],
    reranker: Optional[Reranker],
    rerank_topk: int,
    k_max: int,
) -> List[Any]:
    nodes = retrieve_fn(expanded)
    ranked = reranker(nodes, query=expanded, topk=rerank_topk) if reranker else _pass_through_rerank(nodes)
    merged = _adaptive_k(ranked or [], k_max=k_max)
    return _ctx_expand(merged)


def _fuse_retriever_results(results):
    nodes = tuple(r for r in results if r)
    return RRFFusion(top_k=50)(nodes)


def search_kb(
    payload: dict,
    *,
    retrievers: List[Retriever],
    reranker: Optional[Reranker],
    image_retriever: Optional[Retriever],
    retriever_topk: int = 20,
    rerank_topk: int = 20,
    k_max: int = 10,
    image_topk: int = 3,
):
    query = payload['query']
    # Query formulation belongs to the Agent; retrieval never invokes a chat LLM.
    expanded = query

    def _kb_retrieve(expanded: str):
        filters = payload.get('filters') or {}
        retrieval_kwargs = {'filters': filters, 'topk': retriever_topk}
        if payload.get('llm_config'):
            retrieval_kwargs['llm_config'] = payload['llm_config']
        results = parallel(*retrievers)(
            expanded, **retrieval_kwargs
        )
        return filter_active_nodes(_fuse_retriever_results(results), user_id=payload['user_id'])

    text_nodes = filter_active_nodes(
        _search_text(expanded, _kb_retrieve, reranker, rerank_topk, k_max),
        user_id=payload['user_id'],
    )

    if image_retriever is None:
        return text_nodes

    image_kwargs = {'filters': payload.get('filters') or {}, 'topk': image_topk}
    if payload.get('llm_config'):
        image_kwargs['llm_config'] = payload['llm_config']
    image_nodes = filter_active_nodes(
        list(image_retriever(query, **image_kwargs) or []), user_id=payload['user_id'],
    )
    return list(text_nodes or []) + image_nodes[:image_topk]


def keyword_search(payload: dict, top_k: int = 10):
    """Model-free retrieval over explicitly authorized KBs, shared by Chat and MCP."""
    filters = payload['filters']
    if set(filters) - {'kb_id', 'file_name', 'docid', 'doc_id'}:
        raise ValueError('Keyword search supports kb_id, file_name and doc_id filters only')
    selection = {}
    if filters.get('file_name'):
        selection['file_name'] = filters['file_name']
    if filters.get('doc_id') or filters.get('docid'):
        selection['doc_id'] = filters.get('doc_id') or filters['docid']
    items = []
    for kb_id in filters['kb_id']:
        rows = DOCUMENT.keyword_search(group='block', keyword=payload['query'], kb_id=kb_id,
                                       phrase=True, sort_by='score', size=top_k, **selection)
        items.extend(filter_active_nodes(rows, user_id=payload['user_id']))
    return items[:top_k]
