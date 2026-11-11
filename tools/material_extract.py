"""Turn one local material into located text blocks without running anything inside it.

Every block carries a locator of the source contract, so a fragment can be cited as is.
Reading problems become a status and a note: nothing is cut or guessed silently.
"""
import codecs
from contextlib import closing
import csv
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
import io
from itertools import islice
import json
import logging
from pathlib import Path
import re

from image_header import Animated, NotImage, read_header

LIMITS_FILE = Path(__file__).with_name("input_limits.json")
SUPPORTED = {
    ".md": "md",
    ".txt": "txt",
    ".docx": "docx",
    ".pdf": "pdf",
    ".csv": "csv",
    ".xlsx": "xlsx",
    ".png": "image",
    ".jpg": "image",
    ".jpeg": "image",
    ".webp": "image",
}
IMAGE_FORMATS = {".png": "png", ".jpg": "jpeg", ".jpeg": "jpeg", ".webp": "webp"}
TALL_IMAGE_RATIO = 2.5
TALL_IMAGE_NOTE = "Изображение очень высокое: детали могут потеряться, лучше фрагменты."
TEXT_STATUSES = {"ok", "partial"}
FORMULA_NO_VALUE = "<нет значения формулы>"
NUMBER = re.compile(r"[+-]?\d[\d  ]*(?:[.,]\d+)?%?")
DATE = re.compile(r"\d{4}-\d{2}-\d{2}(?:[ T].*)?|\d{2}[./]\d{2}[./]\d{4}")
WORD = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
DELIMITER_SAMPLE_ROWS = 50


class OverLimit(Exception):
    pass


class Unreadable(Exception):
    pass


class Scan(Exception):
    pass


class BadOption(Exception):
    """The request cannot apply to this file; nothing should be written."""


@dataclass
class Options:
    encoding: str | None = None
    delimiter: str | None = None
    pages: tuple[int, int] | None = None
    sheets: list[str] | None = None

    def rereads(self) -> bool:
        return any(value is not None for value in vars(self).values())


@dataclass
class Extraction:
    status: str
    blocks: list[dict] = field(default_factory=list)
    report: dict = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)
    summary: str | None = None

    @property
    def complete(self) -> bool:
        return self.status == "ok"

    def limits_text(self) -> str:
        if self.summary is not None:
            return " ".join([self.summary, *self.notes])
        lead = "Прочитан в границах формата." if self.complete else "Прочитан не полностью."
        if self.status not in TEXT_STATUSES:
            lead = "Текст не извлечён."
        return " ".join([lead, *self.notes])


def load_limits() -> dict:
    return json.loads(LIMITS_FILE.read_text(encoding="utf-8"))


def check(name: str, value: int, limit: int) -> None:
    if value > limit:
        raise OverLimit(f"{name}: {value} > {limit}")


def text_blocks(text: str, markdown: bool) -> list[dict]:
    """Split text into paragraphs separated by blank lines; headings come from # lines."""
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    blocks, heading = [], None
    for chunk in re.split(r"\n(?:[ \t]*\n)+", text):
        chunk = chunk.strip()
        if not chunk:
            continue
        if markdown:
            for line in chunk.split("\n"):
                found = re.match(r"#{1,6}[ \t]+(.+?)[ \t#]*$", line)
                if found:
                    heading = found.group(1)
        block = {"locator": {"kind": "paragraph", "index": len(blocks) + 1}, "text": chunk}
        if heading:
            block["heading"] = heading
        blocks.append(block)
    return blocks


def decode(raw: bytes, encoding: str) -> str:
    """Decode strictly; a UTF-8 byte order mark never becomes part of the text."""
    name = "utf-8-sig" if codecs.lookup(encoding).name == "utf-8" else encoding
    try:
        return raw.decode(name)
    except UnicodeDecodeError as error:
        raise Unreadable(f"не удалось декодировать как {encoding}") from error


def read_text(path: Path, kind: str, limits: dict, options: Options) -> Extraction:
    text = decode(path.read_bytes(), options.encoding or "utf-8")
    if "\x00" in text:
        raise Unreadable("в тексте есть нулевые символы, кодировка выбрана неверно")
    check("chars", len(text), limits["max_text_chars"])
    blocks = text_blocks(text, kind == "md")
    report = {"encoding": options.encoding or "utf-8", "paragraphs": len(blocks)}
    return Extraction("ok", blocks, report, [f"Абзацев: {len(blocks)}."])


