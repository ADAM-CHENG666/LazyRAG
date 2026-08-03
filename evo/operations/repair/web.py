from __future__ import annotations

import hashlib
import importlib
import json
import re
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, quote_plus, unquote, urljoin, urlparse

import requests
from bs4 import BeautifulSoup

from .memory import content_ref, write_json


def search_web(query: str, artifact_root: Path, limit: int = 5) -> dict[str, Any]:
    """Use the registered web-search providers; snippets are discovery data, not evidence."""
    question = ' '.join(str(query or '').split())
    if not question:
        raise ValueError('web_search_query_empty')
    failures = []
    try:
        providers = _web_search_providers()
    except Exception as exc:
        providers = []
        failures.append(type(exc).__name__)
    raw: object = []
    for provider in providers:
        try:
            candidate = provider.search(question)
        except Exception as exc:
            failures.append(type(exc).__name__)
            continue
        items = candidate if isinstance(candidate, list) else (
            candidate.get('results') if isinstance(candidate, Mapping) else []
        )
        if items:
            raw = candidate
            break
        failures.append(f'{type(provider).__name__}:empty')
    if not raw:
        try:
            raw = _open_search(question, limit)
        except Exception as exc:
            failures.append(f'OpenSearch:{type(exc).__name__}')
    if not raw:
        result = {'query': question, 'results': [], 'status': 'unavailable', 'failures': failures}
        write_json(artifact_root / 'web' / 'searches' / f'{_digest(question)}.json', result)
        return result
    items = raw if isinstance(raw, list) else raw.get('results') if isinstance(raw, Mapping) else []
    results = []
    seen = set()
    for item in items or ():
        if not isinstance(item, Mapping):
            continue
        url = str(item.get('url') or item.get('link') or '').strip()
        if not url or url in seen:
            continue
        seen.add(url)
        results.append({
            'title': str(item.get('title') or item.get('name') or '').strip(),
            'url': url,
            'snippet': str(item.get('snippet') or item.get('description') or item.get('content') or '').strip()[:1000],
        })
        if len(results) >= limit:
            break
    result = {'query': question, 'results': results, 'status': 'completed'}
    write_json(artifact_root / 'web' / 'searches' / f'{_digest(question)}.json', result)
    return result


def read_web_pages(question: str, urls: Sequence[str], work_root: Path, artifact_root: Path, *,
                   seen_urls: set[str] | None = None) -> dict[str, Any]:
    """Fetch selected pages through the existing url_fetch tool and persist readable bodies."""
    prompt = ' '.join(str(question or '').split())
    selected = list(dict.fromkeys(str(url).strip() for url in urls if str(url).strip()))[:3]
    if not prompt or not selected:
        raise ValueError('web_read_requires_question_and_urls')
    from lazymind.chat.engine.tools.web_search import url_fetch

    try:
        response = url_fetch(urls=selected)
    except Exception as exc:
        pages = [{'status': 'failed', 'title': '', 'url': url, 'excerpt': '', 'content_ref': None,
                  'reason': type(exc).__name__} for url in selected]
        result = {'question': prompt, 'pages': pages}
        write_json(artifact_root / 'web' / 'reads' / f'{_digest(prompt + json.dumps(selected))}.json', result)
        return result
    pages = []
    seen_final = set(seen_urls or ())
    for requested, page, error in _fetched_pages(selected, response):
        final_url = str(page.get('final_url') or page.get('url') or requested).strip()
        if final_url in seen_final:
            continue
        seen_final.add(final_url)
        page = _enhance_page(final_url, page)
        final_url = str(page.get('final_url') or page.get('url') or final_url).strip()
        content = str(page.get('content') or '').strip()
        content_type = str(page.get('content_type') or '').casefold()
        status = (
            'failed' if error else
            'unsupported' if content_type and not any(token in content_type for token in ('html', 'text', 'json', 'xml')) else
            'empty' if not content else
            'readable'
        )
        page_ref = None
        if status == 'readable':
            name = f'page-{len(pages) + 1:02d}-{_digest(final_url)[:12]}.txt'
            work_path = work_root / 'web' / 'pages' / name
            artifact_path = artifact_root / 'web' / 'pages' / name
            work_path.parent.mkdir(parents=True, exist_ok=True)
            artifact_path.parent.mkdir(parents=True, exist_ok=True)
            work_path.write_text(content, encoding='utf-8')
            artifact_path.write_text(content, encoding='utf-8')
            page_ref = content_ref(artifact_path, artifact_root)
        pages.append({
            'status': status,
            'title': str(page.get('title') or '').strip(),
            'url': final_url,
            'excerpt': _relevant_excerpt(prompt, content) if status == 'readable' else '',
            'content_ref': page_ref,
            'reason': error,
        })
    result = {'question': prompt, 'pages': pages}
    write_json(artifact_root / 'web' / 'reads' / f'{_digest(prompt + json.dumps(selected))}.json', result)
    return result


