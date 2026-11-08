"""Explicit-input presentation tools, using the existing deterministic PPT engine."""

import hashlib
import tempfile
from pathlib import Path
from typing import Literal

from .contracts import FileInput, ToolError, check_office, file_result, public_tool, read_file


def pptx_source(value, root):
    content = check_office(read_file(value))
    path = Path(root) / 'source.pptx'
    path.write_bytes(content)
    return path


def safe_metadata(value):
    if isinstance(value, dict):
        return {key: safe_metadata(item) for key, item in value.items()
                if key not in {'local_path', 'output', 'output_path', 'source', 'source_path', 'font_path', 'path'}}
    if isinstance(value, list):
        return [safe_metadata(item) for item in value]
    return value


def check_revision(path, expected):
    actual = hashlib.sha256(path.read_bytes()).hexdigest()
    if actual != expected:
        raise ToolError('VERSION_CONFLICT', 'The PPTX changed after inspection.',
                        details={'actual_sha256': actual}, recovery='Call presentation.inspect and rebuild the edit.')


@public_tool
def presentation_inspect(source: FileInput, mode: Literal['content', 'template', 'layout'] = 'content',
                         pages: list[int] | None = None) -> dict:
    """Inspect PPTX text, shapes, template slots or layout geometry; no visual model.

    Args:
        source: PPTX bytes or owned resource.
        mode: Content, template slot inventory or layout geometry.
        pages: Optional one-based pages for content/layout; template inventories all pages.
    """
    from lazymind.chat.engine.tools.file_resources.pptx_content_edit import inspect_content
    from lazymind.chat.engine.tools.file_resources.pptx_template_fill import inspect_template
    from lazymind.chat.engine.tools.file_resources.pptx_layout_edit import layout_snapshot
    with tempfile.TemporaryDirectory(prefix='lazymind-ppt-') as root:
        path = pptx_source(source, root)
        if mode == 'template':
            result = inspect_template(path, cache_workspace=Path(root) / 'inspection-cache', all_pages=True)
        else:
            result = (layout_snapshot if mode == 'layout' else inspect_content)(path, pages=pages)
        return {'source_sha256': hashlib.sha256(path.read_bytes()).hexdigest(), 'inspection': safe_metadata(result)}


@public_tool
def presentation_fill(source: FileInput, source_sha256: str, edits: list[dict]) -> dict:
    """Fill explicit existing template slots, preserving other shapes and validating original text.

    Args:
        source: Template PPTX.
        source_sha256: Hash returned by presentation.inspect.
        edits: Every inspected slot needs id, expected_text and action=keep/replace; replacements also need text.
    """
    from lazymind.chat.engine.tools.file_resources.pptx_template_fill import fill_template
    with tempfile.TemporaryDirectory(prefix='lazymind-ppt-') as root:
        path = pptx_source(source, root)
        check_revision(path, source_sha256)
        output = Path(root) / 'filled.pptx'
        result = fill_template(path, output, source_sha256, edits)
        return {'file': file_result(output.read_bytes(), output.name), 'result': safe_metadata(result)}


@public_tool
def presentation_create(slides: list[dict], template: FileInput | None = None,
                        font_family: str = '') -> dict:
    """Create editable native slides from supplied title/bullets/notes; never generate content.

    Args:
        slides: 1..100 slide objects containing title, bullets and optional notes.
        template: Optional template whose masters/layouts are reused, excluding sample slides.
        font_family: Optional installed portable font; default uses the existing renderer's choice.
    """
    from lazymind.chat.engine.tools.file_resources.native_presentation import build_native_pptx
    if not 1 <= len(slides) <= 100:
        raise ToolError('INVALID_ARGUMENT', 'Provide 1..100 slides.')
    with tempfile.TemporaryDirectory(prefix='lazymind-ppt-') as root:
        source = pptx_source(template, root) if template else None
        output = Path(root) / 'presentation.pptx'
        result = build_native_pptx(slides, output, source, font_family or None)
        return {'file': file_result(output.read_bytes(), output.name), 'result': safe_metadata(result)}


