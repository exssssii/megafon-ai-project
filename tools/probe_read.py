"""Read one local input within the declared limits and print located text.

This is the environment probe from task B02: it confirms that the pinned parsers
work in Claude Code and Codex. Full extraction and the source registry belong to B04.

Exit codes: 0 read, 2 unreadable or unsupported, 3 over a limit.
The first output line is a JSON report; "complete": false means only part was shown.
"""
import argparse
from contextlib import closing
import csv
import io
import json
import re
import sys
from pathlib import Path

LIMITS_FILE = Path(__file__).with_name("input_limits.json")
EXIT_OK, EXIT_UNREADABLE, EXIT_OVER_LIMIT = 0, 2, 3


class OverLimit(Exception):
    pass


class Unreadable(Exception):
    pass


def check(name: str, value: int, limit: int) -> None:
    if value > limit:
        raise OverLimit(f"{name}: {value} > {limit}")


def read_text(path: Path, limits: dict) -> tuple[dict, list[str]]:
    try:
        text = path.read_text(encoding="utf-8-sig")
    except UnicodeDecodeError as error:
        raise Unreadable(f"not UTF-8: {error.reason}") from error
    check("chars", len(text), limits["max_text_chars"])
    lines = text.splitlines()
    return {"lines": len(lines)}, [f"[L{n}] {line}" for n, line in enumerate(lines, 1) if line]


def read_docx(path: Path, limits: dict) -> tuple[dict, list[str]]:
    from docx import Document

    document = Document(path)
    lines = [f"[P{i}] {p.text}" for i, p in enumerate(document.paragraphs, 1) if p.text]
    for t, table in enumerate(document.tables, 1):
        for r, row in enumerate(table.rows, 1):
            lines.append(f"[T{t}R{r}] " + " | ".join(cell.text for cell in row.cells))
    check("chars", sum(map(len, lines)), limits["max_text_chars"])
    return {"paragraphs": len(document.paragraphs), "tables": len(document.tables)}, lines


def read_pdf(path: Path, limits: dict) -> tuple[dict, list[str]]:
    from pypdf import PdfReader

    reader = PdfReader(path)
    if reader.is_encrypted:
        raise Unreadable("encrypted PDF")
    check("pages", len(reader.pages), limits["max_pdf_pages"])
    lines, empty = [], 0
    for number, page in enumerate(reader.pages, 1):
        text = (page.extract_text() or "").strip()
        if text:
            lines.append(f"[p.{number}] {text}")
        else:
            empty += 1
    if not lines:
        raise Unreadable("no extractable text; scans are not supported")
    return {"pages": len(reader.pages), "pages_without_text": empty}, lines


def decode_table(raw: bytes) -> tuple[str, str, bool]:
    if raw.startswith((b"\xff\xfe", b"\xfe\xff")):
        return raw.decode("utf-16"), "utf-16", False
    if b"\x00" in raw:
        raise Unreadable("binary content in a text table")
    try:
        return raw.decode("utf-8-sig"), "utf-8", False
    except UnicodeDecodeError:
        return raw.decode("cp1251"), "cp1251", True


def detect_delimiter(text: str) -> tuple[str, bool]:
    header = re.sub(r'"[^"]*"', "", text.split("\n", 1)[0])
    counts = {delimiter: header.count(delimiter) for delimiter in ";,\t"}
    return max(counts, key=counts.get), sum(map(bool, counts.values())) != 1


def read_csv(path: Path, limits: dict) -> tuple[dict, list[str]]:
    try:
        text, encoding, encoding_assumed = decode_table(path.read_bytes())
    except UnicodeDecodeError as error:
        raise Unreadable(f"unknown encoding: {error.reason}") from error
    delimiter, delimiter_assumed = detect_delimiter(text)
    reader = csv.reader(io.StringIO(text, newline=""), delimiter=delimiter, strict=True)
    rows = [row for row in reader if row]
    check("rows", len(rows), limits["max_table_rows"])
    check("columns", max(map(len, rows), default=0), limits["max_table_columns"])
    check("cells", sum(map(len, rows)), limits["max_table_cells"])
    lines = [f"[R{number}] " + " | ".join(row) for number, row in enumerate(rows, 1)]
    return {
        "encoding": encoding,
        "encoding_assumed": encoding_assumed,
        "delimiter": delimiter,
        "delimiter_assumed": delimiter_assumed,
        "rows": len(rows),
        "empty_cells": sum(cell == "" for row in rows for cell in row),
    }, lines


