"""Generic Markdown-to-DOCX rendering, without a workflow's cover, styles or domain rules."""

import io

from .contracts import ToolError


def render_docx(markdown):
    from docx import Document
    from markdown_it import MarkdownIt
    document = Document()
    tokens = MarkdownIt('commonmark').enable('table').parse(markdown)
    paragraph = None
    table = None
    cells = []
    list_style = []
    for token in tokens:
        kind = token.type
        if kind == 'heading_open':
            paragraph = document.add_heading('', level=int(token.tag[1:]))
        elif kind in {'bullet_list_open', 'ordered_list_open'}:
            list_style.append('List Bullet' if kind == 'bullet_list_open' else 'List Number')
        elif kind in {'bullet_list_close', 'ordered_list_close'}:
            list_style.pop()
        elif kind == 'paragraph_open':
            paragraph = document.add_paragraph(style=list_style[-1] if list_style else None)
        elif kind in {'fence', 'code_block'}:
            paragraph = document.add_paragraph()
            paragraph.add_run(token.content).font.name = 'Courier New'
        elif kind == 'hr':
            document.add_paragraph('────────')
        elif kind == 'table_open':
            table = document.add_table(rows=0, cols=0)
            table.style = 'Table Grid'
        elif kind == 'tr_open':
            cells = []
        elif kind in {'th_open', 'td_open'}:
            if len(table.rows) == 0:
                from docx.shared import Inches
                table.add_column(Inches(1))
            cells.append('')
        elif kind == 'tr_close':
            row = table.add_row()
            for cell, text in zip(row.cells, cells):
                cell.text = text
        elif kind == 'table_close':
            table = None
        elif kind == 'inline':
            if table is not None:
                if any(child.type in {'image', 'html_inline'} for child in token.children or []):
                    raise ToolError('INVALID_ARGUMENT',
                                    'DOCX table cells currently support text, not inline images/HTML.')
                cells[-1] = ''.join(child.content for child in token.children or []
                                    if child.type in {'text', 'code_inline', 'softbreak', 'hardbreak'})
                continue
            bold, italic, link = False, False, ''
            for child in token.children or []:
                if child.type in {'image', 'html_inline'}:
                    raise ToolError('INVALID_ARGUMENT', 'Embed images through a presentation tool or export Markdown; '
                                    'this DOCX renderer does not fetch image URLs or render raw HTML.')
                if child.type in {'strong_open', 'strong_close'}:
                    bold = child.type == 'strong_open'
                elif child.type in {'em_open', 'em_close'}:
                    italic = child.type == 'em_open'
                elif child.type == 'link_open':
                    link = child.attrGet('href') or ''
                elif child.type == 'link_close':
                    paragraph.add_run(f' ({link})')
                else:
                    text = '\n' if child.type in {'softbreak', 'hardbreak'} else child.content
                    run = paragraph.add_run(text)
                    run.bold, run.italic = bold, italic
                    if child.type == 'code_inline':
                        run.font.name = 'Courier New'
        elif kind == 'html_block':
            raise ToolError('INVALID_ARGUMENT', 'Convert raw HTML to Markdown before DOCX export.')
    output = io.BytesIO()
    document.save(output)
    return output.getvalue()
