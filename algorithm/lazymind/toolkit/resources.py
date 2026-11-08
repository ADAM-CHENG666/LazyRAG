"""Read and parse explicit files; never access an implicit workspace or a model."""

import hashlib
import re
import tempfile
from pathlib import Path
from typing import Literal

from .contracts import (FileInput, ToolError, check_office, file_result, materialize,
                        public_tool, read_file, text_window)


@public_tool
def resource_read(source: FileInput, offset: int = 0, limit: int = 12000) -> dict:
    """Read a UTF-8 text file with character pagination. For binary documents use document.parse.

    Args:
        source: Inline file or owned resource_id; no filesystem path.
        offset: Character offset, initially zero.
        limit: Maximum characters, up to 100000.
    """
    content = read_file(source)
    return {**text_window(content.decode('utf-8-sig'), offset, limit),
            'sha256': hashlib.sha256(content).hexdigest()}


@public_tool
def resource_search(source: FileInput, query: str, regex: bool = False,
                    case_sensitive: bool = False, offset: int = 0, limit: int = 100) -> dict:
    """Search lines in one explicit UTF-8 file; results include line numbers and pagination.

    Args:
        source: Explicit UTF-8 file.
        query: Literal text or a regular expression.
        regex: Interpret query as regex with a one-second timeout.
        case_sensitive: Match exact case when true.
        offset: Number of matching lines already consumed.
        limit: Maximum matches, 1..1000.
    """
    import regex as regex_engine
    if not query or len(query) > 4000 or offset < 0 or not 1 <= limit <= 1000:
        raise ToolError('INVALID_ARGUMENT', 'Provide a query, nonnegative offset and limit 1..1000.')
    text = read_file(source).decode('utf-8-sig')
    pattern = query if regex else re.escape(query)
    try:
        compiled = regex_engine.compile(pattern, 0 if case_sensitive else regex_engine.IGNORECASE)
        matches = []
        consumed = 0
        # A single bounded search over the whole file avoids resetting the timeout per line.
        found = set()
        for match in compiled.finditer(text, timeout=1):
            start = text.rfind('\n', 0, match.start()) + 1
            if start in found:
                continue
            found.add(start)
            if consumed >= offset:
                end = text.find('\n', start)
                matches.append({'line': text.count('\n', 0, start) + 1,
                                'text': text[start:end if end >= 0 else len(text)][:12000]})
                if len(matches) > limit:
                    break
            consumed += 1
    except (regex_engine.error, TimeoutError):
        raise ToolError('INVALID_ARGUMENT', 'Regex is invalid or exceeded its time limit; simplify the query.') from None
    return {'matches': matches[:limit], 'next_offset': offset + limit if len(matches) > limit else None}


def parse_text(content, kind):
    if kind == 'pdf':
        import fitz
        with fitz.open(stream=content, filetype='pdf') as document:
            if document.page_count > 1000:
                raise ToolError('RESOURCE_LIMIT', 'Parse at most 1000 PDF pages per file.')
            pages = [page.get_text() for page in document]
        if not any(page.strip() for page in pages):
            raise ToolError('OCR_REQUIRED', 'This PDF has no extractable text.',
                            recovery='Use an explicit OCR capability, then submit its extracted text.')
        return '\n\n'.join(pages), {'page_count': len(pages)}
    if kind == 'docx':
        from lazymind.chat.engine.document_extractor import LocalDocumentReader
        with tempfile.TemporaryDirectory(prefix='lazymind-docx-') as root:
            path = Path(root) / 'document.docx'
            path.write_bytes(check_office(content))
            nodes = LocalDocumentReader._read_docx(path)
        return '\n'.join(node.text for node in nodes), {}
    text = content.decode('utf-8-sig')
    if kind == 'html':
        from bs4 import BeautifulSoup
        soup = BeautifulSoup(text, 'html.parser')
        for tag in soup(['script', 'style', 'noscript']):
            tag.decompose()
        text = soup.get_text('\n', strip=True)
    return text, {}


@public_tool
def document_parse(source: FileInput, format: Literal['text', 'markdown', 'html', 'docx', 'pdf'],
                   offset: int = 0, limit: int = 12000) -> dict:
    """Extract document text without summarizing, rewriting or running OCR.

    Args:
        source: Explicit file bytes or owned resource_id.
        format: Source format; scanned PDFs report OCR_REQUIRED.
        offset: Character offset.
        limit: Character window size up to 100000.
    """
    text, metadata = parse_text(read_file(source), format)
    return {**text_window(text, offset, limit), 'metadata': metadata, 'format': format}


@public_tool
def resource_images_extract(source: FileInput, page: int = 1, regions: list[list[float]] | None = None) -> dict:
    """Extract embedded images or explicit rectangular regions from one PDF page.

    Args:
        source: PDF bytes or owned resource_id.
        page: One-based PDF page number.
        regions: Optional regions in the existing PDF extraction format.
    """
    from lazymind.chat.engine.tools.file_resources.pdf_material_images import extract_pdf_images
    with tempfile.TemporaryDirectory(prefix='lazymind-pdf-') as root:
        path = materialize(source, root, 'input.pdf')
        result = extract_pdf_images(path, Path(root) / 'images', page=page, regions=regions,
                                    source_name=FileInput.model_validate(source).name)
        files = [file_result(file.read_bytes(), file.name) for file in sorted((Path(root) / 'images').rglob('*.png'))]
        if sum(file['size_bytes'] for file in files) > 24 << 20:
            raise ToolError('RESOURCE_LIMIT', 'Extract fewer image regions per call.')
        return {'files': files, 'page': page, 'images': [
            {key: value for key, value in item.items() if key != 'local_path'} for item in result['images']
        ], 'extraction': {key: value for key, value in result.items()
                          if key in {'source_sha256', 'page_count', 'warnings', 'next_page', 'page_box_points'}}}
