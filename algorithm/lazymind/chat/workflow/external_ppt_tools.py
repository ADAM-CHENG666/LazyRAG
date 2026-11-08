"""Audited Workflow bindings to the common PPT implementation.

The package dispatcher pins these exported names; business logic lives in toolkit.
"""
from lazymind.toolkit.ppt_html import (  # noqa: F401
    ExternalToolError, ppt_export_html, ppt_inspect_pptx, ppt_render_html,
)
