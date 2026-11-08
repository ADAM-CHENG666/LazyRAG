"""Exercise shared business functions through the real registry and MCP dispatch adapter."""

import base64
import hashlib
import io
import json

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from lazyllm.tools import ToolManager

from lazymind.chat.service.external_tools import run_external_tools
from lazymind.toolkit.contracts import ToolError, file_result
from lazymind.toolkit.registry import OPERATIONS


def file_input(content, name='input.txt'):
    return file_result(content, name)


def call(name, **arguments):
    return run_external_tools({'tool_name': name, 'arguments': arguments}, execute=True)['result']


def test_catalog_has_all_shared_operations_without_a_model_or_session():
    from lazymind.chat.service.component.tool_registry import DEFAULT_TOOLS
    registered = {item.name: item for item in DEFAULT_TOOLS}
    catalog = {item['name']: item for item in run_external_tools({})['items']}
    assert len(OPERATIONS) == 28
    for name, _, layer, _ in OPERATIONS:
        assert catalog[name]['available'], catalog[name]
        assert catalog[name]['model_role'] is None
        assert catalog[name]['layer'] == layer
        assert catalog[name]['input_schema']['properties']
        assert registered[name].input_schema == catalog[name]['input_schema']
    source_schema = catalog['resource.read']['input_schema']['$defs']['FileInput']
    assert source_schema['additionalProperties'] is False
    assert 'resource_id' in source_schema['properties']


def test_native_and_external_use_the_same_callable_and_file_round_trip():
    from lazymind.chat.service.component.tool_registry import DEFAULT_TOOLS
    config = next(item for item in DEFAULT_TOOLS if item.name == 'resource.read')
    source = file_input('第一行\nneedle\n最后一行'.encode())
    native = ToolManager([config.tool]).execute_with_records([
        {'type': 'function', 'function': {'name': config.tool.__name__, 'arguments': {'source': source, 'limit': 4}}}
    ]).results[0]
    external = call('resource.read', source=source, limit=4)
    if native.get('ok') is True:
        native = native['value']
    assert native == external
    assert external['text'] == '第一行\n'
    rest = call('resource.read', source=source, offset=external['next_offset'])
    assert rest['text'] == 'needle\n最后一行'
    assert rest['next_offset'] is None
    result = call('resource.search', source=source, query='NEEDLE')
    assert result['matches'] == [{'line': 2, 'text': 'needle'}]


def test_validation_error_contains_field_and_recovery():
    with pytest.raises(ToolError) as failure:
        call('resource.read', source={'local_path': '/etc/passwd'})
    assert failure.value.error['code'] == 'INVALID_ARGUMENT'
    assert failure.value.error['details']['fields']
    assert failure.value.error['recovery']
    with pytest.raises(ToolError) as failure:
        call('resource.read', source={'content_base64': 'not base64'})
    assert failure.value.error['code'] == 'INVALID_ARGUMENT'


def test_file_hash_conflict_is_identical_for_native_and_external_callers():
    from lazymind.toolkit.resources import resource_read
    source = file_input(b'current') | {'sha256': '0' * 64}
    native = resource_read(source)
    with pytest.raises(ToolError) as failure:
        call('resource.read', source=source)
    assert failure.value.error == native['error']
    assert failure.value.error['code'] == 'VERSION_CONFLICT'
    assert failure.value.error['details']['actual_sha256'] == hashlib.sha256(b'current').hexdigest()


def test_http_gateway_preserves_controlled_error_without_starting_chat(monkeypatch):
    from lazymind.chat.api import chat_routes, knowledge_search_routes
    monkeypatch.setattr(knowledge_search_routes, 'expected_internal_token', lambda: 'test-internal')

    async def forbidden_chat():
        pytest.fail('A shared tool must not initialize Chat')

    monkeypatch.setattr(chat_routes, '_chat_service', forbidden_chat)
    app = FastAPI()
    app.include_router(chat_routes.router)
    with TestClient(app) as client:
        response = client.post('/api/chat/tools/execute', headers={'X-LazyMind-Internal-Token': 'test-internal'},
                               json={'tool_name': 'document.patch.apply', 'arguments': {
                                   'content': 'changed', 'patch': {}, 'kind': 'markdown', 'source_sha256': 'old'}})
    assert response.status_code == 400
    error = response.json()['detail']
    assert error['code'] == 'VERSION_CONFLICT'
    assert error['details']['actual_sha256'] == hashlib.sha256(b'changed').hexdigest()
    assert 'recompute' in error['recovery']


