import contextlib
import io
import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))

import revision  # noqa: E402

DEMO = ROOT / "agent" / "contracts" / "examples" / "valid" / "demo-case"
REFUSED = 4
STATUS = "Статус: выбор сделан [DEC-0002], вариант [OPT-0001]"
START = "(начало)"

PRODUCT = f"""# Продуктовая стратегия

{STATUS}

## Ценность

Заявки растут [CLM-0004].

## Отказы

Новых пунктов не открываем [CLM-0008].
"""
COMMUNICATION = f"""# Коммуникационная стратегия

{STATUS}

## Барьер

Не знают правил [CLM-0008].
"""
PLAN = f"""# План проверки

{STATUS}

## Исходное состояние

База неизвестна [Q-0001].

## Период наблюдения

Три месяца.
"""

TWO_ENTRIES = """# Пересмотры

## Пересмотр 1 — 2026-11-10

- Новые данные: [SRC-0014]
- Изменилось: [CLM-0007] было «10», стало «12» [CLM-0031]
- Условия пересмотра: не сработало ни одно
- Затронуто: validation-plan.md «Исходное состояние»; commitments.md «Действия»; [CALC-0004]
- Не затронуто: communication-strategy.md
- Выбор: [DEC-0012]
- Прежние версии: outputs/.history/1/

## Пересмотр 2 — 2026-11-20

- Новые данные: [SRC-0015]
- Изменилось: ничего
- Условия пересмотра: не сработало ни одно
- Затронуто: ничего
- Не затронуто: все файлы
- Выбор: изменений нет
- Прежние версии: нет
"""


def codes(problems):
    return [item["code"] for item in problems]


class ParseRevisions(unittest.TestCase):
    def test_two_entries_parsed(self):
        entries, problems = revision.parse_revisions(TWO_ENTRIES)
        self.assertEqual(problems, [])
        self.assertEqual([e["number"] for e in entries], [1, 2])
        self.assertEqual([e["choice"] for e in entries], ["DEC-0012", None])
        self.assertEqual([e["snapshot"] for e in entries], ["outputs/.history/1/", None])
        self.assertEqual([e["date"] for e in entries], ["2026-11-10", "2026-11-20"])
        self.assertEqual([e["line"] for e in entries], [3, 13])
        self.assertIn("validation-plan.md", entries[0]["touched"])
        self.assertIn("commitments.md", entries[0]["touched"])
        self.assertIn("CALC-0004", entries[0]["touched"])
        self.assertEqual(entries[1]["touched"], [])

    def test_missing_bullet_is_reported(self):
        text = TWO_ENTRIES.replace("- Выбор: [DEC-0012]\n", "")
        _, problems = revision.parse_revisions(text)
        self.assertEqual(codes(problems), ["REVISION_FORMAT"])
        self.assertEqual(problems[0]["line"], 3)

    def test_unknown_choice_is_reported(self):
        _, problems = revision.parse_revisions(TWO_ENTRIES.replace("[DEC-0012]", "что-то"))
        self.assertEqual(codes(problems), ["REVISION_FORMAT"])

    def test_bad_title_and_heading(self):
        text = TWO_ENTRIES.replace("# Пересмотры", "# История").replace("Пересмотр 2 — 2026-11-20", "Пересмотр два")
        _, problems = revision.parse_revisions(text)
        self.assertEqual(codes(problems), ["REVISION_FORMAT", "REVISION_FORMAT"])

    def test_snapshot_folder_must_match_number(self):
        _, problems = revision.parse_revisions(TWO_ENTRIES.replace(".history/1/", ".history/7/"))
        self.assertEqual(codes(problems), ["REVISION_FORMAT"])

    def test_gap_in_numbers(self):
        _, problems = revision.parse_revisions(TWO_ENTRIES.replace("Пересмотр 2 —", "Пересмотр 3 —"))
        self.assertEqual(codes(problems), ["REVISION_NUMBER"])

    def test_title_only_is_empty(self):
        self.assertEqual(revision.parse_revisions("# Пересмотры\n"), ([], []))

    def test_garbage_does_not_raise(self):
        for text in ("", "\x00﻿[[]]", "## \n- Выбор:\n", "random\ntext\n## Пересмотр 1 — 2026-13-45\n- Выбор: [DEC-1]"):
            with self.subTest(text=text):
                entries, problems = revision.parse_revisions(text)
                self.assertIsInstance(entries, list)
                self.assertTrue(problems or not text.strip() or entries == [])