def body_items(parent, tags: tuple[str, ...]):
    """Yield child elements with the given tags, looking inside content controls."""
    for child in parent:
        if child.tag == WORD + "sdt":
            content = child.find(WORD + "sdtContent")
            if content is not None:
                yield from body_items(content, tags)
        elif child.tag in tags:
            yield child


def paragraph_text(element) -> str:
    """Collect visible text: inserted runs and field results count, deleted text does not."""
    parts = []

    def walk(node):
        for child in node:
            if child.tag in (WORD + "del", WORD + "moveFrom", WORD + "txbxContent"):
                continue
            if child.tag == WORD + "t":
                parts.append(child.text or "")
            elif child.tag == WORD + "tab":
                parts.append("\t")
            elif child.tag in (WORD + "br", WORD + "cr"):
                parts.append("\n")
            elif child.tag == WORD + "noBreakHyphen":
                parts.append("-")
            else:
                walk(child)

    walk(element)
    return "".join(parts)


def continues_vertical_merge(cell) -> bool:
    merge = cell.find(f"{WORD}tcPr/{WORD}vMerge")
    return merge is not None and merge.get(WORD + "val", "continue") == "continue"


def table_rows(table):
    """Yield row numbers with cell texts and the count of nested tables that were not read."""
    for number, row in enumerate(body_items(table, (WORD + "tr",)), 1):
        parts, nested = [], 0
        for cell in body_items(row, (WORD + "tc",)):
            if continues_vertical_merge(cell):
                parts.append("")
                continue
            items = list(body_items(cell, (WORD + "p", WORD + "tbl")))
            nested += sum(item.tag == WORD + "tbl" for item in items)
            parts.append("\n".join(paragraph_text(item) for item in items if item.tag == WORD + "p").strip())
        yield number, parts, nested


def read_docx(path: Path, kind: str, limits: dict, options: Options) -> Extraction:
    from docx import Document
    from docx.text.paragraph import Paragraph

    document = Document(path)
    blocks, heading = [], None
    paragraph_index = table_index = chars = nested = 0
    for item in body_items(document.element.body, (WORD + "p", WORD + "tbl")):
        if item.tag == WORD + "tbl":
            table_index += 1
            for row_index, parts, inner in table_rows(item):
                nested += inner
                if not any(part.strip() for part in parts):
                    continue
                text = " | ".join(parts)
                chars += len(text)
                check("chars", chars, limits["max_text_chars"])
                locator = {"kind": "doc_table", "table": table_index, "row": row_index}
                blocks.append(with_heading({"locator": locator, "text": text}, heading))
            continue
        paragraph_index += 1
        text = paragraph_text(item)
        if not text.strip():
            continue
        chars += len(text)
        check("chars", chars, limits["max_text_chars"])
        style = Paragraph(item, document).style
        if style is not None and (style.name or "").startswith(("Heading", "Заголовок")):
            heading = text.strip()
        locator = {"kind": "paragraph", "index": paragraph_index}
        blocks.append(with_heading({"locator": locator, "text": text}, heading))
    report = {
        "paragraphs": paragraph_index,
        "tables": table_index,
        "nested_tables": nested,
    }
    notes = [
        f"Абзацев: {paragraph_index}, таблиц: {table_index}.",
        "Колонтитулы, сноски, надписи и изображения не читаются.",
    ]
    if nested:
        notes.append(f"Вложенных таблиц не прочитано: {nested}.")
    return Extraction("partial" if nested else "ok", blocks, report, notes)


def with_heading(block: dict, heading: str | None) -> dict:
    return block | {"heading": heading} if heading else block