@public_tool
def presentation_edit(source: FileInput, source_sha256: str,
                      kind: Literal['content', 'structure', 'layout', 'fonts'], changes: dict) -> dict:
    """Apply explicit content, structure, layout or font edits through the existing PPT engine.

    Args:
        source: PPTX file.
        source_sha256: Expected file version from inspection.
        kind: Engine operation; no automatic review or repair.
        changes: content: text_edits/speaker_notes/chart_edits; structure: operations;
            layout: plan/allowed_pages/allowed_shapes; fonts: edits (style batch).
    """
    from lazymind.chat.engine.tools.file_resources.pptx_content_edit import edit_content
    from lazymind.chat.engine.tools.file_resources.pptx_structure_edit import edit_structure
    from lazymind.chat.engine.tools.file_resources.pptx_layout_edit import apply_layout_repairs
    from lazymind.chat.engine.tools.file_resources.pptx_font_edit import edit_style_batch
    allowed = {'content': {'text_edits', 'speaker_notes', 'chart_edits'}, 'structure': {'operations'},
               'layout': {'plan', 'allowed_pages', 'allowed_shapes'}, 'fonts': {'edits'}}[kind]
    if not changes or set(changes) - allowed:
        raise ToolError('INVALID_ARGUMENT', 'Unknown or empty edit fields.', details={'allowed': sorted(allowed)})
    with tempfile.TemporaryDirectory(prefix='lazymind-ppt-') as root:
        path = pptx_source(source, root)
        check_revision(path, source_sha256)
        output = Path(root) / 'edited.pptx'
        function = {'content': edit_content, 'structure': edit_structure,
                    'layout': apply_layout_repairs, 'fonts': edit_style_batch}[kind]
        result = function(path, output, source_sha256=source_sha256, **changes)
        return {'file': file_result(output.read_bytes(), output.name), 'result': safe_metadata(result)}


@public_tool
def presentation_render(source: FileInput | None = None, html: str = '', page: int = 1) -> dict:
    """Render offline HTML or a PPTX page without visual evaluation. Requires browser CLI/LibreOffice.

    Args:
        source: PPTX file, mutually exclusive with html.
        html: Offline declarative HTML accepted by the existing PPT renderer.
        page: One-based PPTX page to return.
    """
    from .ppt_html import ppt_render_html
    from lazymind.chat.engine.tools.file_resources.pptx_visual_review import render_pages
    if bool(source) == bool(html):
        raise ToolError('INVALID_ARGUMENT', 'Supply exactly one of source and html.')
    if html:
        rendered = ppt_render_html(html)
        return {'file': rendered['files'][0], 'page': 1, 'page_count': 1}
    import os
    import shutil
    if not (os.environ.get('LAZYMIND_LIBREOFFICE_PATH') or shutil.which('soffice')
            or shutil.which('libreoffice') or Path('/Applications/LibreOffice.app/Contents/MacOS/soffice').is_file()):
        raise ToolError('DEPENDENCY_UNAVAILABLE', 'PPTX rendering requires LibreOffice.',
                        recovery='Configure LAZYMIND_LIBREOFFICE_PATH in the service environment and retry.')
    with tempfile.TemporaryDirectory(prefix='lazymind-ppt-') as root:
        path = pptx_source(source, root)
        images = render_pages(path, Path(root) / 'render')
        if not 1 <= page <= len(images):
            raise ToolError('INVALID_ARGUMENT', 'page is outside the presentation.')
        return {'file': file_result(images[page - 1].read_bytes(), images[page - 1].name),
                'page': page, 'page_count': len(images)}


@public_tool
def presentation_export(pages: list[dict], filename: str = 'presentation.pptx') -> dict:
    """Export supplied offline HTML pages to editable PPTX using the audited renderer.

    Args:
        pages: Objects with html and optional notes, at most 100 pages.
        filename: Output basename ending in .pptx.
    """
    from .ppt_html import ppt_export_html
    exported = ppt_export_html(pages, filename)
    return {'file': exported['files'][0], 'page_count': exported['page_count'], 'validation_owner': 'caller'}
