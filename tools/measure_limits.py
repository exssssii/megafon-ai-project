"""Measure the reader at the declared limits on generated files.

Run with: uv run --group fixtures python tools/measure_limits.py
"""
import csv
import json
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from docx import Document
from fpdf import FPDF
from openpyxl import Workbook

TOOLS = Path(__file__).resolve().parent
LIMITS = json.loads((TOOLS / "input_limits.json").read_text(encoding="utf-8"))
ROWS = LIMITS["max_table_rows"]
ROW = [f"значение {n}" for n in range(10)]
WIDE_ROW = ROW * (LIMITS["max_table_cells"] // ROWS // 10)


def make_csv(path: Path, rows: int) -> None:
    with path.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.writer(stream, delimiter=";")
        writer.writerows(ROW for _ in range(rows))


def make_xlsx(path: Path) -> None:
    book = Workbook(write_only=True)
    sheet = book.create_sheet("Данные")
    for _ in range(ROWS):
        sheet.append(WIDE_ROW)
    book.save(path)


def make_pdf(path: Path) -> None:
    pdf = FPDF()
    pdf.set_font("helvetica", size=10)
    for number in range(LIMITS["max_pdf_pages"]):
        pdf.add_page()
        pdf.multi_cell(0, 5, f"Page {number} " + "text line " * 300)
    pdf.output(path)


def make_docx(path: Path) -> None:
    document = Document()
    paragraph = "абзац " * 100
    for _ in range(LIMITS["max_text_chars"] // (len(paragraph) + 8)):
        document.add_paragraph(paragraph)
    document.save(path)


def measure(path: Path) -> dict:
    started = time.perf_counter()
    result = subprocess.run(
        [sys.executable, str(TOOLS / "read_material.py"), "inspect", str(path)],
        capture_output=True,
        encoding="utf-8",
    )
    seconds = round(time.perf_counter() - started, 1)
    report = json.loads(result.stdout.splitlines()[0])
    return {
        "file": path.name,
        "mb": round(path.stat().st_size / 2**20, 2),
        "seconds": seconds,
        "exit": result.returncode,
        "status": report["status"],
        "reason": report.get("reason"),
    }


def main() -> int:
    with tempfile.TemporaryDirectory(dir=TOOLS.parent) as folder:
        base = Path(folder)
        files = {
            "limit.csv": lambda p: make_csv(p, ROWS),
            "over.csv": lambda p: make_csv(p, ROWS + 1),
            "limit.xlsx": make_xlsx,
            "limit.pdf": make_pdf,
            "limit.docx": make_docx,
        }
        for name, make in files.items():
            make(base / name)
            print(json.dumps(measure(base / name), ensure_ascii=False), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