def read_pdf(path: Path, kind: str, limits: dict, options: Options) -> Extraction:
    from pypdf import PdfReader

    logging.getLogger("pypdf").setLevel(logging.ERROR)
    reader = PdfReader(path)
    if reader.is_encrypted:
        raise Unreadable("PDF зашифрован")
    total = len(reader.pages)
    first, last = options.pages or (1, total)
    last = min(last, total)
    selected = range(first, last + 1)
    check("pages", len(selected), limits["max_pdf_pages"])
    blocks, without_text = [], []
    for number in selected:
        text = (reader.pages[number - 1].extract_text() or "").strip()
        if text:
            blocks.append({"locator": {"kind": "page", "page": number}, "text": text})
        else:
            without_text.append(number)
    chosen = f"Выбраны страницы {first}-{last}." if options.pages else None
    if not selected or (not blocks and options.pages):
        notes = [f"В файле страниц: {total}.", f"В выбранных страницах {first}-{options.pages[1]} текста нет."]
        return Extraction("empty", report={"pages": total, "pages_selected": list(options.pages)}, notes=notes)
    if not blocks:
        raise Scan("ни на одной странице нет извлекаемого текста, похоже на скан; OCR не поддерживается")
    report = {
        "pages": total,
        "pages_read": len(selected) - len(without_text),
        "pages_without_text": without_text,
    }
    if options.pages:
        report["pages_selected"] = [first, last]
    notes = [f"Прочитано {len(blocks)} из {total} страниц."] + ([chosen] if chosen else [])
    if without_text:
        notes.append("Без текста: " + ", ".join(map(str, without_text)) + ".")
    notes.append("Изображения не разбираются.")
    status = "ok" if len(blocks) == total else "partial"
    return Extraction(status, blocks, report, notes)


def decode_table(raw: bytes, encoding: str | None) -> tuple[str, str, bool]:
    if encoding:
        return decode(raw, encoding), encoding, False
    if raw.startswith((b"\xff\xfe", b"\xfe\xff")):
        try:
            return raw.decode("utf-16"), "utf-16", False
        except UnicodeDecodeError as error:
            raise Unreadable("не удалось декодировать как utf-16") from error
    if b"\x00" in raw:
        raise Unreadable("двоичное содержимое в текстовой таблице")
    try:
        return raw.decode("utf-8-sig"), "utf-8", False
    except UnicodeDecodeError:
        pass
    try:
        return raw.decode("cp1251"), "cp1251", True
    except UnicodeDecodeError as error:
        raise Unreadable("неизвестная кодировка") from error


def detect_delimiter(text: str) -> tuple[str, bool]:
    """Pick the delimiter that splits the sample into equal rows; anything else is a guess."""
    candidates = {}
    for delimiter in ";,\t":
        reader = csv.reader(io.StringIO(text, newline=""), delimiter=delimiter)
        try:
            rows = [row for row in islice(reader, DELIMITER_SAMPLE_ROWS) if row]
        except csv.Error:
            rows = []
        candidates[delimiter] = rows
    even = [d for d, rows in candidates.items() if rows and len({len(row) for row in rows}) == 1 and len(rows[0]) > 1]
    if len(even) == 1:
        return even[0], False
    pool = even or list(candidates)
    return max(pool, key=lambda d: sum(len(row) > 1 for row in candidates[d])), True


def value_type(value: str) -> str:
    value = value.strip()
    if NUMBER.fullmatch(value):
        return "number"
    return "date" if DATE.fullmatch(value) else "text"


def column_stats(rows: list[list[str]]) -> list[dict]:
    header, *data = rows
    width = max(map(len, rows))
    columns = []
    for index in range(width):
        values = [row[index] for row in data if index < len(row) and row[index].strip()]
        kinds = {value_type(value) for value in values}
        columns.append({
            "index": index + 1,
            "name": header[index] if index < len(header) else None,
            "filled": len(values),
            "empty": len(data) - len(values),
            "type": (kinds.pop() if len(kinds) == 1 else "mixed") if kinds else None,
        })
    return columns