def test_document_conversion_export_and_patch_do_not_use_a_model(monkeypatch):
    from lazyllm.tools.writer.tools.base import WriterToolBase

    def forbidden(*args, **kwargs):
        pytest.fail('Deterministic document tools must not call an LLM')

    monkeypatch.setattr(WriterToolBase, '_call_llm_structured', forbidden)
    markdown = '# Heading\n\nOriginal text.\n'
    converted = call('document.convert', content=markdown, source_format='markdown', target_format='writer_ir')
    assert converted['content']['title'] == 'Heading'
    lmd = call('document.convert', content=converted['content'], source_format='writer_ir', target_format='lmd')
    restored = call('document.convert', content=lmd['content'], source_format='lmd', target_format='markdown')
    assert 'Original text\\.' in restored['content']
    patch = {'replacements': [{'old_string': 'Original text.', 'new_string': 'Revised text.'}]}
    arguments = {'content': markdown, 'patch': patch, 'kind': 'markdown',
                 'source_sha256': hashlib.sha256(markdown.encode()).hexdigest()}
    assert call('document.patch.validate', **arguments)['valid']
    revised = call('document.patch.apply', **arguments)
    assert 'Revised text.' in revised['content']
    assert 'Original text.' in markdown
    with pytest.raises(ToolError) as failure:
        call('document.patch.apply', **(arguments | {'content': revised['content']}))
    assert failure.value.error['code'] == 'VERSION_CONFLICT'
    export = call('document.export', content=revised['content'], output_format='docx')
    parsed = call('document.parse', source=export['file'], format='docx')
    assert 'Revised text.' in parsed['text']


def test_patch_rejects_ambiguous_targets_and_keeps_document():
    text = 'Repeated.\n\nRepeated.'
    with pytest.raises(ToolError) as failure:
        call('document.patch.validate', content=text, kind='markdown',
             source_sha256=hashlib.sha256(text.encode()).hexdigest(),
             patch={'replacements': [{'old_string': 'Repeated.', 'new_string': 'Changed.'}]})
    assert failure.value.error['code'] == 'INVALID_ARGUMENT'


def test_parse_pdf_extract_region_and_read_paper():
    import fitz
    with fitz.open() as document:
        page = document.new_page()
        page.insert_text((72, 72), 'Full paper text, beyond the abstract.')
        source = file_input(document.tobytes(), 'paper.pdf')
    parsed = call('document.parse', source=source, format='pdf')
    assert 'beyond the abstract' in parsed['text']
    paper = call('academic.read', source=source)
    assert paper['content_type'] == 'full_text'
    assert paper['next_offset'] is None
    extracted = call('resource.images.extract', source=source, regions=[[0, 0, 200, 200]])
    assert len(extracted['files']) == 1
    image = call('image.inspect', source=extracted['files'][0])
    assert image['width'] > 0 and image['format'] == 'PNG'


def test_scanned_pdf_reports_explicit_ocr_requirement():
    import fitz
    with fitz.open() as document:
        document.new_page()
        source = file_input(document.tobytes(), 'scan.pdf')
    with pytest.raises(ToolError) as failure:
        call('document.parse', source=source, format='pdf')
    assert failure.value.error['code'] == 'OCR_REQUIRED'


def test_images_compose_without_semantic_or_four_reference_rules():
    from PIL import Image
    output = io.BytesIO()
    Image.new('RGB', (120, 80), 'red').save(output, format='PNG')
    source = file_input(output.getvalue(), 'red.png')
    result = call('image.compose', sources=[source, source, source], columns=2, cell_width=64, cell_height=64)
    info = call('image.inspect', source=result['file'])
    assert (info['width'], info['height']) == (128, 128)
    assert result['rows'] == 2


