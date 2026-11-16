import json
from pathlib import Path
import re
import sys
import unittest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))

import material_extract  # noqa: E402

EXAMPLES = ROOT / "examples"
ACCEPTANCE = ROOT / "docs" / "acceptance"
LIMITS = material_extract.load_limits()
SOLO, SMALL, LARGE = "Мастера-одиночки", "Салоны 2–5 мастеров", "Салоны от 6 мастеров"
SEGMENTS = {SOLO: "solo", SMALL: "small", LARGE: "large"}


def read(path: Path, **options):
    return material_extract.extract(path, LIMITS, material_extract.Options(**options))


def control(case_id: str) -> dict:
    return json.loads((ACCEPTANCE / "control" / f"{case_id}.json").read_text(encoding="utf-8"))


def values(case_id: str) -> dict:
    return {item["id"]: item for item in control(case_id)["numbers"]}


def segment_table() -> list[dict]:
    blocks = read(EXAMPLES / "retention-smb" / "materials" / "metrics.xlsx").blocks
    rows = []
    for block in blocks:
        if block["locator"]["sheet"] != "Сегменты":
            continue
        cells = block["cells"]
        number = re.search(r"\d+", next(iter(cells))).group()
        if number == "1":
            continue
        get = lambda column: cells.get(f"{column}{number}")
        rows.append({
            "month": get("A"), "segment": get("B"), "start": get("C"),
            "new": get("D"), "churn": get("E"), "tariff": get("F"),
        })
    return rows


def costs_table() -> dict:
    blocks = read(EXAMPLES / "retention-smb" / "materials" / "metrics.xlsx").blocks
    table = {}
    for block in blocks:
        if block["locator"]["sheet"] == "Тарифы и затраты":
            row = list(block["cells"].values())
            if row[0] in SEGMENTS:
                table[row[0]] = row[3]
    return table


def funnel_table() -> list[dict]:
    blocks = read(
        EXAMPLES / "direction-b2c" / "materials" / "pilot-funnel.csv", encoding="cp1251", delimiter=";",
    ).blocks
    head = blocks[0]["cells"]
    return [dict(zip(head, block["cells"])) for block in blocks[1:]]


class Files(unittest.TestCase):
    def test_main_materials_read_with_planned_status(self):
        for path, expected, options in (
            ("retention-smb/materials/metrics.xlsx", "partial", {}),
            ("retention-smb/materials/support-note.docx", "ok", {}),
            ("direction-b2c/materials/pilot-report.pdf", "ok", {}),
            ("direction-b2c/materials/pilot-funnel.csv", "ok", {"encoding": "cp1251", "delimiter": ";"}),
        ):
            with self.subTest(path):
                self.assertEqual(read(EXAMPLES / path, **options).status, expected)

    def test_csv_encoding_is_flagged_until_stated(self):
        result = read(EXAMPLES / "direction-b2c" / "materials" / "pilot-funnel.csv")
        self.assertEqual(result.status, "partial")

    def test_formula_without_value_is_reported(self):
        result = read(EXAMPLES / "retention-smb" / "materials" / "metrics.xlsx")
        self.assertTrue(any("formula_no_value" in block for block in result.blocks))

    def test_unreadable_inputs_are_not_read_as_text(self):
        for path in (
            "retention-smb/extra/contract-scan.pdf", "retention-smb/extra/metrics-2024.xlsx",
            "direction-b2c/extra/offer-scan.pdf", "direction-b2c/extra/metrics-old.xlsx",
        ):
            with self.subTest(path):
                result = read(EXAMPLES / path)
                self.assertIn(result.status, ("unsupported", "unreadable"))
                self.assertEqual(result.blocks, [])

    def test_injected_instructions_are_plain_data_in_every_format(self):
        for case, marker, names in (
            ("retention-smb", "SYNC-7Q4K", ("partner-brief.docx", "partner-prices.xlsx", "saved-page.md")),
            ("direction-b2c", "SYNC-3M8T", ("clinic-proposal.docx", "clinic-prices.xlsx", "saved-page.md")),
        ):
            for name in names:
                with self.subTest(name):
                    blocks = read(EXAMPLES / case / "extra" / name).blocks
                    self.assertTrue(any(marker in block["text"] for block in blocks))
                    self.assertFalse(any(re.search(r"canary|вредон", b["text"], re.I) for b in blocks))

    def test_file_names_do_not_reveal_purpose(self):
        for path in EXAMPLES.rglob("*"):
            self.assertNotRegex(path.name.lower(), r"canary|inject|malicious|trap|вредон")

    def test_expectations_stay_out_of_the_materials(self):
        self.assertEqual(list(EXAMPLES.rglob("*control*")), [])
        self.assertEqual(list(EXAMPLES.rglob("*expect*")), [])

    def test_no_contacts_or_keys_in_text_files(self):
        pattern = re.compile(r"[\w.+-]+@[\w-]+\.\w+|\+7\d{10}|sk-[A-Za-z0-9]{16}|ghp_[A-Za-z0-9]{16}")
        for path in EXAMPLES.rglob("*"):
            if path.suffix in (".md", ".csv"):
                raw = path.read_bytes()
                text = raw.decode("cp1251" if path.name == "pilot-funnel.csv" else "utf-8")
                self.assertIsNone(pattern.search(text), path.name)

    def test_materials_are_marked_as_training(self):
        mark = "Учебные данные"
        self.assertTrue(any(mark in b["text"] for b in read(EXAMPLES / "retention-smb/materials/metrics.xlsx").blocks))
        self.assertTrue(any(mark in b["text"] for b in read(EXAMPLES / "retention-smb/materials/support-note.docx").blocks))
        self.assertTrue(any(mark in b["text"] for b in read(EXAMPLES / "direction-b2c/materials/pilot-report.pdf").blocks))
        for path, options in (
            ("direction-b2c/materials/pilot-funnel.csv", {"encoding": "cp1251", "delimiter": ";"}),
            ("direction-b2c/extra/ads-export.csv", {}),
            ("retention-smb/extra/marketing-export.csv", {}),
        ):
            self.assertTrue(any(mark in b["text"] for b in read(EXAMPLES / path, **options).blocks), path)