def _enhance_page(url: str, page: Mapping[str, Any]) -> Mapping[str, Any]:
    """Replace navigation-heavy HTML extraction with Repair's focused body.

    The shared url_fetch remains the network/security boundary. Repair performs
    a second safe read only for HTML so documentation pages can keep tables and
    code examples that the shared 4k generic extraction often truncates.
    """
    content_type = str(page.get('content_type') or '').casefold()
    if content_type and 'html' not in content_type and 'xhtml' not in content_type:
        return page
    try:
        enhanced = _fetch_repair_page(url)
    except Exception:
        return page
    return enhanced if str(enhanced.get('content') or '').strip() else page


def _fetch_repair_page(url: str) -> dict[str, Any]:
    from lazymind.chat.engine.tools.infra.web_search_support import fetch_public_url

    headers = {
        'User-Agent': (
            'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
            '(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36'
        ),
    }
    with requests.Session() as session:
        response = fetch_public_url(session, url, timeout=15, headers=headers)
        response.raise_for_status()
        soup = BeautifulSoup(response.text, 'html.parser')
        if redirect := _html_redirect(response.url, soup):
            response = fetch_public_url(session, redirect, timeout=15, headers=headers)
            response.raise_for_status()
            soup = BeautifulSoup(response.text, 'html.parser')
    return {
        'url': url,
        'final_url': response.url,
        'content_type': str(response.headers.get('Content-Type') or 'text/html'),
        'title': str(soup.title.string if soup.title and soup.title.string else '').strip(),
        'content': _extract_repair_text(soup),
    }


def _html_redirect(base_url: str, soup: BeautifulSoup) -> str:
    refresh = soup.find('meta', attrs={'http-equiv': re.compile(r'^refresh$', re.I)})
    if refresh:
        match = re.search(r'url\s*=\s*[\"\']?([^\"\';]+)', str(refresh.get('content') or ''), re.I)
        if match:
            return urljoin(base_url, match.group(1).strip())
    canonical = soup.find('link', attrs={'rel': lambda value: value and 'canonical' in value})
    href = str(canonical.get('href') or '').strip() if canonical else ''
    return urljoin(base_url, href) if href else ''


def _extract_repair_text(soup: BeautifulSoup, limit: int = 60_000) -> str:
    root = (
        soup.select_one('#main-content')
        or soup.select_one('[role="main"] article')
        or soup.find('article')
        or soup.select_one('.td-content')
        or soup.find('main')
        or soup.body
        or soup
    )
    for tag in root.select('script, style, noscript, nav, aside, footer'):
        tag.decompose()
    lines = []
    seen = set()
    for node in root.find_all(['h1', 'h2', 'h3', 'h4', 'p', 'li', 'pre', 'tr']):
        text = ' '.join(node.get_text(' ', strip=True).split())
        if text and text not in seen:
            seen.add(text)
            lines.append(text)
    if not lines:
        lines = [line.strip() for line in root.get_text('\n', strip=True).splitlines() if line.strip()]
    return '\n'.join(lines)[:limit]