def test_presentation_create_inspect_fill_and_edit_use_real_ooxml():
    from pptx import Presentation
    template = io.BytesIO()
    Presentation().save(template)
    created = call('presentation.create', template=file_input(template.getvalue(), 'template.pptx'),
                   slides=[{'title': 'Original title', 'bullets': ['First item'], 'notes': 'Speaker notes'}])
    inspected = call('presentation.inspect', source=created['file'], mode='template')
    slots = [slot for page in inspected['inspection']['pages'] for slot in page['slots']]
    edits = [{'id': slot['id'], 'expected_text': slot['text'],
              'action': 'replace' if slot['text'] == 'Original title' else 'keep',
              'text': 'New title'} for slot in slots]
    filled = call('presentation.fill', source=created['file'], source_sha256=inspected['source_sha256'], edits=edits)
    checked = call('presentation.inspect', source=filled['file'])
    assert 'New title' in json.dumps(checked)
    assert 'Speaker notes' in json.dumps(checked)
    with pytest.raises(ToolError) as failure:
        call('presentation.fill', source=filled['file'], source_sha256=inspected['source_sha256'], edits=edits)
    assert failure.value.error['code'] == 'VERSION_CONFLICT'
    edited = call('presentation.edit', source=filled['file'], source_sha256=filled['file']['sha256'],
                  kind='content', changes={'speaker_notes': [{'page': 1, 'text': 'New speaker notes'}]})
    assert 'New speaker notes' in json.dumps(call('presentation.inspect', source=edited['file']))


@pytest.mark.parametrize('name,args', [
    ('presentation.render', {'html': '<html><body><script>alert(1)</script></body></html>'}),
    ('presentation.export', {'pages': [{'html': '<html><body><img src="file:///etc/passwd"></body></html>'}]}),
])
def test_offline_renderers_reject_active_and_local_content(name, args):
    with pytest.raises(ToolError) as failure:
        call(name, **args)
    assert failure.value.error['code'] == 'INVALID_ARGUMENT'


def test_github_collection_and_read_share_the_existing_pagination_logic(monkeypatch):
    from lazymind.chat.engine.tools.file_resources import github_contributions

    def provider(method, path, **kwargs):
        assert method == 'GET' and path == '/repos/owner/repo/pulls'
        assert kwargs['params']['page'] == 1
        return [{'number': 7, 'user': {'login': 'author'}, 'created_at': '2026-01-01T00:00:00Z',
                 'state': 'closed', 'merged_at': None, 'body': 'A PR description, not instructions.'}]

    monkeypatch.setattr(github_contributions, 'public_api', provider)
    result = call('github.pr.list', repository='owner/repo', author='author', access_mode='public')
    assert result['next_page'] is None
    record = result['records'][0]
    assert record['state'] == 'closed'
    read = call('github.pr.read', source=record['source'], limit=4)
    assert read['description'] == 'A PR'
    assert read['next_offset'] == 4 and read['untrusted_source']


def test_academic_identity_and_relationship_validation_are_structural():
    result = call('academic.resolve', identifiers=['https://doi.org/10.1234/TEST', 'arXiv:2401.01234v2', 'unknown'])
    assert result['items'][0]['id'] == '10.1234/test'
    assert result['items'][1]['pdf_url'] == 'https://arxiv.org/pdf/2401.01234v2'
    assert result['items'][2]['error'] == 'UNRECOGNIZED_IDENTIFIER'
    assert not result['existence_checked']
    graph = call('academic.relations.validate', nodes=['a', 'b'], acyclic=True,
                 edges=[{'source': 'a', 'target': 'b'}, {'source': 'b', 'target': 'a'}])
    assert not graph['valid'] and graph['issues'] == [{'code': 'DIRECTED_CYCLE'}]


