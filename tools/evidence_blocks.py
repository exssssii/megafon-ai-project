"""Match contract locators to extracted blocks and quotes to block text.

Shared by the case validator and tools/evidence.py so that both answer the same way.
Nothing here prints: callers must keep material text out of their reports.
"""
import json
import re
from pathlib import Path

CELL_RE = re.compile(r"([A-Z]{1,3})([0-9]+)")
RANGE_RE = re.compile(r"([A-Z]{1,3})([0-9]+)(?::([A-Z]{1,3})([0-9]+))?")
DIGIT = re.compile(r"[0-9]")
SEPARATORS = ".,"


def load_blocks(path: Path) -> list[dict]:
    """Read an extraction file; ValueError when it is not valid UTF-8 JSON lines of blocks."""
    try:
        text = path.read_bytes().decode("utf-8")
    except UnicodeDecodeError as error:
        raise ValueError("not valid UTF-8") from error
    blocks = []
    for line in text.split("\n"):
        if not line.strip():
            continue
        try:
            block = json.loads(line)
        except (ValueError, RecursionError) as error:
            raise ValueError("not valid JSON") from error
        if (not isinstance(block, dict) or not isinstance(block.get("n"), int)
                or isinstance(block.get("n"), bool) or not isinstance(block.get("locator"), dict)
                or not isinstance(block.get("text"), str)):
            raise ValueError("line is not a block")
        blocks.append(block)
    return blocks


def _column_number(letters: str) -> int:
    number = 0
    for letter in letters:
        number = number * 26 + ord(letter) - 64
    return number


def parse_range(text) -> tuple[int, int, int, int] | None:
    """Return (first column, first row, last column, last row) or None when it cannot be read."""
    if not isinstance(text, str):
        return None
    found = RANGE_RE.fullmatch(text)
    if not found:
        return None
    c1, r1 = _column_number(found[1]), int(found[2])
    c2, r2 = (_column_number(found[3]), int(found[4])) if found[3] else (c1, r1)
    return min(c1, c2), min(r1, r2), max(c1, c2), max(r1, r2)


def _number(value):
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _matches(block: dict, locator: dict) -> bool:
    own = block["locator"]
    kind = locator["kind"]
    if kind == "section":
        return isinstance(block.get("heading"), str) and block["heading"] == locator["heading"]
    if own.get("kind") != kind:
        return False
    if kind == "paragraph":
        return _number(own.get("index")) == locator["index"]
    if kind == "page":
        return _number(own.get("page")) == locator["page"]
    if kind == "doc_table":
        return _number(own.get("table")) == locator["table"] and _number(own.get("row")) == locator["row"]
    if kind == "rows":
        row = _number(own.get("from"))
        return row is not None and locator["from"] <= row <= locator["to"]
    if kind == "cells":
        if own.get("sheet") != locator["sheet"]:
            return False
        wanted, actual = parse_range(locator["range"]), parse_range(own.get("range"))
        if wanted is None or actual is None:
            return False
        return wanted[0] <= actual[2] and actual[0] <= wanted[2] and wanted[1] <= actual[3] and actual[1] <= wanted[3]
    return False


def matching_blocks(blocks: list[dict], locator: dict) -> list[dict]:
    """Blocks the locator points to; empty when it points nowhere."""
    return [block for block in blocks if _matches(block, locator)]


def collapse(text: str) -> str:
    """Runs of whitespace of any kind, non-breaking spaces and line breaks included, become one space."""
    return " ".join(text.split())


LEFT_NUMBER = re.compile(r"[.,]?[0-9]")
RIGHT_NUMBER = re.compile(r".*[0-9][.,]?", re.S)


def _digit(text: str, index: int) -> bool:
    return 0 <= index < len(text) and DIGIT.fullmatch(text[index]) is not None


def _group_before(text: str, end: int) -> int:
    """Length of the run of digits that ends at `end` (exclusive)."""
    start = end
    while _digit(text, start - 1):
        start -= 1
    return end - start


def _group_after(text: str, start: int) -> int:
    end = start
    while _digit(text, end):
        end += 1
    return end - start


def _left_touches(text: str, start: int, needle: str) -> bool:
    if needle[0] in SEPARATORS:
        return _digit(text, start - 1)
    if _digit(text, start - 1):
        return True
    if start > 0 and text[start - 1] in SEPARATORS and _digit(text, start - 2):
        return True
    if start > 1 and text[start - 1] == " " and _digit(text, start - 2):
        first_run = len(re.match(r"[0-9]+", needle)[0])
        return first_run == 3 and 1 <= _group_before(text, start - 1) <= 3
    return False


def _right_touches(text: str, end: int, needle: str) -> bool:
    if needle[-1] in SEPARATORS:
        return _digit(text, end)
    if _digit(text, end):
        return True
    if end < len(text) and text[end] in SEPARATORS and _digit(text, end + 1):
        return True
    if end + 1 < len(text) and text[end] == " " and _digit(text, end + 1):
        last_run = len(re.search(r"[0-9]+$", needle)[0])
        return 1 <= last_run <= 3 and _group_after(text, end + 1) == 3
    return False


def quote_in(quote: str, text: str) -> bool:
    """Case-sensitive containment after whitespace collapse.

    A quote that begins or ends with a number does not match inside a longer one: not in `1140`,
    `0.140`, `140,5`, nor in a number grouped by spaces (`1 140`, `140 000`).
    """
    needle, haystack = collapse(quote), collapse(text)
    if not needle:
        return False
    left, right = LEFT_NUMBER.match(needle) is not None, RIGHT_NUMBER.fullmatch(needle) is not None
    start = haystack.find(needle)
    while start != -1:
        end = start + len(needle)
        if not ((left and _left_touches(haystack, start, needle)) or (right and _right_touches(haystack, end, needle))):
            return True
        start = haystack.find(needle, start + 1)
    return False