def _web_search_providers() -> list[Any]:
    """Load only providers present in this LazyLLM build.

    Repair owns this compatibility boundary because provider availability differs
    between the Evo image and the chat-service image.
    """
    search = importlib.import_module('lazyllm.tools.tools.search')
    providers = []
    for name in ('GoogleSearch', 'BingSearch', 'BochaSearch', 'TavilySearch'):
        provider_type = getattr(search, name, None)
        if provider_type is None:
            continue
        try:
            providers.append(provider_type())
        except Exception:
            continue
    return providers


def _open_search(question: str, limit: int) -> list[dict[str, str]]:
    """Credential-free Repair fallback for deployments where HTML search is blocked."""
    from lazymind.chat.engine.tools.infra.web_search_support import fetch_public_url

    headers = {
        'User-Agent': (
            'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
            '(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36'
        ),
    }
    for query in _search_query_variants(question):
        url = f'https://lite.duckduckgo.com/lite/?q={quote_plus(query)}'
        with requests.Session() as session:
            response = fetch_public_url(session, url, timeout=15, headers=headers)
            response.raise_for_status()
        if results := _parse_open_results(response.text, limit):
            return results
    return []


def _search_query_variants(question: str) -> list[str]:
    """Normalize Agent queries locally without extending the shared WebSearch tool."""
    normalized = ' '.join(str(question or '').split())
    if not normalized:
        return []
    concise = ' '.join(normalized.split()[:10])
    return list(dict.fromkeys((concise, normalized)))


def _parse_open_results(html: str, limit: int) -> list[dict[str, str]]:
    results = []
    for link in BeautifulSoup(html, 'html.parser').select('a.result-link'):
        href = str(link.get('href') or '').strip()
        if href.startswith('//'):
            href = f'https:{href}'
        parsed = urlparse(href)
        redirect = parse_qs(parsed.query).get('uddg') if parsed.netloc.endswith('duckduckgo.com') else None
        target = unquote(redirect[0]) if redirect else href
        if urlparse(target).scheme not in {'http', 'https'}:
            continue
        results.append({
            'title': link.get_text(' ', strip=True),
            'url': target,
            'snippet': '',
        })
        if len(results) >= max(1, min(int(limit), 20)):
            break
    return results


def _fetched_pages(urls: list[str], response: object) -> list[tuple[str, Mapping[str, Any], str]]:
    payload = response.get('result') if isinstance(response, Mapping) else None
    if not isinstance(payload, Mapping):
        return [(url, {}, 'url_fetch_invalid_response') for url in urls]
    rows = payload.get('results')
    if not isinstance(rows, list):
        return [(urls[0], payload, '')]
    result = []
    for item in rows:
        if not isinstance(item, Mapping):
            continue
        requested = str(item.get('url') or '').strip()
        page = item.get('result') if isinstance(item.get('result'), Mapping) else {}
        error = '' if item.get('success') is True else str(item.get('error') or 'url_fetch_failed')
        result.append((requested, page, error))
    return result


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _relevant_excerpt(question: str, content: str, limit: int = 1200) -> str:
    paragraphs = [line.strip() for line in content.splitlines() if line.strip()]
    if not paragraphs:
        return ''
    english = set(re.findall(r'[a-z0-9_]{2,}', question.casefold()))
    chinese = set(re.findall(r'[\u4e00-\u9fff]', question))
    ranked = sorted(
        enumerate(paragraphs),
        key=lambda item: (
            -sum(item[1].casefold().count(term) for term in english)
            -sum(char in item[1] for char in chinese),
            item[0],
        ),
    )
    selected = sorted(ranked[:3])
    return '\n'.join(paragraph for _, paragraph in selected)[:limit]
