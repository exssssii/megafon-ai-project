import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))

from probe_read import EXIT_OK, EXIT_OVER_LIMIT, EXIT_UNREADABLE, LIMITS_FILE, probe  # noqa: E402

FIXTURES = ROOT / "tests" / "fixtures" / "environment"
LIMITS = json.loads(LIMITS_FILE.read_text(encoding="utf-8"))


class Fixtures(unittest.TestCase):
    def read(self, name):
        code, report, lines = probe(FIXTURES / name, LIMITS)
        self.assertEqual(code, EXIT_OK, report)
        self.assertTrue(report["complete"])
        return report, "\n".join(lines)

    def test_markers_keep_their_locators(self):
        expected = {
            "probe-note.md": "[L3] Маркер МД-3H6",
            "probe-document.docx": "[P2] Контрольный абзац: маркер ДОК-7Q3",
            "probe-document.pdf": "[p.2] Вторая страница: маркер ПДФ-2M8",
            "probe-table.xlsx": "[Выручка!B3] 1284",
            "probe-table.csv": "[R4] Маркер ЦСВ-5T2 | 437",
        }
        for name, marker in expected.items():
            with self.subTest(name):
                self.assertIn(marker, self.read(name)[1])

    def test_docx_table_and_second_sheet_are_read(self):
        self.assertIn("[T1R2] Маркер таблицы | ДОКТАБ-4K9", self.read("probe-document.docx")[1])
        self.assertIn("[Заметки!A1] Маркер листа ТАБ-9X1", self.read("probe-table.xlsx")[1])

    def test_formula_without_cached_value_is_reported_not_guessed(self):
        report, text = self.read("probe-table.xlsx")
        self.assertEqual(report["formulas_without_value"], ["Выручка!C3"])
        self.assertIn("[Выручка!C3] <formula without cached value>", text)

    def test_csv_encoding_delimiter_and_gaps_are_detected(self):
        report, _ = self.read("probe-table.csv")
        self.assertEqual(
            (report["encoding"], report["delimiter"], report["empty_cells"]), ("cp1251", ";", 1)
        )
        self.assertTrue(report["encoding_assumed"])