class Base(unittest.TestCase):
    """A copy of the contract example with small outputs written for the test."""

    def setUp(self):
        folder = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(folder.cleanup)
        self.case = Path(folder.name) / "demo-case"
        shutil.copytree(DEMO, self.case)
        shutil.rmtree(self.case / "outputs")
        self.write("outputs/product-strategy.md", PRODUCT)
        self.write("outputs/communication-strategy.md", COMMUNICATION)
        self.write("outputs/validation-plan.md", PLAN)

    def write(self, name, text):
        path = self.case / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")

    def run_tool(self, *args):
        raw = io.BytesIO()
        out = io.TextIOWrapper(raw, encoding="utf-8", newline="\n")
        with contextlib.redirect_stdout(out):
            code = revision.main([*map(str, args)])
        out.flush()
        return code, json.loads(raw.getvalue().decode("utf-8").splitlines()[0])

    def affected(self, *ids):
        code, report = self.run_tool("affected", self.case, *ids)
        self.assertEqual(code, 0, report)
        return report

    def files(self):
        return {p.relative_to(self.case).as_posix(): p.read_bytes() for p in self.case.rglob("*") if p.is_file()}


class Affected(Base):
    def test_union_of_two_ids(self):
        report = self.affected("SRC-0004", "CALC-0002", "CLM-0001")
        self.assertEqual(report["command"], "affected")
        self.assertEqual(report["ids"], ["SRC-0004", "CALC-0002", "CLM-0001"])
        self.assertEqual(report["records"], {"CLM": ["CLM-0001"], "Q": ["Q-0002"]})

    def test_sections_split(self):
        report = self.affected("CLM-0004")
        self.assertEqual(report["affected"], {"outputs/product-strategy.md": ["Ценность"]})
        self.assertEqual(report["unaffected"]["outputs/product-strategy.md"], [START, "Отказы"])
        self.assertEqual(report["unaffected"]["outputs/communication-strategy.md"], [START, "Барьер"])
        self.assertNotIn("outputs/communication-strategy.md", report["affected"])

    def test_status_line_is_not_a_citation(self):
        report = self.affected("DEC-0002", "OPT-0001")
        self.assertEqual(report["affected"], {})
        self.assertEqual(report["reapproval"], [])
        self.assertEqual(report["unaffected"]["outputs/product-strategy.md"], [START, "Ценность", "Отказы"])

    def test_preamble_counts_for_other_lines(self):
        self.write("outputs/product-strategy.md", PRODUCT.replace(STATUS, STATUS + "\n\nОснование [CLM-0004]."))
        report = self.affected("CLM-0004")
        self.assertEqual(report["affected"], {"outputs/product-strategy.md": [START, "Ценность"]})
        self.assertEqual(report["reapproval"], [])
        self.write("outputs/validation-plan.md", PLAN.replace(STATUS, STATUS + "\n\nОснование [CLM-0004]."))
        self.assertEqual(self.affected("CLM-0004")["reapproval"], [])

    def test_claim_in_an_option_basis_needs_no_reapproval(self):
        # the plan names the direction in its status line only; the claim lies under the option
        report = self.affected("CLM-0006")
        self.assertEqual(report["records"], {"OPT": ["OPT-0003"], "DEC": ["DEC-0002"]})
        self.assertEqual(report["affected"], {})
        self.assertEqual(report["reapproval"], [])

    def test_accounting_report_is_not_analysed(self):
        self.write("outputs/accounting.md", "# Учёт работы\n\nПо [CLM-0004].\n")
        report = self.affected("CLM-0004")
        self.assertNotIn("outputs/accounting.md", report["affected"])
        self.assertNotIn("outputs/accounting.md", report["unaffected"])

    def test_reapproval(self):
        self.assertEqual(self.affected("CLM-0004")["reapproval"], [])
        self.write("outputs/validation-plan.md", PLAN.replace("[Q-0001]", "[CLM-0004]"))
        self.assertEqual(self.affected("CLM-0004")["reapproval"], ["thresholds"])
        self.write("outputs/commitments.md", "# Обязательства\n\n## Действия\n\nСрок [CLM-0004].\n")
        self.assertEqual(self.affected("CLM-0004")["reapproval"], ["thresholds", "commitments"])

    def test_unaffected_commitments_need_no_reapproval(self):
        self.write("outputs/commitments.md", "# Обязательства\n\n## Действия\n\nСрок [CLM-0008].\n")
        report = self.affected("CLM-0004")
        self.assertEqual(report["reapproval"], [])
        self.assertEqual(report["unaffected"]["outputs/commitments.md"], ["Действия"])

    def test_unknown_id_refused(self):
        for bad in ("CLM-9999", "nonsense"):
            with self.subTest(bad):
                code, report = self.run_tool("affected", self.case, bad)
                self.assertEqual((code, report["status"]), (REFUSED, "refused"))

    def test_withdrawn_id_traced(self):
        self.write("outputs/product-strategy.md", PRODUCT.replace("[CLM-0004]", "[CLM-0005]"))
        report = self.affected("CLM-0005")
        self.assertEqual(report["affected"], {"outputs/product-strategy.md": ["Ценность"]})

    def test_revisions_md_not_analysed(self):
        self.write(revision.REVISIONS, "# Пересмотры\n\n## Пересмотр 1 — 2026-11-10\n\n- Изменилось: [CLM-0004]\n")
        report = self.affected("CLM-0004")
        self.assertNotIn(revision.REVISIONS, report["affected"])
        self.assertNotIn(revision.REVISIONS, report["unaffected"])

    def test_history_not_analysed(self):
        self.write("outputs/.history/1/product-strategy.md", PRODUCT)
        report = self.affected("CLM-0004")
        self.assertEqual(list(report["affected"]), ["outputs/product-strategy.md"])

    def test_changes_nothing(self):
        before = self.files()
        self.affected("CLM-0004")
        self.assertEqual(self.files(), before)