def read_csv(path: Path, kind: str, limits: dict, options: Options) -> Extraction:
    text, encoding, encoding_assumed = decode_table(path.read_bytes(), options.encoding)
    delimiter, delimiter_assumed = options.delimiter or None, False
    if delimiter is None:
        delimiter, delimiter_assumed = detect_delimiter(text)
    reader = csv.reader(io.StringIO(text, newline=""), delimiter=delimiter, strict=True)
    blocks, rows, cells = [], [], 0
    try:
        for number, row in enumerate(reader, 1):
            check("rows", number, limits["max_table_rows"])
            if not row:
                continue
            check("columns", len(row), limits["max_table_columns"])
            cells += len(row)
            check("cells", cells, limits["max_table_cells"])
            rows.append(row)
            locator = {"kind": "rows", "from": number, "to": number}
            blocks.append({"locator": locator, "text": " | ".join(row), "cells": row})
    except csv.Error as error:
        raise Unreadable("нарушена структура CSV") from error
    if not rows:
        return Extraction("empty", report={"encoding": encoding}, notes=["Строк с данными нет."])
    uneven = sum(len(row) != len(rows[0]) for row in rows)
    delimiter_assumed = delimiter_assumed or (uneven > 0 and not options.delimiter)
    report = {
        "encoding": encoding,
        "encoding_assumed": encoding_assumed,
        "delimiter": delimiter,
        "delimiter_assumed": delimiter_assumed,
        "rows": len(rows),
        "uneven_rows": uneven,
        "empty_cells": sum(cell == "" for row in rows for cell in row),
        "columns": column_stats(rows),
    }
    notes = [f"Строк: {len(rows)}. Первая строка принята за заголовок."]
    if encoding_assumed:
        notes.append(f"Кодировка {encoding} предположена, текст нужно сверить.")
    if delimiter_assumed:
        notes.append("Разделитель предположен, столбцы нужно сверить.")
    if uneven:
        notes.append(f"Строк с другим числом столбцов: {uneven}.")
    status = "partial" if encoding_assumed or delimiter_assumed else "ok"
    return Extraction(status, blocks, report, notes)


def cell_value(value):
    if isinstance(value, (datetime, date, time)):
        return value.isoformat()
    if isinstance(value, timedelta):
        return str(value)
    return value


def is_formula(origin, value) -> bool:
    if hasattr(origin, "text"):
        return True
    return isinstance(origin, str) and origin.startswith("=") and value != origin


def read_xlsx(path: Path, kind: str, limits: dict, options: Options) -> Extraction:
    from openpyxl import load_workbook

    with closing(load_workbook(path, data_only=True, read_only=True)) as values:
        with closing(load_workbook(path, data_only=False, read_only=True)) as formulas:
            return scan_workbook(values, formulas, limits, options)


def scan_workbook(values, formulas, limits: dict, options: Options) -> Extraction:
    names = [sheet.title for sheet in values.worksheets]
    wanted = options.sheets or names
    unknown = [name for name in wanted if name not in names]
    if unknown:
        raise BadOption("в книге нет запрошенного листа")
    blocks, sheets, no_value, cached = [], [], [], []
    cells = 0
    for sheet in values.worksheets:
        if sheet.title not in wanted:
            continue
        stats, cells = scan_sheet(sheet, formulas[sheet.title], limits, cells, blocks, no_value, cached)
        sheets.append(stats)
    report = {
        "sheets": sheets,
        "sheets_skipped": [name for name in names if name not in wanted],
        "formula_no_value": no_value,
        "formula_cached": cached,
    }
    notes = [f"Прочитано листов: {len(sheets)} из {len(names)}."]
    if report["sheets_skipped"]:
        notes.append("Прочитаны: " + ", ".join(sheet["name"] for sheet in sheets) + ".")
        notes.append("Не читались: " + ", ".join(report["sheets_skipped"]) + ".")
    hidden = [sheet["name"] for sheet in sheets if sheet["hidden"]]
    if hidden:
        notes.append(f"Скрытых листов среди прочитанных: {len(hidden)}.")
    if no_value:
        notes.append(f"Формул без сохранённого значения: {len(no_value)}.")
    if cached:
        notes.append(f"Формул с сохранённым значением: {len(cached)}, свежесть значений не проверялась.")
    notes.append("Объединённая ячейка даёт значение только в верхней левой, скрытые строки и столбцы не отличаются от видимых. Примечания и диаграммы не читаются.")
    status = "partial" if no_value or report["sheets_skipped"] else "ok"
    if not blocks:
        status = "empty"
    return Extraction(status, blocks, report, notes)


