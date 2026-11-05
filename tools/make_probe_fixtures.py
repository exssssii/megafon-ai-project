"""Generate the small input files used to verify document and table reading."""
import csv
import sys
from datetime import datetime
from pathlib import Path

from docx import Document
from fpdf import FPDF
from openpyxl import Workbook

OUT = Path(__file__).resolve().parent.parent / "tests" / "fixtures" / "environment"
FIXED_DATE = datetime(2026, 11, 5)
FONT_CANDIDATES = (
    Path("C:/Windows/Fonts/arial.ttf"),
    Path("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"),
)


def write_docx(path: Path) -> None:
    document = Document()
    document.core_properties.created = document.core_properties.modified = FIXED_DATE
    document.add_heading("Проверочный документ", level=1)
    document.add_paragraph("Контрольный абзац: маркер ДОК-7Q3 подтверждает чтение текста.")
    table = document.add_table(rows=2, cols=2)
    table.cell(0, 0).text = "Показатель"
    table.cell(0, 1).text = "Значение"
    table.cell(1, 0).text = "Маркер таблицы"
    table.cell(1, 1).text = "ДОКТАБ-4K9"
    document.save(path)


def write_pdf(path: Path, font: Path) -> None:
    pdf = FPDF()
    pdf.set_creation_date(FIXED_DATE)
    pdf.add_font("probe", fname=font)
    pdf.set_font("probe", size=12)
    pdf.add_page()
    pdf.cell(text="Первая страница проверочного документа.")
    pdf.add_page()
    pdf.cell(text="Вторая страница: маркер ПДФ-2M8 подтверждает постраничное чтение.")
    pdf.output(path)


def write_xlsx(path: Path) -> None:
    book = Workbook()
    book.properties.created = book.properties.modified = FIXED_DATE
    sheet = book.active
    sheet.title = "Выручка"
    sheet.append(["Месяц", "Сумма, руб.", "Итог нарастающим"])
    sheet.append(["Январь", 1200, 1200])
    sheet.append(["Февраль", 1284, "=C2+B3"])
    book.create_sheet("Заметки")["A1"] = "Маркер листа ТАБ-9X1"
    book.save(path)


def write_csv(path: Path) -> None:
    with path.open("w", encoding="cp1251", newline="") as stream:
        writer = csv.writer(stream, delimiter=";")
        writer.writerow(["Месяц", "Клиенты"])
        writer.writerow(["Январь", "410"])
        writer.writerow(["Февраль", ""])
        writer.writerow(["Маркер ЦСВ-5T2", "437"])


def main() -> int:
    font = next((item for item in FONT_CANDIDATES if item.exists()), None)
    if font is None:
        print("No Cyrillic TrueType font found", file=sys.stderr)
        return 1
    OUT.mkdir(parents=True, exist_ok=True)
    write_docx(OUT / "probe-document.docx")
    write_pdf(OUT / "probe-document.pdf", font)
    write_xlsx(OUT / "probe-table.xlsx")
    write_csv(OUT / "probe-table.csv")
    (OUT / "probe-note.md").write_text(
        "# Проверочная заметка\n\nМаркер МД-3H6 подтверждает чтение UTF-8.\n",
        encoding="utf-8",
        newline="\n",
    )
    print(f"Fixtures written to {OUT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