class Snapshot(Base):
    def setUp(self):
        super().setUp()
        self.write("outputs/accounting.md", "# Учёт работы\n")
        self.write(revision.REVISIONS, "# Пересмотры\n")
        self.folder = self.case / "outputs" / ".history" / "1"

    def snapshot(self, number=1):
        return self.run_tool("snapshot", self.case, number)

    def test_copies_all_but_log(self):
        code, report = self.snapshot()
        self.assertEqual(code, 0)
        self.assertEqual(report, {"command": "snapshot", "number": 1, "folder": "outputs/.history/1/",
                                  "files": ["accounting.md", "communication-strategy.md", "product-strategy.md",
                                            "validation-plan.md"], "already": False})
        self.assertEqual((self.folder / "product-strategy.md").read_text(encoding="utf-8"), PRODUCT)
        self.assertFalse((self.folder / "revisions.md").exists())

    def test_repeat_is_already(self):
        self.snapshot()
        before = self.files()
        code, report = self.snapshot()
        self.assertEqual((code, report["already"]), (0, True))
        self.assertEqual(self.files(), before)

    def test_changed_outputs_refused(self):
        self.snapshot()
        self.write("outputs/product-strategy.md", PRODUCT + "\nПравка.\n")
        before = self.files()
        code, report = self.snapshot()
        self.assertEqual((code, report["status"]), (REFUSED, "refused"))
        self.assertEqual(self.files(), before)

    def test_extra_file_in_folder_refused(self):
        self.snapshot()
        (self.folder / "extra.md").write_text("x", encoding="utf-8")
        self.assertEqual(self.snapshot()[0], REFUSED)

    def test_bad_number_refused(self):
        self.assertEqual(self.snapshot(0)[0], REFUSED)
        self.assertFalse((self.case / "outputs" / ".history").exists())


if __name__ == "__main__":
    unittest.main()
