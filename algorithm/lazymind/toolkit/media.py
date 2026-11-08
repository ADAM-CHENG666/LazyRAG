"""Image transformations from explicit bytes; generation remains an explicit model capability."""

import io
import tempfile
from pathlib import Path

from .contracts import FileInput, ToolError, file_result, materialize, public_tool, read_file


def open_image(source):
    from PIL import Image
    image = Image.open(io.BytesIO(read_file(source)))
    if image.width * image.height > 36_000_000 or getattr(image, 'n_frames', 1) > 300:
        image.close()
        raise ToolError('RESOURCE_LIMIT', 'Images may contain at most 36 megapixels and 300 frames.')
    return image


@public_tool
def image_inspect(source: FileInput) -> dict:
    """Inspect image format, dimensions and frames without a vision model.

    Args:
        source: Image bytes or owned immutable resource.
    """
    with open_image(source) as image:
        result = {'format': image.format, 'width': image.width, 'height': image.height,
                  'mode': image.mode, 'frames': getattr(image, 'n_frames', 1)}
        image.verify()
    return result


@public_tool
def image_caption(source: FileInput, caption: str, caption_box: list[float] | None = None,
                  text_color: str = '#FFFFFF', stroke_color: str = '#000000') -> dict:
    """Place exact supplied text on a static image or every GIF frame using the shared caption renderer.

    Args:
        source: Image or GIF bytes.
        caption: Exact text, never translated or generated.
        caption_box: Optional normalized left/top/right/bottom rectangle.
        text_color: Text color.
        stroke_color: Outline color.
    """
    from lazymind.chat.engine.tools.infra.image_workflow_support import render_caption_file
    with open_image(source) as image, tempfile.TemporaryDirectory(prefix='lazymind-caption-') as root:
        if image.width * image.height * getattr(image, 'n_frames', 1) > 60_000_000:
            raise ToolError('RESOURCE_LIMIT', 'Reduce GIF dimensions or frame count before captioning.')
        animated = getattr(image, 'n_frames', 1) > 1
        output = Path(root) / ('caption.gif' if animated else 'caption.png')
        try:
            layout = render_caption_file(image, output, caption, caption_box, text_color, stroke_color)
        except RuntimeError as exc:
            if 'font' not in str(exc).lower():
                raise
            raise ToolError('DEPENDENCY_UNAVAILABLE', 'Caption rendering requires a font covering the supplied text.',
                            recovery='Install a suitable font or configure LAZYMIND_MEME_FONT_PATH and retry.') from None
        return {'file': file_result(output.read_bytes(), output.name), 'caption': caption,
                'animated': animated, 'font_size': layout['font_size'], 'wrapped_text': layout['text']}


@public_tool
def image_compose(sources: list[FileInput], columns: int = 2, cell_width: int = 512,
                  cell_height: int = 512, background: str = '#FFFFFF') -> dict:
    """Compose a bounded contact sheet; images keep aspect ratio. No character/scene rules are implied.

    Args:
        sources: One to sixteen explicit image files.
        columns: Grid columns, 1..8.
        cell_width: Cell width, 32..2048.
        cell_height: Cell height, 32..2048.
        background: Pillow color for empty space.
    """
    from PIL import Image, ImageOps
    if not 1 <= len(sources) <= 16 or not 1 <= columns <= 8:
        raise ToolError('INVALID_ARGUMENT', 'Use 1..16 images and 1..8 columns.')
    if sum(len(read_file(source)) for source in sources) > 24 << 20:
        raise ToolError('RESOURCE_LIMIT', 'Combined file inputs exceed 24 MiB.')
    rows = (len(sources) + columns - 1) // columns
    if (not 32 <= cell_width <= 2048 or not 32 <= cell_height <= 2048
            or columns * rows * cell_width * cell_height > 36_000_000):
        raise ToolError('RESOURCE_LIMIT', 'Grid is too large; use smaller cells.')
    canvas = Image.new('RGB', (columns * cell_width, rows * cell_height), background)
    for index, source in enumerate(sources):
        with open_image(source) as image:
            tile = ImageOps.contain(image.convert('RGBA'), (cell_width, cell_height))
            x = (index % columns) * cell_width + (cell_width - tile.width) // 2
            y = (index // columns) * cell_height + (cell_height - tile.height) // 2
            canvas.paste(tile, (x, y), tile)
    output = io.BytesIO()
    canvas.save(output, format='PNG')
    return {'file': file_result(output.getvalue(), 'contact-sheet.png'), 'rows': rows, 'columns': columns}


@public_tool
def video_to_gif(source: FileInput, fps: int = 10, width: int = 480,
                 start: float = 0, duration: float = 5) -> dict:
    """Convert an explicit video file to GIF with the shared FFmpeg implementation.

    Args:
        source: Self-contained MP4/MOV bytes or owned resource; playlists are rejected.
        fps: Output frame rate, 1..30.
        width: Output width, 32..1280.
        start: Nonnegative start time in seconds.
        duration: Duration, greater than zero and at most 30 seconds.
    """
    from lazymind.chat.engine.tools.infra.video_generation_support import run_video_to_gif
    from lazymind.common.ffmpeg_deps import resolve_ffmpeg_binaries
    if not all(resolve_ffmpeg_binaries()):
        raise ToolError('DEPENDENCY_UNAVAILABLE', 'Video conversion requires FFmpeg and FFprobe.',
                        recovery='Configure FFmpeg in Settings → System tools, then retry.')
    content = read_file(source)
    if len(content) < 12 or content[4:8] != b'ftyp':
        raise ToolError('INVALID_ARGUMENT', 'Provide a self-contained MP4/MOV file, not a playlist or URL.')
    if not 1 <= fps <= 30 or not 32 <= width <= 1280 or start < 0 or not 0 < duration <= 30:
        raise ToolError('INVALID_ARGUMENT', 'Use fps 1..30, width 32..1280, start >= 0 and duration (0,30].')
    with tempfile.TemporaryDirectory(prefix='lazymind-gif-') as root:
        path = materialize(source, root, 'input.mp4')
        result = run_video_to_gif(str(path), fps=fps, width=width, start=start, duration=duration,
                                  output_path=str(Path(root) / 'video.gif'), input_format='mov')
        output = Path(result['local_path'])
        try:
            return {'file': file_result(output.read_bytes(), 'video.gif')}
        finally:
            output.unlink(missing_ok=True)


@public_tool
def image_search(query: str, count: int = 5) -> dict:
    """Search configured web providers for images and validate file readability; no semantic image review.

    Args:
        query: Search text.
        count: Maximum selected images, 1..5.
    """
    from lazyllm.tools.agent import ToolExecutionError
    from lazymind.chat.engine.tools.infra.image_workflow_support import (
        image_search_and_validate, require_image_search_configuration,
    )
    import json
    if not query.strip() or not 1 <= count <= 5:
        raise ToolError('INVALID_ARGUMENT', 'Provide a query and count 1..5.')
    try:
        require_image_search_configuration()
    except ToolExecutionError:
        raise ToolError('DEPENDENCY_UNAVAILABLE', 'Image search requires a configured web search provider.',
                        recovery='Configure web search in LazyMind Settings, '
                        'or use the host image-search capability.') from None
    result = image_search_and_validate(query=query, target_valid=count)
    return json.loads(result) if isinstance(result, str) else result