class Boundaries(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.folder = Path(folder.name)

    def test_each_limit_refuses_instead_of_truncating(self):
        cases = (
            ("probe-document.pdf", "max_pdf_pages", 1, "pages"),
            ("probe-table.csv", "max_table_rows", 3, "rows"),
            ("probe-table.xlsx", "max_table_rows", 2, "rows"),
            ("probe-table.xlsx", "max_table_columns", 2, "columns"),
            ("probe-table.xlsx", "max_table_cells", 5, "cells"),
            ("probe-document.docx", "max_text_chars", 10, "chars"),
            ("probe-note.md", "max_file_bytes", 10, "bytes"),
        )
        for name, limit, value, reason in cases:
            with self.subTest(name=name, limit=limit):
                code, report, lines = probe(FIXTURES / name, LIMITS | {limit: value})
                self.assertEqual(code, EXIT_OVER_LIMIT)
                self.assertEqual(report["status"], "over_limit")
                self.assertTrue(report["reason"].startswith(reason))
                self.assertEqual(lines, [])

    def test_output_cap_is_marked_partial(self):
        limits = LIMITS | {"max_output_chars": 40}
        code, report, lines = probe(FIXTURES / "probe-table.xlsx", limits)
        self.assertEqual(code, EXIT_OK)
        self.assertEqual(report["status"], "partial")
        self.assertFalse(report["complete"])
        self.assertLess(len(lines), report["lines_total"])

    def test_damaged_files_are_unreadable(self):
        for name in ("broken.pdf", "broken.docx", "broken.xlsx"):
            with self.subTest(name):
                path = self.folder / name
                path.write_bytes(b"not a real file")
                code, report, _ = probe(path, LIMITS)
                self.assertEqual(code, EXIT_UNREADABLE)
                self.assertEqual(report["status"], "unreadable")

    def test_pdf_without_text_is_not_reported_as_read(self):
        from pypdf import PdfWriter

        writer = PdfWriter()
        writer.add_blank_page(width=200, height=200)
        path = self.folder / "scan.pdf"
        writer.write(path)
        code, report, _ = probe(path, LIMITS)
        self.assertEqual(code, EXIT_UNREADABLE)
        self.assertIn("scans", report["reason"])

    def test_pdf_with_textless_page_is_not_complete(self):
        from pypdf import PdfReader, PdfWriter

        writer = PdfWriter()
        writer.append(PdfReader(FIXTURES / "probe-document.pdf"))
        writer.add_blank_page(width=200, height=200)
        path = self.folder / "mixed.pdf"
        writer.write(path)
        code, report, _ = probe(path, LIMITS)
        self.assertEqual((code, report["status"], report["complete"]), (EXIT_OK, "partial", False))
        self.assertEqual(report["pages_without_text"], 1)

    def test_encrypted_pdf_is_unreadable(self):
        from pypdf import PdfReader, PdfWriter

        writer = PdfWriter()
        writer.append(PdfReader(FIXTURES / "probe-document.pdf"))
        writer.encrypt("secret", algorithm="RC4-128")
        path = self.folder / "locked.pdf"
        writer.write(path)
        code, report, _ = probe(path, LIMITS)
        self.assertEqual((code, report["reason"]), (EXIT_UNREADABLE, "encrypted PDF"))

    def write(self, name, data):
        path = self.folder / name
        path.write_bytes(data)
        return path

    def test_csv_quotes_keep_records_intact(self):
        path = self.write("quoted.csv", '"первая\nвторая";5\nx;6\n'.encode("utf-8"))
        code, report, lines = probe(path, LIMITS)
        self.assertEqual((code, report["rows"]), (EXIT_OK, 2))
        self.assertEqual(lines[1], "[R2] x | 6")

    def test_csv_unusual_line_separator_stays_in_cell(self):
        path = self.write("separator.csv", "левая\u2028правая;5\nx;6\n".encode("utf-8"))
        self.assertEqual(probe(path, LIMITS)[1]["rows"], 2)

    def test_csv_unclosed_quote_does_not_swallow_rows(self):
        path = self.write("broken.csv", '"сбой;1\nx;2\ny;3\n'.encode("utf-8"))
        code, report, _ = probe(path, LIMITS)
        self.assertEqual((code, report["status"]), (EXIT_UNREADABLE, "unreadable"))

    def test_csv_delimiter_ignores_quoted_text(self):
        path = self.write("header.csv", '"Выручка, руб., всего";Клиенты\n100;5\n'.encode("utf-8"))
        report = probe(path, LIMITS)[1]
        self.assertEqual((report["delimiter"], report["delimiter_assumed"]), (";", False))

    def test_csv_utf16_and_binary_are_not_guessed_as_cp1251(self):
        report = probe(self.write("excel.csv", "Месяц;Сумма\n".encode("utf-16")), LIMITS)[1]
        self.assertEqual(report["encoding"], "utf-16")
        self.assertEqual(probe(self.write("binary.csv", b"\x00\x01\x02"), LIMITS)[0], EXIT_UNREADABLE)

    def test_non_utf8_text_is_unreadable(self):
        self.assertEqual(probe(self.write("bad.md", b"\xff\xfe\xfa"), LIMITS)[0], EXIT_UNREADABLE)

    def test_xlsx_wrong_dimension_does_not_hide_rows(self):
        import re
        import zipfile

        from openpyxl import Workbook

        book = Workbook()
        for row in range(5):
            book.active.append([row, row])
        source = self.folder / "source.xlsx"
        book.save(source)
        path = self.folder / "narrow.xlsx"
        with zipfile.ZipFile(source) as old, zipfile.ZipFile(path, "w") as new:
            for item in old.infolist():
                data = old.read(item)
                if item.filename.endswith("sheet1.xml"):
                    data = re.sub(rb'<dimension ref="[^"]*"', b'<dimension ref="A1"', data)
                new.writestr(item, data)
        self.assertEqual(probe(path, LIMITS)[1]["sheets"][0]["rows"], 5)

    def test_xlsx_array_formula_without_value_is_reported(self):
        from openpyxl import Workbook
        from openpyxl.worksheet.formula import ArrayFormula

        book = Workbook()
        book.active.title = "S"
        book.active["A1"] = 1
        book.active["B1"] = ArrayFormula("B1", "=SUM(A1:A1)")
        path = self.folder / "array.xlsx"
        book.save(path)
        self.assertEqual(probe(path, LIMITS)[1]["formulas_without_value"], ["S!B1"])

    def test_empty_input_is_not_a_complete_read(self):
        for name in ("empty.csv", "empty.md"):
            with self.subTest(name):
                code, report, _ = probe(self.write(name, b""), LIMITS)
                self.assertEqual((code, report["status"]), (EXIT_UNREADABLE, "empty"))
                self.assertFalse(report["complete"])

    def test_unknown_format_and_missing_file(self):
        path = self.folder / "image.png"
        path.write_bytes(b"x")
        self.assertEqual(probe(path, LIMITS)[1]["status"], "unsupported")
        self.assertEqual(probe(self.folder / "none.csv", LIMITS)[0], EXIT_UNREADABLE)


if __name__ == "__main__":
    unittest.main()