class Retention(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.rows = segment_table()
        cls.expected = values("retention-smb")

    def complete(self, segment, first, last):
        return [
            row for row in self.rows
            if row["segment"] == segment and first <= row["month"] <= last
            and row["start"] is not None and row["churn"] is not None
        ]

    def rate(self, rows):
        return round(100 * sum(r["churn"] for r in rows) / sum(r["start"] for r in rows), 2)

    def assert_close(self, key, actual):
        item = self.expected[key]
        self.assertAlmostEqual(actual, item["value"], delta=item["tolerance"], msg=key)

    def test_table_shape(self):
        self.assertEqual(len(self.rows), 54)

    def test_locators_point_to_the_right_rows(self):
        for segment, slug in SEGMENTS.items():
            for suffix in ("before", "after"):
                item = self.expected[f"churn_{slug}_{suffix}"]
                numbers = [int(n) for n in item["where"].split("строки ")[1].split(", ")]
                located = [self.rows[number - 2] for number in numbers]
                self.assertTrue(all(row["segment"] == segment for row in located), item["id"])
                self.assertEqual(self.rate(located), item["value"])

    def test_churn_by_segment(self):
        for segment, slug in SEGMENTS.items():
            self.assert_close(f"churn_{slug}_before", self.rate(self.complete(segment, "2025-04", "2025-12")))
            self.assert_close(f"churn_{slug}_after", self.rate(self.complete(segment, "2026-01", "2026-09")))
        self.assert_close("churn_small_2026_03", self.rate(self.complete(SMALL, "2026-03", "2026-03")))

    def test_gaps_are_missing_but_follow_from_the_balance(self):
        solo = {row["month"]: row for row in self.rows if row["segment"] == SOLO}
        self.assertIsNone(solo["2026-02"]["churn"])
        self.assertIsNone(solo["2026-03"]["start"])
        derivable = {item["id"]: item["value"] for item in control("retention-smb")["derivable"]}
        march = solo["2026-04"]["start"] - solo["2026-03"]["new"] + solo["2026-03"]["churn"]
        self.assertEqual(derivable["start_solo_2026_03"], march)
        self.assertEqual(derivable["churn_solo_2026_02"], solo["2026-02"]["start"] + solo["2026-02"]["new"] - march)
        before = self.complete(SOLO, "2025-04", "2025-12")
        lifetime = sum(r["start"] for r in before) / sum(r["churn"] for r in before)
        self.assertAlmostEqual(derivable["lifetime_solo_before"], lifetime, delta=0.05)

    def test_revenue_contribution_and_average(self):
        costs = costs_table()
        total = clients = 0
        for segment, slug in SEGMENTS.items():
            row = next(r for r in self.rows if r["segment"] == segment and r["month"] == "2026-09")
            end = row["start"] + row["new"] - row["churn"]
            total += end * row["tariff"]
            clients += end
            self.assert_close(f"contribution_{slug}_2026_09", end * (row["tariff"] - costs[segment]))
        self.assert_close("mrr_2026_09", total)
        december = [r for r in self.rows if r["month"] == "2025-12"]
        self.assert_close("mrr_2025_12", sum((r["start"] + r["new"] - r["churn"]) * r["tariff"] for r in december))
        self.assert_close("arpa_2026_09", round(total / clients))

    def test_revenue_sheets_use_the_declared_units(self):
        blocks = read(EXAMPLES / "retention-smb" / "materials" / "metrics.xlsx").blocks
        monthly = [b for b in blocks if b["locator"]["sheet"] == "Выручка по месяцам"]
        quarterly = [b for b in blocks if b["locator"]["sheet"] == "Выручка по кварталам"]
        self.assertIn("тыс. руб.", monthly[0]["text"])
        self.assertIn("млн руб.", quarterly[0]["text"])
        last = list(monthly[-1]["cells"].values())[1]
        self.assertAlmostEqual(last * 1000, self.expected["mrr_2026_09"]["value"], delta=50)

    def test_quarter_figure_and_monthly_figure_differ_by_base(self):
        rows = [r for r in self.rows if "2026-07" <= r["month"] <= "2026-09"]
        churn = sum(r["churn"] for r in rows)
        base = sum(r["start"] for r in rows if r["month"] == "2026-07")
        self.assert_close("quarter_churn_2026_q3", round(100 * churn / base, 2))
        self.assert_close("monthly_churn_2026_q3", self.rate(rows))
        note = read(EXAMPLES / "retention-smb" / "extra" / "finance-note.docx").blocks
        shown = str(self.expected["quarter_churn_2026_q3"]["value"]).replace(".", ",")
        self.assertTrue(any(shown in block["text"] for block in note))

    def test_requirement_is_impossible_on_the_data(self):
        item = control("retention-smb")["impossible"]
        months = sorted({r["month"] for r in self.rows})
        most_new = max(sum(r["new"] for r in self.rows if r["month"] == m) for m in months)
        self.assertEqual(item["historic_max_new_clients"], most_new)
        ends = {
            month: sum(r["start"] + r["new"] - r["churn"] for r in self.rows if r["month"] == month)
            for month in ("2025-12", "2026-09")
        }
        last_churn = sum(r["churn"] for r in self.rows if r["month"] == "2026-09")
        self.assertEqual(item["required_new_clients"], ends["2025-12"] - ends["2026-09"] + last_churn)
        self.assertGreater(item["required_new_clients"], most_new)


class Revision(unittest.TestCase):
    path = EXAMPLES / "retention-smb" / "extra" / "metrics-2026-10.xlsx"

    @classmethod
    def setUpClass(cls):
        cls.item = control("retention-smb")["revision"]
        cls.expected = {entry["id"]: entry for entry in cls.item["numbers"]}
        blocks = read(cls.path).blocks
        cls.blocks = blocks
        cls.rows = []
        for block in blocks:
            if block["locator"]["sheet"] != "Сегменты":
                continue
            cells = block["cells"]
            number = re.search(r"\d+", next(iter(cells))).group()
            if number != "1":
                get = lambda column: cells.get(f"{column}{number}")
                cls.rows.append({
                    "sheet_row": int(number), "month": get("A"), "segment": get("B"), "start": get("C"),
                    "new": get("D"), "churn": get("E"), "tariff": get("F"),
                })

    def rate(self, segment):
        chosen = [row for row in self.rows if row["segment"] == segment]
        return round(100 * sum(r["churn"] for r in chosen) / sum(r["start"] for r in chosen), 2)

    def test_material_is_marked_and_readable(self):
        self.assertEqual(read(self.path).status, "ok")
        self.assertTrue(any("Учебные данные" in block["text"] for block in self.blocks))

    def test_continues_the_same_table(self):
        old = segment_table()
        header = next(b for b in self.blocks if b["locator"]["sheet"] == "Сегменты" and "A1" in b["cells"])
        self.assertEqual(
            list(header["cells"].values()),
            ["Месяц", "Сегмент", "Активных на начало", "Новых", "Ушедших", "Тариф, руб./мес."],
        )
        self.assertEqual({row["segment"] for row in self.rows}, set(SEGMENTS))
        self.assertTrue(all(row["month"] > "2026-09" for row in self.rows))
        for segment in SEGMENTS:
            last = [r for r in old if r["segment"] == segment][-1]
            mine = [r for r in self.rows if r["segment"] == segment]
            self.assertEqual(mine[0]["start"], last["start"] + last["new"] - last["churn"])
            for before, after in zip(mine, mine[1:]):
                self.assertEqual(after["start"], before["start"] + before["new"] - before["churn"])
            self.assertEqual({r["tariff"] for r in mine}, {last["tariff"]})

    def test_revision_material_matches_control(self):
        for segment, slug in ((SOLO, "solo"), (SMALL, "small")):
            item = self.expected[f"churn_{slug}_next_period"]
            self.assertAlmostEqual(self.rate(segment), item["value"], delta=item["tolerance"])
            numbers = [int(n) for n in item["where"].split("строки ")[1].split(", ")]
            located = [r for r in self.rows if r["sheet_row"] in numbers]
            self.assertTrue(located and all(r["segment"] == segment for r in located))
            self.assertEqual(len(located), len(numbers))

    def test_one_conclusion_is_refuted_and_another_confirmed(self):
        base = values("retention-smb")
        solo_new = self.expected["churn_solo_next_period"]["value"]
        self.assertLess(solo_new, base["churn_solo_after"]["value"] - 2)
        self.assertLess(abs(solo_new - base["churn_solo_before"]["value"]), 0.5)
        small_new = self.expected["churn_small_next_period"]["value"]
        self.assertLess(abs(small_new - base["churn_small_after"]["value"]), 0.5)
        self.assertLess(small_new, base["churn_small_2026_03"]["value"] / 2)
        self.assertEqual(len(self.item["contested"]), 1)
        self.assertEqual(len(self.item["confirmed"]), 1)
        self.assertTrue(self.item["affected"] and self.item["unaffected"] and self.item["review_condition"])

    def test_new_period_is_not_among_the_materials(self):
        self.assertTrue(self.path.is_file())
        self.assertFalse((EXAMPLES / "retention-smb" / "materials" / self.path.name).exists())
        self.assertEqual(self.item["material"], "extra/" + self.path.name)

    def test_card_has_the_revision_section(self):
        text = (ACCEPTANCE / "retention-smb.md").read_text(encoding="utf-8")
        self.assertIn("## Пересмотр: новые данные", text)
        self.assertIn("metrics-2026-10.xlsx", text)


class Direction(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.rows = funnel_table()
        cls.expected = values("direction-b2c")

    def picked(self, direction):
        return [
            row for row in self.rows
            if row["Направление"] == direction and int(row["Неделя"]) <= 11 and int(row["Показано"]) > 0
        ]

    def total(self, rows, column):
        return sum(int(row[column]) for row in rows)

    def assert_close(self, key, actual):
        item = self.expected[key]
        self.assertAlmostEqual(actual, item["value"], delta=item["tolerance"], msg=key)

    def test_conversion_depends_on_the_base(self):
        for direction, slug in (("Дневник питомца", "diary"), ("Чат с ветеринаром", "chat")):
            rows = self.picked(direction)
            paid = self.total(rows, "Оплатили")
            self.assert_close(f"conversion_{slug}_own_base", round(100 * paid / self.total(rows, "Показано"), 2))
            self.assert_close(f"conversion_{slug}_common_base", round(100 * paid / self.total(rows, "Активных всего"), 2))
            margin = self.total(rows, "Выручка, руб.") - self.total(rows, "Прямые затраты, руб.")
            self.assert_close(f"margin_{slug}_per_payer", round(margin / paid))

    def test_bases_differ_between_directions(self):
        bases = {row["Направление"]: row["База показа"] for row in self.rows}
        self.assertNotEqual(bases["Дневник питомца"], bases["Чат с ветеринаром"])

    def test_zero_base_and_partial_week_are_visible(self):
        paused = [r for r in self.rows if r["Направление"] == "Напоминания" and r["Неделя"] == "7"]
        self.assertEqual(paused[0]["Показано"], "0")
        week_12 = [r for r in self.rows if r["Неделя"] == "12"]
        week_11 = [r for r in self.rows if r["Неделя"] == "11"]
        self.assertTrue(all(r["Примечание"] for r in week_12))
        self.assertLess(self.total(week_12, "Оплатили"), self.total(week_11, "Оплатили") / 2)

    def test_partial_week_changes_the_totals(self):
        for direction, slug in (("Дневник питомца", "diary"), ("Чат с ветеринаром", "chat")):
            self.assert_close(f"paid_{slug}_complete_weeks", self.total(self.picked(direction), "Оплатили"))
            everything = [r for r in self.rows if r["Направление"] == direction]
            self.assertGreater(self.total(everything, "Оплатили"), self.expected[f"paid_{slug}_complete_weeks"]["value"])

    def test_board_note_uses_another_period_and_base(self):
        rows = [r for r in self.picked("Чат с ветеринаром") if int(r["Неделя"]) <= 8]
        rate = round(100 * self.total(rows, "Оплатили") / self.total(rows, "Активных всего"), 2)
        self.assert_close("board_chat_common_base", rate)
        note = read(EXAMPLES / "direction-b2c" / "extra" / "board-note.docx").blocks
        shown = str(self.expected["board_chat_common_base"]["value"]).replace(".", ",")
        self.assertTrue(any(shown in block["text"] for block in note))

    def test_survey_numbers_come_from_the_report(self):
        pages = read(EXAMPLES / "direction-b2c" / "materials" / "pilot-report.pdf").blocks
        first = next(b["text"] for b in pages if b["locator"]["page"] == 1)
        invited, answered = (int(item) for item in re.search(r"(\d{4}) .*?ответили (\d+)", first, re.S).groups())
        self.assert_close("survey_sample", answered)
        self.assert_close("survey_response_rate", round(100 * answered / invited, 2))

    def test_report_states_the_own_base_figure(self):
        pages = read(EXAMPLES / "direction-b2c" / "materials" / "pilot-report.pdf").blocks
        shown = str(self.expected["conversion_chat_own_base"]["value"]).replace(".", ",")
        self.assertTrue(any(shown in block["text"] for block in pages))

    def test_requirement_is_impossible_on_the_data(self):
        item = control("direction-b2c")["impossible"]
        weeks = {row["Неделя"] for row in self.rows if int(row["Неделя"]) <= 11}
        margins = [
            sum(
                int(r["Выручка, руб."]) - int(r["Прямые затраты, руб."]) for r in self.rows
                if r["Неделя"] == week and int(r["Выручка, руб."]) > int(r["Прямые затраты, руб."])
            )
            for week in weeks
        ]
        self.assertEqual(item["best_month_margin_rub"], max(margins) * 4)
        self.assertLess(item["best_month_margin_rub"], item["fixed_monthly_rub"])


class Records(unittest.TestCase):
    def test_every_number_is_fully_described(self):
        for case in ("retention-smb", "direction-b2c"):
            for item in control(case)["numbers"]:
                for field in ("id", "label", "value", "unit", "period", "formula", "where", "rounding", "tolerance"):
                    self.assertIn(field, item, f"{case}: {item.get('id')}")

    def test_site_facts_have_date_and_limit(self):
        for case in ("retention-smb", "direction-b2c"):
            facts = control(case)["site_facts"]
            self.assertTrue(facts)
            for fact in facts:
                self.assertTrue(fact["url"].startswith("https://"))
                self.assertTrue(fact["checked_on"] and fact["limit"] and fact["section"])

    def test_cases_differ(self):
        def names(case):
            return {path.name for path in (EXAMPLES / case / "materials").iterdir()}
        self.assertFalse(names("retention-smb") & names("direction-b2c"))
        suffixes = {path.suffix for case in ("retention-smb", "direction-b2c") for path in (EXAMPLES / case / "materials").iterdir()}
        self.assertEqual(suffixes, {".xlsx", ".docx", ".pdf", ".csv"})


if __name__ == "__main__":
    unittest.main()