def read_xlsx(path: Path, limits: dict) -> tuple[dict, list[str]]:
    from openpyxl import load_workbook

    with closing(load_workbook(path, data_only=True, read_only=True)) as values:
        with closing(load_workbook(path, data_only=False, read_only=True)) as formulas:
            return scan_workbook(values, formulas, limits)


def scan_workbook(values, formulas, limits: dict) -> tuple[dict, list[str]]:
    from openpyxl.utils import get_column_letter

    lines, missing, sheets = [], [], []
    cells = 0
    for sheet in values.worksheets:
        origin_sheet = formulas[sheet.title]
        sheet.reset_dimensions()
        origin_sheet.reset_dimensions()
        pairs = zip(sheet.iter_rows(values_only=True), origin_sheet.iter_rows(values_only=True))
        count = 0
        for r, (row, source) in enumerate(pairs, 1):
            count += 1
            check("rows", count, limits["max_table_rows"])
            check("columns", len(row), limits["max_table_columns"])
            cells += len(row)
            check("cells", cells, limits["max_table_cells"])
            for c, (value, origin) in enumerate(zip(row, source), 1):
                cell = f"{sheet.title}!{get_column_letter(c)}{r}"
                if value is None and origin is not None:
                    missing.append(cell)
                    lines.append(f"[{cell}] <formula without cached value>")
                elif value is not None:
                    lines.append(f"[{cell}] {value}")
        sheets.append({"name": sheet.title, "rows": count})
    return {"sheets": sheets, "formulas_without_value": missing}, lines


READERS = {
    ".md": read_text,
    ".txt": read_text,
    ".docx": read_docx,
    ".pdf": read_pdf,
    ".csv": read_csv,
    ".xlsx": read_xlsx,
}


def probe(path: Path, limits: dict) -> tuple[int, dict, list[str]]:
    report = {"path": path.name, "status": "ok", "complete": False}
    if not path.is_file():
        return EXIT_UNREADABLE, report | {"status": "unreadable", "reason": "file not found"}, []
    report["bytes"] = path.stat().st_size
    reader = READERS.get(path.suffix.lower())
    if reader is None:
        return EXIT_UNREADABLE, report | {"status": "unsupported", "reason": path.suffix}, []
    if report["bytes"] > limits["max_file_bytes"]:
        reason = f"bytes: {report['bytes']} > {limits['max_file_bytes']}"
        return EXIT_OVER_LIMIT, report | {"status": "over_limit", "reason": reason}, []
    try:
        meta, lines = reader(path, limits)
    except OverLimit as error:
        return EXIT_OVER_LIMIT, report | {"status": "over_limit", "reason": str(error)}, []
    except Unreadable as error:
        return EXIT_UNREADABLE, report | {"status": "unreadable", "reason": str(error)}, []
    except Exception as error:
        reason = f"{type(error).__name__}: {error}"
        return EXIT_UNREADABLE, report | {"status": "unreadable", "reason": reason}, []
    if not lines:
        return EXIT_UNREADABLE, report | meta | {"status": "empty", "reason": "no content"}, []
    shown, used = [], 0
    for line in lines:
        used += len(line) + 1
        if used > limits["max_output_chars"]:
            break
        shown.append(line)
    report |= meta | {"complete": len(shown) == len(lines) and not meta.get("pages_without_text")}
    report |= {"lines_total": len(lines), "lines_shown": len(shown)}
    if not report["complete"]:
        report["status"] = "partial"
    return EXIT_OK, report, shown


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("path", type=Path)
    args = parser.parse_args()
    sys.stdout.reconfigure(encoding="utf-8")
    limits = json.loads(LIMITS_FILE.read_text(encoding="utf-8"))
    code, report, lines = probe(args.path, limits)
    print(json.dumps(report, ensure_ascii=False))
    if lines:
        print("\n".join(lines))
    return code


if __name__ == "__main__":
    raise SystemExit(main())
