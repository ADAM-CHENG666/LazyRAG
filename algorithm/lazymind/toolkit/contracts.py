"""Bounded file transport and actionable errors for shared tools."""

import base64
import functools
import hashlib
import io
import logging
import mimetypes
import zipfile
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, ValidationError


MAX_FILE_BYTES = 24 << 20
LOG = logging.getLogger(__name__)


class ToolError(ValueError):
    def __init__(self, code, message, *, details=None,  # noqa: B042
                 recovery='Correct the input and retry.', retryable=False):
        # This exception crosses transports only as its explicit error payload.
        super().__init__(message)
        self.error = {'code': code, 'message': message, 'details': details or {},
                      'recovery': recovery, 'retryable': retryable}


def public_tool(function):
    """Keep the same recoverable result for direct, Chat and MCP callers."""
    @functools.wraps(function)
    def wrapped(*args, **kwargs):
        try:
            return function(*args, **kwargs)
        except ToolError as exc:
            return {'ok': False, 'error': exc.error}
        except ValidationError as exc:
            fields = [{'path': list(item['loc']), 'message': item['msg']} for item in exc.errors()]
            return {'ok': False, 'error': ToolError(
                'INVALID_ARGUMENT', 'Input does not match the tool schema.', details={'fields': fields}).error}
        except ImportError:
            return {'ok': False, 'error': ToolError(
                'DEPENDENCY_UNAVAILABLE', 'A required tool dependency is not installed.',
                recovery='Install the algorithm service dependencies and retry.').error}
        except (ValueError, TypeError, KeyError, zipfile.BadZipFile) as exc:
            # These operations validate data locally. Do not expose provider exceptions or tracebacks.
            code = {'DEPENDENCY_MISSING': 'DEPENDENCY_UNAVAILABLE', 'OUTPUT_LIMIT': 'RESOURCE_LIMIT',
                    'TOOL_EXECUTION_FAILED': 'TOOL_EXECUTION_FAILED'}.get(getattr(exc, 'code', ''), 'INVALID_ARGUMENT')
            return {'ok': False, 'error': ToolError(code, str(exc)[:1000]).error}
        except Exception:
            LOG.exception('Shared tool failed: %s', function.__name__)
            return {'ok': False, 'error': ToolError(
                'TOOL_EXECUTION_FAILED', 'The tool could not complete this operation.',
                recovery='Check service logs and dependencies; retry with the same explicit inputs.',
                retryable=True).error}
    return wrapped


class FileInput(BaseModel):
    """Inline bytes, or an owned immutable resource resolved by Core before dispatch."""
    model_config = ConfigDict(extra='forbid')
    name: str = Field(default='input.bin', max_length=200)
    content_base64: str = Field(default='', max_length=(MAX_FILE_BYTES * 4 // 3 + 4))
    resource_id: str = Field(default='', max_length=128)
    mime_type: str = Field(default='', max_length=200)
    size_bytes: int | None = Field(default=None, ge=0, le=MAX_FILE_BYTES)
    sha256: str = Field(default='', max_length=64)


def read_file(value):
    item = FileInput.model_validate(value)
    if item.resource_id:
        raise ToolError('RESOURCE_NOT_RESOLVED', 'Resolve resource_id through the authenticated Core tool gateway.',
                        recovery='Call tool.call with this FileInput; Core checks ownership before reading it.')
    if not item.content_base64:
        raise ToolError('INVALID_ARGUMENT', 'FileInput.content_base64 or resource_id is required.')
    try:
        content = base64.b64decode(item.content_base64, validate=True)
    except ValueError:
        raise ToolError('INVALID_ARGUMENT', 'FileInput.content_base64 is not valid base64.') from None
    if item.size_bytes is not None and item.size_bytes != len(content):
        raise ToolError('INVALID_ARGUMENT', 'FileInput.size_bytes does not match the file.')
    if len(content) > MAX_FILE_BYTES:
        raise ToolError('RESOURCE_LIMIT', 'Input file exceeds 24 MiB.')
    digest = hashlib.sha256(content).hexdigest()
    if item.sha256 and item.sha256 != digest:
        raise ToolError('VERSION_CONFLICT', 'File SHA-256 does not match the supplied bytes.',
                        details={'actual_sha256': digest}, recovery='Read the current file and recompute the edit.')
    return content


def file_result(content, name, mime_type=None):
    if len(content) > MAX_FILE_BYTES:
        raise ToolError('RESOURCE_LIMIT', 'Output file exceeds 24 MiB; reduce the output size.')
    return {'name': Path(name).name, 'mime_type': mime_type or mimetypes.guess_type(name)[0]
            or 'application/octet-stream', 'content_base64': base64.b64encode(content).decode('ascii'),
            'size_bytes': len(content), 'sha256': hashlib.sha256(content).hexdigest()}


def materialize(value, root, name='input.bin'):
    content = read_file(value)
    path = Path(root) / name
    path.write_bytes(content)
    return path


def check_office(content):
    """Bound decompression and prohibit linked resources before parsing/rendering Office files."""
    from defusedxml import ElementTree
    with zipfile.ZipFile(io.BytesIO(content)) as archive:
        infos = archive.infolist()
        if len(infos) > 10000 or sum(item.file_size for item in infos) > 128 << 20:
            raise ToolError('RESOURCE_LIMIT', 'Office archive expands beyond the tool limit.')
        for item in infos:
            if item.filename.endswith('.rels'):
                for rel in ElementTree.fromstring(archive.read(item)):
                    if rel.get('TargetMode') == 'External' and not rel.get('Type', '').endswith('/hyperlink'):
                        raise ToolError('INVALID_ARGUMENT', 'Linked Office resources must be embedded before upload.')
    return content


def text_window(text, offset=0, limit=12000):
    if type(offset) is not int or type(limit) is not int or offset < 0 or not 1 <= limit <= 100000:
        raise ToolError('INVALID_ARGUMENT', 'offset must be nonnegative; limit must be 1..100000.')
    end = min(len(text), offset + limit)
    return {'text': text[offset:end], 'offset': offset, 'total_characters': len(text),
            'next_offset': end if end < len(text) else None}
