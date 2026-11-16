"""A small renderer of the constrained Markdown of case outputs, used by the case page.

Recognized: headings, paragraphs, `-` and `1.` lists, pipe tables, record links `[CLM-0001]` and
`[CLM-0001, CALC-0002]`, the marks `(допущение)`, `(синтетика)`, `[бриф]`. Everything else, including
`[text](url)`, raw HTML, emphasis marks and fenced code, is escaped and shown as text. Nothing in
the output is built from case text except escaped text, so no tag, link or script can come from it.
"""

import html
import re
from typing import Callable

from validate_project import ID_RE

Footnote = Callable[[str], "str | None"]

HEADING_RE = re.compile(r"(#{1,6})[ \t]+(.*?)[ \t]*")
BULLET_RE = re.compile(r"-[ \t]+(.*)")
NUMBERED_RE = re.compile(r"[0-9]+\.[ \t]+(.*)")
IDS = rf"(?:{ID_RE.pattern})(?:[ \t]*,[ \t]*(?:{ID_RE.pattern}))*"
INLINE_RE = re.compile(rf"\[({IDS})\]|\((допущение)\)|\((синтетика)\)|(\[бриф\])")
MARKS = {
    "допущение": ("mark mark-assumption", "допущение"),
    "синтетика": ("mark mark-synthetic", "синтетика"),
    "бриф": ("mark mark-brief", "слова руководителя"),
}


def escape(text: str) -> str:
    return html.escape(text, quote=True)


def mark(name: str) -> str:
    css, label = MARKS[name]
    return f'<span class="{css}">{label}</span>'


def render_inline(text: str, footnote: Footnote) -> str:
    """One line of text as HTML: the recognized tokens become markup, the rest is escaped."""
    out, position = [], 0
    for match in INLINE_RE.finditer(text):
        out.append(escape(text[position:match.start()]))
        position = match.end()
        if match.group(1):
            ids = [part.strip() for part in match.group(1).split(",")]
            links = [footnote(record_id) for record_id in ids]
            if all(link is None for link in links):
                out.append(escape(match.group(0)))
            else:
                parts = [link if link is not None else f'<span class="ref">{escape(record_id)}</span>'
                         for record_id, link in zip(ids, links)]
                out.append('<span class="refs">' + " ".join(parts) + "</span>")
        elif match.group(2):
            out.append(mark("допущение"))
        elif match.group(3):
            out.append(mark("синтетика"))
        else:
            out.append(mark("бриф"))
    out.append(escape(text[position:]))
    return "".join(out)


def split_row(line: str) -> list[str]:
    return [cell.strip() for cell in line.strip().strip("|").split("|")]


def is_separator(cells: list[str]) -> bool:
    return all(cell and set(cell) <= set(":- ") for cell in cells)


def render_table(rows: list[str], footnote: Footnote) -> str:
    cells = [split_row(row) for row in rows]
    header, body = cells[0], [row for row in cells[1:] if not is_separator(row)]
    head = "".join(f"<th>{render_inline(cell, footnote)}</th>" for cell in header)
    lines = []
    for row in body:
        lines.append("<tr>" + "".join(
            f'<td data-label="{escape(header[i]) if i < len(header) else ""}">{render_inline(cell, footnote)}</td>'
            for i, cell in enumerate(row)) + "</tr>")
    return (f'<div class="table-wrap"><table><thead><tr>{head}</tr></thead>'
            f'<tbody>{"".join(lines)}</tbody></table></div>')


def render(lines: list[str], footnote: Footnote, heading_offset: int = 2) -> str:
    """HTML of the lines. Headings are lowered by `heading_offset` levels, down to h6."""
    out, paragraph = [], []

    def flush():
        if paragraph:
            out.append("<p>" + render_inline(" ".join(paragraph), footnote) + "</p>")
            paragraph.clear()

    index = 0
    while index < len(lines):
        line = lines[index].rstrip()
        stripped = line.strip()
        heading = HEADING_RE.fullmatch(stripped) if not line.startswith((" ", "\t")) else None
        bullet = BULLET_RE.fullmatch(stripped)
        numbered = NUMBERED_RE.fullmatch(stripped)
        if not stripped:
            flush()
        elif heading:
            flush()
            level = min(len(heading.group(1)) + heading_offset, 6)
            out.append(f"<h{level}>{render_inline(heading.group(2), footnote)}</h{level}>")
        elif stripped.startswith("|"):
            flush()
            rows = []
            while index < len(lines) and lines[index].strip().startswith("|"):
                rows.append(lines[index])
                index += 1
            out.append(render_table(rows, footnote))
            continue
        elif bullet or numbered:
            flush()
            pattern, tag = (BULLET_RE, "ul") if bullet else (NUMBERED_RE, "ol")
            items = []
            while index < len(lines):
                found = pattern.fullmatch(lines[index].strip())
                if not found:
                    break
                items.append(f"<li>{render_inline(found.group(1), footnote)}</li>")
                index += 1
            out.append(f"<{tag}>{''.join(items)}</{tag}>")
            continue
        else:
            paragraph.append(stripped)
        index += 1
    flush()
    return "\n".join(out)
