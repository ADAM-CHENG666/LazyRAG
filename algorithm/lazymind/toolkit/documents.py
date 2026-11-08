"""Provider-neutral document conversion and deterministic, versioned edits."""

import hashlib
import json
from typing import Literal

from .contracts import ToolError, file_result, public_tool


def document_hash(value):
    text = (value if isinstance(value, str) else
            json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':')))
    return hashlib.sha256(text.encode('utf-8')).hexdigest()


def as_ir(content, source_format, document_id):
    from lazyllm.tools.writer.data_models import WriterDocument
    from lazyllm.tools.writer.utils import writer_document_from_lmd
    from lazymind.document_tools.artifacts import markdown_to_writer_document
    if source_format == 'markdown':
        return markdown_to_writer_document(content, document_id=document_id)
    if source_format == 'lmd':
        return writer_document_from_lmd(content)
    return WriterDocument.model_validate(content)


@public_tool
def document_convert(content: str | dict, source_format: Literal['markdown', 'lmd', 'writer_ir'],
                     target_format: Literal['markdown', 'lmd', 'writer_ir'], document_id: str = 'document') -> dict:
    """Convert Markdown, LMD and Writer IR using the shared Writer converters.

    Args:
        content: Source text or Writer IR object.
        source_format: Explicit source format.
        target_format: Desired format; does not summarize or rewrite.
        document_id: Identifier when converting Markdown to structured form.
    """
    from lazyllm.tools.writer.utils import writer_document_to_lmd, writer_document_to_markdown
    document = as_ir(content, source_format, document_id)
    result = (document.model_dump() if target_format == 'writer_ir' else
              writer_document_to_lmd(document) if target_format == 'lmd' else writer_document_to_markdown(document))
    return {'content': result, 'format': target_format, 'sha256': document_hash(result)}


@public_tool
def document_export(content: str | dict, source_format: Literal['markdown', 'lmd', 'writer_ir'] = 'markdown',
                    output_format: Literal['markdown', 'text', 'latex', 'lmd', 'writer_ir', 'docx'] = 'markdown',
                    document_id: str = 'document') -> dict:
    """Export the Writer engine's supported formats as downloadable bytes without cloud writes.

    Args:
        content: Source document.
        source_format: Markdown, LMD or Writer IR.
        output_format: Supported deterministic Writer export format.
        document_id: Identifier for Markdown conversion.
    """
    from lazyllm.tools.writer.provider.base import WriterProviderBase
    from lazyllm.tools.writer.utils import writer_document_to_lmd
    document = as_ir(content, source_format, document_id)
    if output_format == 'docx':
        from lazyllm.tools.writer.utils import writer_document_to_markdown
        from .docx_export import render_docx
        markdown = content if source_format == 'markdown' else writer_document_to_markdown(document)
        return {'file': file_result(render_docx(markdown), 'document.docx'), 'format': 'docx'}
    if output_format == 'lmd':
        result = writer_document_to_lmd(document)
    elif output_format == 'writer_ir':
        result = document.model_dump_json(indent=2)
    else:
        result = WriterProviderBase.convert_common_document(
            content if source_format == 'markdown' else document, output_format=output_format).content
    extension = {'markdown': 'md', 'text': 'txt', 'latex': 'tex', 'lmd': 'lmd', 'writer_ir': 'json'}[output_format]
    return {'file': file_result(result.encode('utf-8'), f'document.{extension}'), 'format': output_format}


def apply_document_patch(content, patch, kind, source_sha256):
    from lazyllm.tools.writer.data_models import WriterDocument
    from lazyllm.tools.writer.data_models.revision import PatchSet, StringReplaceSet
    from lazyllm.tools.writer.tools.revision_tools import WriterRevisionTools, apply_patch_to_ir
    actual = document_hash(content)
    if source_sha256 != actual:
        raise ToolError('VERSION_CONFLICT', 'Document changed or source_sha256 is incorrect.',
                        details={'actual_sha256': actual}, recovery='Read the current document and recompute the patch.')
    if kind == 'writer_ir':
        document, result = apply_patch_to_ir(WriterDocument.model_validate(content), PatchSet.model_validate(patch))
        return document.model_dump(), result.model_dump()
    if not isinstance(content, str):
        raise ToolError('INVALID_ARGUMENT', 'Markdown replacements require text input.')
    replacements = StringReplaceSet.model_validate(patch)
    if not replacements.replacements:
        raise ToolError('INVALID_ARGUMENT', 'The replacement set is empty.')
    tools = WriterRevisionTools(llm=None)
    for replacement in replacements.replacements:
        reference = replacement.content_ref
        start, end = ((0, len(content)) if reference is None or reference.document_root
                      else tools._markdown_section_range(content, reference))
        if content[start:end].count(replacement.old_string) > 1:
            raise ToolError('INVALID_ARGUMENT', 'Replacement target is ambiguous within its selected scope.',
                            recovery='Provide a unique old_string with more context or a narrower content_ref.')
        content = tools._apply_markdown_replacement(content, replacement)
    return content, {'applied_replacements': len(replacements.replacements)}


@public_tool
def document_patch_validate(content: str | dict, patch: dict, kind: Literal['markdown', 'writer_ir'],
                            source_sha256: str) -> dict:
    """Dry-run a patch on a copy; validate structure, exact targets and version, without semantic review.

    Args:
        content: Current document; never modified in storage.
        patch: Writer PatchSet or StringReplaceSet object for the selected kind.
        kind: writer_ir patches or markdown exact replacements.
        source_sha256: SHA-256 from document.convert/resource.read; objects use canonical sorted JSON.
    """
    _, result = apply_document_patch(content, patch, kind, source_sha256)
    return {'valid': True, 'validation': 'structure_targets_and_version', 'result': result}


@public_tool
def document_patch_apply(content: str | dict, patch: dict, kind: Literal['markdown', 'writer_ir'],
                         source_sha256: str) -> dict:
    """Apply a version-checked patch and return new content; never publish or write to a provider.

    Args:
        content: Current document.
        patch: Writer PatchSet or StringReplaceSet object.
        kind: Format and patch variant.
        source_sha256: Expected current document hash.
    """
    revised, result = apply_document_patch(content, patch, kind, source_sha256)
    return {'content': revised, 'sha256': document_hash(revised), 'result': result}