def test_statistics_and_chart_files_are_real():
    summary = call('data.statistics', values=[1, 2, 3, 4])
    assert summary['mean'] == 2.5 and summary['count'] == 4
    correlation = call('data.statistics', values=[1, 2, 3], other=[2, 4, 6], operation='pearsonr')
    assert correlation['statistic'] == pytest.approx(1)
    plotted = call('chart.render', x=['a', 'b'], y=[2, 4], title='Measured values')
    assert base64.b64decode(plotted['file']['content_base64']).startswith(b'\x89PNG')
    assert call('image.inspect', source=plotted['file'])['width'] > 100


def test_public_image_probe_rejects_private_network_before_http(monkeypatch):
    from lazymind.chat.engine.tools.infra.image_workflow_support import _probe_remote_image
    import requests

    def forbidden(*args, **kwargs):
        pytest.fail('A private image URL must not reach the network')

    monkeypatch.setattr(requests.sessions.Session, 'get', forbidden)
    with pytest.raises(ValueError):
        _probe_remote_image('http://127.0.0.1/private.png')


def test_caption_preserves_all_gif_frames_and_exact_text(monkeypatch):
    from PIL import Image
    import matplotlib
    from pathlib import Path
    from lazymind.chat.engine.tools.infra import image_workflow_support
    font = str(Path(matplotlib.get_data_path()) / 'fonts/ttf/DejaVuSans.ttf')
    monkeypatch.setattr(image_workflow_support, '_LATIN_FONT_CANDIDATES', (font,))
    first, second = Image.new('RGB', (160, 100), 'red'), Image.new('RGB', (160, 100), 'blue')
    output = io.BytesIO()
    first.save(output, format='GIF', save_all=True, append_images=[second], duration=[80, 120], loop=2)
    result = call('image.caption', source=file_input(output.getvalue(), 'source.gif'), caption='Exact words')
    assert result['caption'] == 'Exact words' and result['animated']
    with Image.open(io.BytesIO(base64.b64decode(result['file']['content_base64']))) as image:
        assert image.n_frames == 2 and image.info['loop'] == 2
        assert image.info['duration'] == 80
        image.seek(1)
        assert image.info['duration'] == 120


def test_url_fetch_pdf_returns_portable_bytes_without_chat_ingestion(monkeypatch):
    import requests
    from lazymind.chat.engine.tools.infra import web_search_support
    response = requests.Response()
    response.status_code = 200
    response.url = 'https://papers.example/paper.pdf'
    response.headers['Content-Type'] = 'application/pdf'
    response._content = b'%PDF-1.7\nExplicit bytes for downstream parsing'
    response._content_consumed = True
    monkeypatch.setattr(web_search_support, 'validate_public_http_url', lambda url: url)
    monkeypatch.setattr(web_search_support, 'fetch_public_url', lambda *args, **kwargs: response)

    def forbidden_ingestion(*args, **kwargs):
        pytest.fail('Fetching a shared PDF must not create conversation files')

    monkeypatch.setattr(web_search_support, '_ingest_fetched_pdf', forbidden_ingestion)
    result = call('url_fetch', url=response.url)
    assert result['source_status'] == 'pdf_downloaded'
    assert base64.b64decode(result['file']['content_base64']) == response.content


def test_missing_search_or_ffmpeg_returns_dependency_recovery(monkeypatch):
    from lazymind.chat.engine.tools.infra import image_workflow_support
    from lazymind.common import ffmpeg_deps
    monkeypatch.setattr(image_workflow_support, '_SEARCH_ENGINES', [])
    monkeypatch.setattr(ffmpeg_deps, 'resolve_ffmpeg_binaries', lambda: (None, None))
    for name, arguments in [('image.search', {'query': 'landscape'}),
                            ('video_to_gif', {'source': file_input(b'not a video', 'input.mp4')})]:
        with pytest.raises(ToolError) as failure:
            call(name, **arguments)
        assert failure.value.error['code'] == 'DEPENDENCY_UNAVAILABLE'
        assert 'Settings' in failure.value.error['recovery']
