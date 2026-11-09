"""Sections of an output file, shared by the gate and the revision tool."""

import re

PREAMBLE = "(начало)"
STATUS_CHOSEN_RE = re.compile(r"Статус: выбор сделан \[(DEC-[0-9]{4,})\], вариант \[(OPT-[0-9]{4,})\]")
STATUS_PRELIMINARY_RE = re.compile(r"Статус: предварительно, выбор не сделан, предложен вариант \[(OPT-[0-9]{4,})\]")


def is_status_line(line: str) -> bool:
    return bool(STATUS_CHOSEN_RE.fullmatch(line.strip()) or STATUS_PRELIMINARY_RE.fullmatch(line.strip()))


def split_sections(lines: list[str]) -> dict:
    """Heading of the second level -> list of (line number, line)."""
    sections, current, fenced = {}, None, False
    for number, line in enumerate(lines, 1):
        if line.lstrip().startswith("```"):
            fenced = not fenced
        if not fenced and line.startswith("## "):
            current = sections.setdefault(line[3:].strip(), [])
        elif not fenced and line.startswith("# "):
            current = None
        elif current is not None:
            current.append((number, line))
    return sections


def split_with_preamble(lines: list[str]) -> dict:
    """The sections of `split_sections`, preceded by the lines before the first second-level heading.

    The preamble holds the status line. It is named `(начало)` and listed only when it has text
    besides the title.
    """
    preamble, fenced = [], False
    for number, line in enumerate(lines, 1):
        if line.lstrip().startswith("```"):
            fenced = not fenced
        if not fenced and line.startswith("## "):
            break
        if fenced or not line.startswith("# "):
            preamble.append((number, line))
    sections = split_sections(lines)
    if any(line.strip() for _, line in preamble):
        return {PREAMBLE: preamble} | sections
    return sections