def scan_sheet(sheet, origin_sheet, limits, cells, blocks, no_value, cached) -> tuple[dict, int]:
    from openpyxl.utils import get_column_letter

    sheet.reset_dimensions()
    origin_sheet.reset_dimensions()
    pairs = zip(sheet.iter_rows(values_only=True), origin_sheet.iter_rows(values_only=True))
    count = width = 0
    for r, (row, source) in enumerate(pairs, 1):
        count += 1
        check("rows", count, limits["max_table_rows"])
        check("columns", len(row), limits["max_table_columns"])
        cells += len(row)
        check("cells", cells, limits["max_table_cells"])
        width = max(width, len(row))
        found, parts, missing = {}, [], []
        for c, (value, origin) in enumerate(zip(row, source), 1):
            address = f"{get_column_letter(c)}{r}"
            if value is None and origin is not None:
                found[address] = None
                parts.append(FORMULA_NO_VALUE)
                missing.append(address)
                no_value.append(f"{sheet.title}!{address}")
            elif value is not None:
                found[address] = cell_value(value)
                parts.append(str(found[address]))
                if is_formula(origin, value):
                    cached.append(f"{sheet.title}!{address}")
        if not found:
            continue
        first, last = next(iter(found)), list(found)[-1]
        locator = {"kind": "cells", "sheet": sheet.title, "range": f"{first}:{last}" if first != last else first}
        block = {"locator": locator, "text": " | ".join(parts), "cells": found}
        if missing:
            block["formula_no_value"] = missing
        blocks.append(block)
    hidden = sheet.sheet_state != "visible"
    return {"name": sheet.title, "rows": count, "columns": width, "hidden": hidden}, cells


def read_image(path: Path, kind: str, limits: dict, options: Options) -> Extraction:
    """Register the picture itself: its header is read, its pixels are not. The text comes from observations."""
    try:
        info = read_header(path.read_bytes())
    except Animated as error:
        raise Unreadable(str(error)) from error
    except NotImage as error:
        raise Unreadable("файл не является изображением нужного формата") from error
    if info.format != IMAGE_FORMATS[path.suffix.lower()]:
        raise Unreadable("содержимое файла не соответствует его расширению")
    check("image_side", max(info.width, info.height), limits["max_image_side"])
    notes = ["Наблюдения не записаны."]
    if info.height / info.width > TALL_IMAGE_RATIO:
        notes.append(TALL_IMAGE_NOTE)
    report = {"format": info.format, "width": info.width, "height": info.height}
    summary = f"Изображение {info.format.upper()} {info.width}×{info.height}."
    return Extraction("partial", [], report, notes, summary)


READERS = {
    "md": read_text,
    "txt": read_text,
    "docx": read_docx,
    "pdf": read_pdf,
    "csv": read_csv,
    "xlsx": read_xlsx,
    "image": read_image,
}


def extract(path: Path, limits: dict, options: Options | None = None) -> Extraction:
    """Read a file whose extension is supported; BadOption means the request itself is wrong."""
    options = options or Options()
    kind = SUPPORTED[path.suffix.lower()]
    size = path.stat().st_size
    base = {"file": path.name, "type": kind, "bytes": size}
    if size > limits["max_file_bytes"]:
        return failed("over_limit", base, f"bytes: {size} > {limits['max_file_bytes']}")
    if size == 0:
        return failed("empty", base, "файл пуст")
    try:
        result = READERS[kind](path, kind, limits, options)
    except (BadOption, ImportError, MemoryError, OSError):
        raise
    except OverLimit as error:
        return failed("over_limit", base, str(error))
    except Scan as error:
        return failed("unsupported", base, str(error))
    except Unreadable as error:
        return failed("unreadable", base, str(error))
    except Exception as error:
        return failed("unreadable", base, f"ошибка разбора: {type(error).__name__}")
    if result.status in TEXT_STATUSES and not result.blocks and kind != "image":
        result.status = "empty"
    result.blocks = numbered(result.blocks)
    result.report = base | result.report | {"status": result.status, "blocks": len(result.blocks)}
    return result


def numbered(blocks: list[dict]) -> list[dict]:
    return [{"n": number} | block for number, block in enumerate(blocks, 1)]


def failed(status: str, base: dict, reason: str) -> Extraction:
    advice = " Сократите материал или выберите часть." if status == "over_limit" else ""
    report = base | {"status": status, "reason": reason, "blocks": 0}
    return Extraction(status, report=report, notes=[f"Причина: {reason}.{advice}"])
