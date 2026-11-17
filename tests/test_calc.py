import contextlib
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))

import calc  # noqa: E402
import calc_engine as engine  # noqa: E402
import evidence  # noqa: E402
import read_material  # noqa: E402
import validate_project  # noqa: E402
from calc_engine import CalcError, Formula  # noqa: E402

EXAMPLES = ROOT / "examples"
CONTROL = ROOT / "docs" / "acceptance" / "control"
OK, MISMATCH, REFUSED = 0, 1, 4
CALCS = "evidence/calculations.json"


def control(case_id: str) -> dict:
    data = json.loads((CONTROL / f"{case_id}.json").read_text(encoding="utf-8"))
    return {item["id"]: item for item in data["numbers"] + data.get("derivable", [])} | {"impossible": data["impossible"]}


class Numbers(unittest.TestCase):
    def test_cell_text(self):
        for text, value in (("1440", 1440), (" 1 440 ", 1440), ("1 440,5", 1440.5), ("−12", -12),
                            ("3,34%", 3.34), ("0.5", 0.5), ("+7", 7)):
            with self.subTest(text):
                self.assertEqual(engine.parse_cell_text(text), value)
        for text in ("", "abc", "1.234,5", "12 34", "2025-01", "1e3", "12 руб."):
            with self.subTest(text):
                self.assertIsNone(engine.parse_cell_text(text))

    def test_numbers_in_text(self):
        self.assertEqual(engine.numbers_in_text("ответили 212"), [212])
        self.assertEqual(engine.numbers_in_text("составила 1,52% от всех"), [1.52])
        self.assertEqual(engine.numbers_in_text("за 8 недель 1,52%"), [8, 1.52])
        self.assertEqual(engine.numbers_in_text("выручка 1 234 567 руб."), [1234567])
        self.assertEqual(engine.numbers_in_text("рост на -5 пунктов, COVID-19"), [-5, 19])
        self.assertEqual(engine.numbers_in_text("ID A12 и версия 1.2.3"), [])

    def test_a_dash_before_a_number_is_not_read_as_plus(self):
        with self.assertRaises(CalcError) as caught:
            engine.numbers_in_text("изменение –5%")
        self.assertEqual(caught.exception.code, "ambiguous_sign")
        self.assertEqual(engine.numbers_in_text("недели 5–7"), [5, 7])
        self.assertEqual(engine.numbers_in_text("недели 5 – 7"), [5, 7])
        self.assertIsNone(engine.parse_cell_text("–5"))

    def test_huge_values_are_refused_not_a_crash(self):
        for value in (10 ** 400, 1e301, -1e301):
            with self.subTest(value=value), self.assertRaises(CalcError) as caught:
                engine.round_half_up(value, 6)
            self.assertEqual(caught.exception.code, "overflow")
        self.assertEqual(engine.round_half_up(1e30, 6), 10 ** 30)
        self.assertEqual(engine.round_half_up(1.5e26, 10), 150000000000000000000000000)
        self.assertEqual(engine.round_half_up(engine.Decimal("123456789012345678901234567.25"), 1),
                         float("123456789012345678901234567.3"))

    def test_round_half_up_keeps_integers_whole(self):
        self.assertEqual(engine.round_half_up(2.675, 2), 2.68)
        self.assertEqual(engine.round_half_up(-2.5, 0), -3)
        self.assertEqual(engine.round_half_up(120.0, 6), 120)
        self.assertIsInstance(engine.round_half_up(120.0, 6), int)
        self.assertEqual(engine.round_half_up(10 ** 20, 2), 10 ** 20)


class Formulas(unittest.TestCase):
    def test_only_arithmetic_over_names_is_allowed(self):
        for text in ("a ** 2", "a if b else c", "abs(a)", "a.real", "a[0]", "'1' + a", "a < b", "(a := 1)",
                     "__import__('os')", "lambda: 1", "[a]", "a and b", "True + a", "1j * a", "f'{a}'", "a % b"):
            with self.subTest(text), self.assertRaises(CalcError) as caught:
                Formula(text)
            self.assertIn(caught.exception.code, ("formula_forbidden", "formula_syntax"))
        self.assertEqual(Formula("100 * (a - b) / -c").used(), {"a", "b", "c"})

    def test_limits_and_syntax(self):
        for text, code in (("", "formula_syntax"), ("a +", "formula_syntax"), ("a" + " + a" * 300, "formula_too_long")):
            with self.subTest(code), self.assertRaises(CalcError) as caught:
                Formula(text)
            self.assertEqual(caught.exception.code, code)

    def test_overflow_is_refused_not_a_crash(self):
        big = "1" * 200
        for text in ("1e308 * 10", f"{big} * {big}", "a * 1e300 * 1e300"):
            with self.subTest(text[:12]), self.assertRaises(CalcError) as caught:
                engine.finish_value(Formula(text).evaluate({"a": 10})[0], 2)
            self.assertEqual(caught.exception.code, "overflow")
        with self.assertRaises(CalcError) as caught:
            Formula("1" * 400)
        self.assertEqual(caught.exception.code, "formula_forbidden")

    def test_zero_denominator_is_a_result(self):
        self.assertEqual(Formula("a / (b - b)").evaluate({"a": 1, "b": 2}), (None, "b - b"))
        self.assertEqual(Formula("a / b").evaluate({"a": 1, "b": 4}), (engine.Decimal("0.25"), None))

    def test_shares_that_add_up_to_the_whole_leave_an_exact_zero(self):
        values = {"a": 1, "b": 0.3, "c": 0.1, "d": 0.2}
        self.assertEqual(Formula("a / (b - c - d)").evaluate(values), (None, "b - c - d"))
        self.assertEqual(engine.aggregate([0.1, 0.2], "sum"), 0.3)
        self.assertEqual(engine.finish_value(Formula("a / 3").evaluate({"a": 1})[0], 4), 0.3333)

    def test_units_must_match_in_sums_and_cancel_in_ratios(self):
        units = {"a": "тыс. руб.", "b": "руб.", "c": "Руб.", "n": "клиентов", "m": "клиентов"}
        periods = dict.fromkeys(units, "2026-09")
        with self.assertRaises(CalcError) as caught:
            Formula("a + b").mixes(units, periods)
        self.assertEqual(caught.exception.code, "unit_mismatch")
        self.assertEqual(Formula("b - c").mixes(units, periods), [])
        self.assertEqual(Formula("1 - n / m").dimension(units, periods), "безразмерно")
        self.assertEqual(Formula("b * n / m").dimension(units, periods), "руб.")
        self.assertEqual(Formula("b / n").dimension(units, periods), "руб. / клиентов")

    def test_periods_mix_in_sums_and_ratios_but_not_in_products(self):
        units = dict.fromkeys(("a", "b", "c", "t"), "клиентов")
        periods = {"a": "2026-04", "b": "2026-03", "c": "2026-03 ", "t": None}
        self.assertEqual(len(Formula("a - b").mixes(units, periods)), 1)
        self.assertEqual(len(Formula("a / b").mixes(units, periods)), 1)
        self.assertEqual(Formula("a * b").mixes(units, periods), [])
        self.assertEqual(Formula("b + c").mixes(units, periods), [])
        self.assertEqual(Formula("a + t").mixes(units, periods), [])
        self.assertEqual(len(Formula("(a + t) / b").mixes(units, periods)), 1)


class Base(unittest.TestCase):
    """A case built by the real material tool, so that block numbers and values are the real ones."""

    def setUp(self):
        folder = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(folder.cleanup)
        self.case = Path(folder.name) / "case"
        self.case.mkdir()
        (self.case / "case.json").write_text(json.dumps({
            "format_version": 1, "case_id": "case", "title": "Учебный кейс", "created_at": "2026-11-06T10:00:00+03:00",
        }), encoding="utf-8")
        self.requests = 0

    @staticmethod
    def call(main, args):
        raw = io.BytesIO()
        out = io.TextIOWrapper(raw, encoding="utf-8", newline="\n")
        with contextlib.redirect_stdout(out):
            code = main([str(a) for a in args])
        out.flush()
        lines = raw.getvalue().decode("utf-8").rstrip("\n").split("\n")
        return code, json.loads(lines[0]), lines

    def register(self, path, *extra):
        code, report, _ = self.call(read_material.main, ["add", self.case, path, "--title", "Материал",
                                                         "--origin", "own_training", "--allowed", "yes", *extra])
        self.assertEqual(code, OK, report)
        return report["id"]

    def blocks(self, source_id):
        lines = self.case.joinpath("sources", "extracted", f"{source_id}.jsonl").read_text(encoding="utf-8")
        return [json.loads(line) for line in lines.splitlines() if line.strip()]

    def xlsx_n(self, source_id, sheet, row):
        for block in self.blocks(source_id):
            if block["locator"]["sheet"] == sheet and engine.parse_range(block["locator"]["range"])[1] == row:
                return block["n"]
        raise AssertionError(f"no row {row}")

    def request(self, data):
        self.requests += 1
        folder = self.case / "sources" / "incoming"
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / f"r{self.requests}.json"
        path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        return path

    def add(self, data):
        path = self.request(data)
        code, report, lines = self.call(calc.main, ["add", self.case, path])
        return code, report, path, lines

    def computed(self, data):
        code, report, _, _ = self.add(data)
        self.assertEqual(code, OK, report)
        return report

    def registry(self):
        return json.loads((self.case / CALCS).read_text(encoding="utf-8"))["records"]

    def validate(self):
        report = validate_project.validate_case(self.case)
        return sorted((f.code, f.record_id) for f in report.errors), sorted((f.code, f.record_id) for f in report.warnings)

    def assert_refused(self, result, code):
        status, report, path, _ = result
        self.assertEqual((status, report.get("status"), report.get("code")), (REFUSED, "refused", code), report)
        self.assertTrue(path.exists(), "a refused request stays for correction")


def cells(source, block, column, unit, period, name, block_to=None, how=None):
    item = {"name": name, "source_id": source, "block": block, "column": column, "unit": unit, "period": period}
    if block_to is not None:
        item["block_to"] = block_to
    if how is not None:
        item["aggregate"] = how
    return item


def quote(source, block, text, unit, period, name):
    return {"name": name, "source_id": source, "block": block, "quote": text, "unit": unit, "period": period}


def from_calc(calc_id, name):
    return {"name": name, "calculation_id": calc_id}


def request(inputs, formula, unit, period, digits=None, **extra):
    result = {"unit": unit, "period": period} | ({"digits": digits} if digits is not None else {})
    return {"description": "Расчёт", "inputs": inputs, "formula": formula, "result": result} | extra


SOLO, SMALL, LARGE = (2, 11, 20), (20, 29, 38), (38, 47, 56)   # first row before, first row after, row after the end
SHEET = "Сегменты"


class Retention(Base):
    """Every control number of the retention case, computed by the tool from the real workbook."""

    def setUp(self):
        super().setUp()
        self.src = self.register(EXAMPLES / "retention-smb" / "materials" / "metrics.xlsx")
        self.note = self.register(EXAMPLES / "retention-smb" / "extra" / "finance-note.docx")
        self.expected = control("retention-smb")

    def n(self, row, sheet=SHEET):
        return self.xlsx_n(self.src, sheet, row)

    def column(self, letter, first, last, period, name, how="sum", unit="клиентов"):
        return cells(self.src, self.n(first), letter, unit, period, name, self.n(last), how)

    def cell(self, letter, row, period, name, unit="клиентов", sheet=SHEET):
        return cells(self.src, self.xlsx_n(self.src, sheet, row), letter, unit, period, name)

    def close(self, key, report):
        item = self.expected[key]
        self.assertEqual(report["result"]["status"], "computed", key)
        self.assertAlmostEqual(report["result"]["value"], item["value"], delta=item.get("tolerance", 0.05), msg=key)

    def churn(self, first, last, period):
        return request([self.column("E", first, last, period, "ушедшие"), self.column("C", first, last, period, "база")],
                       "100 * ушедшие / база", "%", period, 2)

    def test_churn_before_and_after_the_price_change(self):
        for slug, (before, after, end) in (("solo", SOLO), ("small", SMALL), ("large", LARGE)):
            with self.subTest(slug):
                self.close(f"churn_{slug}_before", self.computed(self.churn(before, after - 1, "2025-04..2025-12")))
                if slug != "solo":
                    self.close(f"churn_{slug}_after", self.computed(self.churn(after, end - 1, "2026-01..2026-09")))
        self.close("churn_small_2026_03", self.computed(self.churn(31, 31, "2026-03")))

    def test_a_gap_is_refused_until_the_range_is_split_around_it(self):
        period = "2026-01..2026-09 без 2026-02 и 2026-03"
        refused = self.add(self.churn(11, 19, period))
        self.assert_refused(refused, "empty_cell")
        self.assertEqual(refused[1]["cells"], ["E12"])
        split = request([self.column("E", 11, 11, period, "е1", how="value"), self.column("E", 14, 19, period, "е2"),
                         self.column("C", 11, 11, period, "с1", how="value"), self.column("C", 14, 19, period, "с2")],
                        "100 * (е1 + е2) / (с1 + с2)", "%", period, 2, gaps=["Нет оттока за 2026-02 и начала 2026-03"])
        self.close("churn_solo_after", self.computed(split))

    def test_revenue_contribution_and_average_check_through_a_chain(self):
        ends, mrr = {}, {}
        for month, rows in (("2025-12", (10, 28, 46)), ("2026-09", (19, 37, 55))):
            inputs, terms = [], []
            for number, row in enumerate(rows, 1):
                inputs += [self.cell("C", row, month, f"н{number}"), self.cell("D", row, month, f"п{number}"),
                           self.cell("E", row, month, f"у{number}"), self.cell("F", row, month, f"т{number}", "руб./мес.")]
                terms.append(f"(н{number} + п{number} - у{number}) * т{number}")
            mrr[month] = self.computed(request(inputs, " + ".join(terms), "руб.", month, 0))
            self.close(f"mrr_{month.replace('-', '_')}", mrr[month])
        for slug, row, cost_row in (("solo", 19, 2), ("small", 37, 3), ("large", 55, 4)):
            end = self.computed(request([self.cell("C", row, "2026-09", "н"), self.cell("D", row, "2026-09", "п"),
                                         self.cell("E", row, "2026-09", "у")], "н + п - у", "клиентов", "2026-09"))
            ends[slug] = end["id"]
            contribution = self.computed(request(
                [from_calc(end["id"], "клиенты"), self.cell("F", row, "2026-09", "тариф", "руб./мес."),
                 self.cell("D", cost_row, None, "затраты", "руб./мес.", "Тарифы и затраты")],
                "клиенты * (тариф - затраты)", "руб.", "2026-09", 0))
            self.close(f"contribution_{slug}_2026_09", contribution)
        clients = self.computed(request([from_calc(ends[s], s) for s in ends], "solo + small + large", "клиентов", "2026-09"))
        arpa = self.computed(request([from_calc(mrr["2026-09"]["id"], "выручка"), from_calc(clients["id"], "клиенты")],
                                     "выручка / клиенты", "руб./мес.", "2026-09", 0))
        self.close("arpa_2026_09", arpa)
        self.assertEqual(arpa["dimension"], "руб. / клиентов")

    def test_quarter_churn_needs_an_explanation_of_the_other_base(self):
        quarter, start = "2026-07..2026-09", "2026-07"
        churned = [self.column("E", first, first + 2, quarter, f"у{n}") for n, first in enumerate((17, 35, 53), 1)]
        starts = [self.column("C", first, first, start, f"н{n}", how="value") for n, first in enumerate((17, 35, 53), 1)]
        data = request(churned + starts, "100 * (у1 + у2 + у3) / (н1 + н2 + н3)", "%", quarter, 2)
        refused = self.add(data)
        self.assert_refused(refused, "period_mix")
        self.assertEqual(refused[1]["operations"][0]["operation"], "/")
        quarterly = self.computed(data | {"period_mix": "Отток квартала относится к базе на его начало"})
        self.close("quarter_churn_2026_q3", quarterly)
        record = next(r for r in self.registry() if r["id"] == quarterly["id"])
        self.assertIn("Разные периоды: Отток квартала относится к базе на его начало", record["assumptions"])
        monthly = [self.column("C", first, first + 2, quarter, f"н{n}") for n, first in enumerate((17, 35, 53), 1)]
        self.close("monthly_churn_2026_q3", self.computed(
            request(churned + monthly, "100 * (у1 + у2 + у3) / (н1 + н2 + н3)", "%", quarter, 2)))
        explained = self.add(request(churned + monthly, "100 * (у1 + у2 + у3) / (н1 + н2 + н3)", "%", quarter, 2,
                                     period_mix="лишнее"))
        self.assert_refused(explained, "period_mix_unused")

    def test_the_finance_note_figure_comes_from_its_quote(self):
        n = next(b["n"] for b in self.blocks(self.note) if "14,42%" in b["text"])
        report = self.computed(request([quote(self.note, n, "14,42%", "%", "2026-07..2026-09", "заметка")],
                                       "заметка", "%", "2026-07..2026-09", 2))
        self.close("quarter_churn_2026_q3", report)
        two = self.add(request([quote(self.note, n, "за III квартал 2026 года составил 14,42%", "%", "q", "з")],
                               "з", "%", "q"))
        self.assert_refused(two, "quote_numbers")

    def test_gaps_follow_from_the_balance_with_a_stated_assumption(self):
        why = "Начало апреля равно концу марта"
        march = self.computed(request([self.cell("C", 14, "2026-04", "апрель"), self.cell("D", 13, "2026-03", "новые"),
                                       self.cell("E", 13, "2026-03", "ушедшие")],
                                      "апрель - новые + ушедшие", "клиентов", "2026-03", period_mix=why))
        self.close("start_solo_2026_03", march)
        february = self.computed(request([self.cell("C", 12, "2026-02", "начало"), self.cell("D", 12, "2026-02", "новые"),
                                          from_calc(march["id"], "март")],
                                         "начало + новые - март", "клиентов", "2026-02", period_mix=why))
        self.close("churn_solo_2026_02", february)
        lifetime = self.computed(request([self.column("C", 2, 10, "2025-04..2025-12", "активные"),
                                          self.column("E", 2, 10, "2025-04..2025-12", "ушедшие")],
                                         "активные / ушедшие", "мес.", "2025-04..2025-12", 1))
        self.close("lifetime_solo_before", lifetime)
        refused = self.add(request([self.cell("B", 2, None, "срок", "мес.", "Сводка")], "срок", "мес.", None))
        self.assert_refused(refused, "formula_no_value")

    def test_the_impossible_requirement_is_shown_by_the_gap(self):
        totals = {}
        for month, rows in (("2025-12", (10, 28, 46)), ("2026-09", (19, 37, 55))):
            inputs, terms = [], []
            for number, row in enumerate(rows, 1):
                inputs += [self.cell(letter, row, month, f"{letter}{number}") for letter in "CDE"]
                terms.append(f"(C{number} + D{number} - E{number})")
            totals[month] = self.computed(request(inputs, " + ".join(terms), "клиентов", month))
        churn = self.computed(request([self.cell("E", row, "2026-09", f"у{row}") for row in (19, 37, 55)],
                                      "у19 + у37 + у55", "клиентов", "2026-09"))
        needed = self.computed(request([from_calc(totals["2025-12"]["id"], "декабрь"),
                                        from_calc(totals["2026-09"]["id"], "сентябрь"), from_calc(churn["id"], "отток")],
                                       "декабрь - сентябрь + отток", "клиентов", "2026-10",
                                       period_mix="Разрыв между концом декабря и концом сентября плюс отток месяца"))
        self.assertEqual(needed["result"]["value"], self.expected["impossible"]["required_new_clients"])
        self.assertGreater(needed["result"]["value"], 0)
        self.assertEqual(self.validate()[0], [])

    def test_the_cause_share_is_recorded_as_not_computable(self):
        report = self.computed({"description": "Доля оттока по причинам", "inputs": [], "not_computable": True,
                                "result": {"unit": "%", "period": None}, "gaps": ["Причины ухода не записываются"]})
        self.assertEqual(report["result"]["status"], "not_computable")
        record = self.registry()[-1]
        self.assertNotIn("formula", record["method"])
        self.assertEqual(self.validate()[0], [])
        for data in ({"description": "x", "inputs": [], "not_computable": True, "result": {"unit": None, "period": None}},
                     {"description": "x", "inputs": [], "not_computable": True, "formula": "1",
                      "result": {"unit": None, "period": None}, "gaps": ["g"]}):
            status, answer, _, _ = self.add(data)
            self.assertEqual(status, REFUSED, answer)

    def test_table_inputs_refuse_text_headers_mixed_units_and_wrong_shapes(self):
        cases = [
            (request([self.column("E", 1, 3, "p", "x")], "x", None, "p"), "not_number"),
            (request([self.column("A", 2, 3, "p", "x")], "x", None, "p"), "not_number"),
            (request([self.cell("E", 2, "p", "a"), self.cell("F", 2, "p", "b", "руб.")], "a + b", None, "p"), "unit_mismatch"),
            (request([self.cell("E", 2, "p", "a"), self.cell("E", 3, "p", "b")], "a", None, "p"), "unused_input"),
            (request([self.cell("E", 2, "p", "a")], "a + b", None, "p"), "unknown_name"),
            (request([self.column("E", 2, 4, "p", "a", how="value")], "a", None, "p"), "aggregate_value_many"),
        ]
        for data, code in cases:
            with self.subTest(code):
                self.assert_refused(self.add(data), code)
        many = self.add(request([self.column("E", 2, 4, "p", "a", how=None)], "a", None, "p"))
        self.assertEqual((many[0], many[1]["reason"]), (REFUSED, "для нескольких строк укажите aggregate"))
        for column in ("e", "E1", 5):
            with self.subTest(column=column):
                bad = cells(self.src, self.n(2), column, None, "p", "a")
                self.assertEqual(self.add(request([bad], "a", None, "p"))[0], REFUSED)
        other_sheet = cells(self.src, self.n(2), "E", None, "p", "a", self.xlsx_n(self.src, "Сводка", 2), "sum")
        self.assertEqual(self.add(request([other_sheet], "a", None, "p"))[0], REFUSED)
        backwards = cells(self.src, self.n(5), "E", None, "p", "a", self.n(2), "sum")
        self.assertEqual(self.add(request([backwards], "a", None, "p"))[0], REFUSED)
        self.assertFalse((self.case / CALCS).exists())


class Direction(Base):
    """Every control number of the direction case: a cp1251 CSV, a PDF and a DOCX."""

    def setUp(self):
        super().setUp()
        self.funnel = self.register(EXAMPLES / "direction-b2c" / "materials" / "pilot-funnel.csv",
                                    "--encoding", "cp1251", "--delimiter", ";")
        self.report = self.register(EXAMPLES / "direction-b2c" / "materials" / "pilot-report.pdf")
        self.board = self.register(EXAMPLES / "direction-b2c" / "extra" / "board-note.docx")
        self.expected = control("direction-b2c")

    def close(self, key, report):
        item = self.expected[key]
        self.assertEqual(report["result"]["status"], "computed", key)
        self.assertAlmostEqual(report["result"]["value"], item["value"], delta=item["tolerance"], msg=key)

    def column(self, header, first, last, name, unit, period="недели 1–11"):
        return cells(self.funnel, first, header, unit, period, name, last, "sum")

    def test_conversion_depends_on_the_base_and_margin_is_per_payment(self):
        for slug, first in (("diary", 2), ("chat", 26)):
            with self.subTest(slug):
                last = first + 10
                paid = self.column("Оплатили", first, last, "оплаты", "оплат")
                shown = self.column("Показано", first, last, "показы", "оплат")
                active = self.column("Активных всего", first, last, "активные", "оплат")
                self.close(f"conversion_{slug}_own_base",
                           self.computed(request([paid, shown], "100 * оплаты / показы", "%", "недели 1–11", 2)))
                self.close(f"conversion_{slug}_common_base",
                           self.computed(request([paid, active], "100 * оплаты / активные", "%", "недели 1–11", 2)))
                revenue = self.column("Выручка, руб.", first, last, "выручка", "руб.")
                costs = self.column(8, first, last, "затраты", "руб.")
                self.close(f"margin_{slug}_per_payer", self.computed(
                    request([revenue, costs, paid], "(выручка - затраты) / оплаты", "руб./оплата", "недели 1–11", 0)))
                self.close(f"paid_{slug}_complete_weeks",
                           self.computed(request([paid], "оплаты", "оплат", "недели 1–11")))

    def test_the_board_figure_uses_eight_weeks_and_the_whole_base(self):
        paid = self.column("Оплатили", 26, 33, "оплаты", "оплат", "недели 1–8")
        active = self.column("Активных всего", 26, 33, "активные", "пользователей", "недели 1–8")
        self.close("board_chat_common_base",
                   self.computed(request([paid, active], "100 * оплаты / активные", "%", "недели 1–8", 2)))
        note = next(b["n"] for b in self.blocks(self.board) if "1,52%" in b["text"])
        self.close("board_chat_common_base", self.computed(
            request([quote(self.board, note, "1,52%", "%", "недели 1–8", "записка")], "записка", "%", "недели 1–8", 2)))
        whole = self.column("Активных всего", 26, 36, "активные", "пользователей")
        self.assert_refused(self.add(request([paid, whole], "100 * оплаты / активные", "%", "?", 2)), "period_mix")

    def test_survey_numbers_come_from_quotes_of_the_report(self):
        invited = quote(self.report, 1, "показано 5300", "пользователей", "опрос", "приглашены")
        answered = quote(self.report, 1, "ответили 212", "ответов", "опрос", "ответили")
        self.close("survey_sample", self.computed(request([answered], "ответили", "ответов", "опрос")))
        self.close("survey_response_rate", self.computed(
            request([invited, answered], "100 * ответили / приглашены", "%", "опрос", 2)))
        wrong = quote(self.report, 1, "ответили 213", "ответов", "опрос", "ответили")
        self.assert_refused(self.add(request([wrong], "ответили", None, "опрос")), "quote_not_found")
        table = quote(self.funnel, 2, "18400", None, "p", "x")
        self.assertEqual(self.add(request([table], "x", None, "p"))[0], REFUSED)
        text_column = cells(self.report, 1, 1, None, "p", "x")
        self.assertEqual(self.add(request([text_column], "x", None, "p"))[0], REFUSED)

    def test_zero_base_and_no_payments_are_not_computable(self):
        tried = cells(self.funnel, 20, "Попробовали", "пользователей", "неделя 7", "попробовали")
        shown = cells(self.funnel, 20, "Показано", "пользователей", "неделя 7", "показы")
        week = self.computed(request([tried, shown], "100 * попробовали / показы", "%", "неделя 7", 2))
        self.assertEqual(week["result"], {"status": "not_computable", "value": None, "unit": "%", "period": "неделя 7"})
        self.assertIn("Деление на ноль: знаменатель показы равен нулю", self.registry()[-1]["gaps"])
        revenue = self.column("Выручка, руб.", 14, 24, "выручка", "руб.")
        costs = self.column("Прямые затраты, руб.", 14, 24, "затраты", "руб.")
        paid = self.column("Оплатили", 14, 24, "оплаты", "оплат")
        margin = self.computed(request([revenue, costs, paid], "(выручка - затраты) / оплаты", "руб.", "недели 1–11"))
        self.assertEqual(margin["result"]["status"], "not_computable")
        self.assertEqual(self.validate()[0], [])
        refused = self.add(request([from_calc(margin["id"], "маржа")], "маржа * 2", "руб.", "недели 1–11"))
        self.assertEqual(refused[0], REFUSED)

    def test_header_must_be_unique_and_present(self):
        for header in ("Нет такого", "", 0, 11):
            with self.subTest(header=header):
                item = cells(self.funnel, 2, header, None, "p", "x")
                self.assertEqual(self.add(request([item], "x", None, "p"))[0], REFUSED)

    def test_output_carries_no_material_text(self):
        code, report, _, lines = self.add(request(
            [quote(self.report, 1, "ответили 212", "ответов", "опрос", "ответили")], "ответили", "ответов", "опрос"))
        self.assertEqual(code, OK)
        text = "\n".join(lines)
        for fragment in ("Опрос", "приглашение", "Хвостик", "Участие"):
            self.assertNotIn(fragment, text)
        failed = self.add(request([cells(self.funnel, 2, "Направление", None, "p", "x")], "x", None, "p"))
        self.assertNotIn("Дневник", json.dumps(failed[1], ensure_ascii=False))


class Records(Base):
    """The registry side: repeats, rollback, validator recomputation, withdrawal and check."""

    def setUp(self):
        super().setUp()
        self.src = self.register(EXAMPLES / "retention-smb" / "materials" / "metrics.xlsx")

    def churn(self):
        n2, n10 = self.xlsx_n(self.src, SHEET, 2), self.xlsx_n(self.src, SHEET, 10)
        period = "2025-04..2025-12"
        return request([cells(self.src, n2, "E", "клиентов", period, "ушедшие", n10, "sum"),
                        cells(self.src, n2, "C", "клиентов", period, "база", n10, "sum")],
                       "100 * ушедшие / база", "%", period, 2)

    def test_repeat_returns_the_same_record_and_removes_the_request(self):
        first = self.computed(self.churn())
        code, again, path, _ = self.add(self.churn())
        self.assertEqual((code, again["id"], again["existing"]), (OK, first["id"], True))
        self.assertFalse(path.exists())
        self.assertEqual(len(self.registry()), 1)
        record = self.registry()[0]
        self.assertEqual(record["reproduce"], f"tools/calc.py check projects/case {first['id']}")
        self.assertEqual(record["method"], {"kind": "tool", "description": "Расчёт",
                                            "formula": "100 * ушедшие / база", "digits": 2})

    def test_a_new_error_on_the_record_restores_the_registry(self):
        self.computed(self.churn())
        before = (self.case / CALCS).read_bytes()
        data = self.churn() | {"formula": "100 * база / ушедшие"}
        with mock.patch.object(validate_project.calc_engine, "recompute", return_value=(["forced"], True)):
            status, report, path, _ = self.add(data)
        self.assertEqual((status, report["errors"]), (REFUSED, ["CALC_MISMATCH"]))
        self.assertEqual((self.case / CALCS).read_bytes(), before)
        self.assertTrue(path.exists())

    def test_validator_recomputes_inputs_results_and_chains(self):
        first = self.computed(self.churn())
        chained = self.computed(request([from_calc(first["id"], "доля")], "доля / 100", None, "2025-04..2025-12", 4))
        self.assertEqual(self.validate()[0], [])
        original = (self.case / CALCS).read_bytes()

        def tamper(change):
            data = json.loads(original)
            change(data["records"])
            (self.case / CALCS).write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
            return self.validate()[0]

        mismatch = [("CALC_MISMATCH", first["id"])]
        self.assertEqual(tamper(lambda r: r[0]["inputs"][0].update(value=393)), mismatch)
        self.assertEqual(tamper(lambda r: r[0]["result"].update(value=3.35)),
                         mismatch + [("CALC_MISMATCH", chained["id"])])
        self.assertEqual(tamper(lambda r: r[0]["method"].update(formula="100 * ушедшие / база + 1")), mismatch)
        self.assertEqual(tamper(lambda r: r[0]["method"].update(digits=3)), mismatch)
        self.assertEqual(tamper(lambda r: r[0]["inputs"][0]["locator"].update(range="E2:E9")), mismatch)
        self.assertEqual(tamper(lambda r: r[0]["inputs"][0].update(aggregate="mean")), mismatch)
        self.assertEqual(tamper(lambda r: r[0]["result"].update(status="not_computable", value=None)),
                         mismatch + [("CALC_MISMATCH", chained["id"])])
        self.assertEqual(tamper(lambda r: r[0]["inputs"][0]["locator"].update(range="E2:F10")), mismatch)
        self.assertEqual(tamper(lambda r: r[1]["inputs"][0].update(unit="клиентов")), [("CALC_MISMATCH", chained["id"])])
        self.assertEqual(tamper(lambda r: r[0]["inputs"][0]["locator"].update(sheet="Нет")),
                         [("LOCATOR_NOT_FOUND", first["id"])])
        self.assertEqual(tamper(lambda r: r[0]["inputs"][0].update(source_id="SRC-0009")), [("ID_MISSING", first["id"])])

        def by_hand(records):
            records[0]["method"] = {"kind": "formula", "description": "вручную"}
        self.assertEqual(tamper(by_hand), [])
        self.assertIn(("CALC_UNVERIFIED", first["id"]), self.validate()[1])

    def test_records_the_validator_cannot_crash_on(self):
        first = self.computed(self.churn())
        original = json.loads((self.case / CALCS).read_text(encoding="utf-8"))

        def tamper(change):
            data = json.loads(json.dumps(original))
            change(data["records"][0])
            (self.case / CALCS).write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
            return self.validate()[0]

        mismatch = [("CALC_MISMATCH", first["id"])]
        self.assertEqual(tamper(lambda r: r["method"].update(formula="ушедшие * 1e30 / база")), mismatch)
        self.assertEqual(tamper(lambda r: r["method"].update(formula="ушедшие * 1e200 * 1e200 / база")), mismatch)
        self.assertEqual(tamper(lambda r: r["inputs"][0]["locator"].update(range="E2:E999999999")), mismatch)
        self.assertEqual(tamper(lambda r: r["inputs"][0].update(column="F")), mismatch)
        self.assertEqual(tamper(lambda r: r["inputs"][0].update(column="E")), [])
        big = self.add(request([cells(self.src, self.xlsx_n(self.src, SHEET, 2), "E", None, "p", "a")],
                               "a * 1e300", None, "p"))
        self.assert_refused(big, "overflow")

    def test_a_record_without_extracted_text_is_unverified_not_ok(self):
        first = self.computed(self.churn())
        data = json.loads((self.case / CALCS).read_text(encoding="utf-8"))
        (self.case / "sources" / "extracted" / f"{self.src}.jsonl").unlink()
        code, report, _ = self.call(calc.main, ["check", self.case])
        self.assertEqual((code, report["ok"], report["unverified"]), (OK, [], [first["id"]]))
        data["records"][0]["result"]["value"] = 4
        (self.case / CALCS).write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        self.assertIn(("CALC_MISMATCH", first["id"]), self.validate()[0])
        self.assertEqual(self.call(calc.main, ["check", self.case])[0], MISMATCH)

    def test_a_long_range_over_missing_rows_is_refused_quickly(self):
        n2 = self.xlsx_n(self.src, SHEET, 2)
        with self.assertRaises(CalcError) as caught:
            engine.xlsx_values(self.blocks(self.src), SHEET, "E", 2, 10 ** 9)
        self.assertEqual((caught.exception.code, caught.exception.details["count"]), ("empty_cell", 10 ** 9 - 1 - 54))
        self.assertEqual(len(caught.exception.details["cells"]), engine.MAX_LISTED_CELLS)
        self.assertEqual(caught.exception.details["cells"][0], "E56")
        self.assertGreater(n2, 0)

    def test_repeating_a_not_computable_request_creates_no_duplicate(self):
        data = {"description": "Доля оттока по причинам", "inputs": [], "not_computable": True,
                "result": {"unit": "%", "period": None}, "gaps": ["Причины ухода не записываются"]}
        first = self.computed(data)
        again = self.computed(data)
        self.assertEqual((again["id"], again["existing"]), (first["id"], True))
        other = self.computed(data | {"gaps": ["Опроса ушедших нет"]})
        self.assertNotEqual(other["id"], first["id"])

    def test_withdrawal_lowers_claims_through_the_chain_and_names_dependents(self):
        first = self.computed(self.churn())
        chained = self.computed(request([from_calc(first["id"], "доля")], "доля / 100", None, "2025-04..2025-12", 4))
        claims = {"format_version": 1, "records": [{
            "id": "CLM-0001", "created_at": "2026-11-06T10:00:00+03:00", "text": "Вывод", "kind": "inference",
            "status": "confirmed", "supports": [], "contradicts": [], "derived_from": [chained["id"]], "limits": None}]}
        (self.case / "evidence" / "claims.json").write_text(json.dumps(claims, ensure_ascii=False), encoding="utf-8")
        self.assertEqual(self.validate()[0], [])
        code, report, _ = self.call(evidence.main, ["withdraw", self.case, first["id"], "--reason", "Не та база"])
        self.assertEqual(code, OK, report)
        self.assertEqual((report["downgraded"], report["dependents"]["CALC"]), (["CLM-0001"], [chained["id"]]))
        withdrawn = self.registry()[0]
        self.assertEqual((withdrawn["record_status"], withdrawn["status_reason"]), ("withdrawn", "Не та база"))
        self.assertEqual(list(withdrawn)[:4], ["id", "created_at", "record_status", "status_reason"])
        self.assertIn(("WITHDRAWN_USED", chained["id"]), self.validate()[0])
        self.assertNotIn(("CALC_MISMATCH", chained["id"]), self.validate()[0])
        again = self.call(evidence.main, ["withdraw", self.case, first["id"], "--reason", "Не та база"])
        self.assertEqual((again[0], again[1]["already"]), (OK, True))
        refused = self.add(request([from_calc(first["id"], "доля")], "доля", None, None))
        self.assertEqual(refused[0], REFUSED)

    def test_check_reports_and_exits_by_verdict(self):
        first = self.computed(self.churn())
        code, report, _ = self.call(calc.main, ["check", self.case])
        self.assertEqual((code, report["ok"]), (OK, [first["id"]]))
        data = json.loads((self.case / CALCS).read_text(encoding="utf-8"))
        data["records"][0]["result"]["value"] = 4
        (self.case / CALCS).write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        code, report, _ = self.call(calc.main, ["check", self.case, first["id"]])
        self.assertEqual((code, [item["id"] for item in report["mismatch"]]), (MISMATCH, [first["id"]]))
        for args in (["check", self.case, "CALC-0099"], ["check", self.case, "CLM-0001"], ["check"]):
            with self.subTest(args=args[2:]):
                self.assertEqual(self.call(calc.main, args)[0], REFUSED)

    def test_trace_shows_formula_aggregate_and_column(self):
        first = self.computed(self.churn())
        code, report, lines = self.call(evidence.main, ["trace", self.case, first["id"]])
        self.assertEqual(code, OK, report)
        items = [json.loads(line) for line in lines[2:]]
        record = next(item for item in items if item.get("type") == "record")
        self.assertEqual(record["method"]["formula"], "100 * ушедшие / база")
        inputs = [item for item in items if item.get("aggregate")]
        self.assertEqual({item["aggregate"] for item in inputs}, {"sum"})

    def test_requests_are_checked_before_anything_is_read(self):
        bad = [
            {"description": "x", "inputs": [], "formula": "1", "result": {"unit": None, "period": None}},
            {"description": "x", "inputs": "a", "formula": "1", "result": {"unit": None, "period": None}},
            self.churn() | {"extra": 1},
            self.churn() | {"result": {"unit": "%", "period": None, "digits": 11}},
            self.churn() | {"inputs": [self.churn()["inputs"][0]] * 2},
            self.churn() | {"inputs": [self.churn()["inputs"][0] | {"name": "1x"}]},
            self.churn() | {"inputs": [self.churn()["inputs"][0] | {"name": "class"}]},
            self.churn() | {"inputs": [self.churn()["inputs"][0] | {"aggregate": "median"}]},
            self.churn() | {"inputs": [self.churn()["inputs"][0] | {"quote": "1"}]},
            self.churn() | {"inputs": [{k: v for k, v in self.churn()["inputs"][0].items() if k != "unit"}]},
            {"description": "x", "inputs": [], "result": {"unit": None, "period": None}},
        ]
        for number, data in enumerate(bad):
            with self.subTest(number):
                status, report, path, _ = self.add(data)
                self.assertEqual(status, REFUSED, report)
        outside = self.case.parent / "r.json"
        outside.write_text(json.dumps(self.churn(), ensure_ascii=False), encoding="utf-8")
        self.assertEqual(self.call(calc.main, ["add", self.case, outside])[0], REFUSED)
        self.assertFalse((self.case / CALCS).exists())


class Journal(Base):
    """With --environment calc add writes the action journal itself."""

    def setUp(self):
        super().setUp()
        self.src = self.register(EXAMPLES / "retention-smb" / "materials" / "metrics.xlsx")

    def churn(self):
        n2, n10 = self.xlsx_n(self.src, SHEET, 2), self.xlsx_n(self.src, SHEET, 10)
        period = "2025-04..2025-12"
        return request([cells(self.src, n2, "E", "клиентов", period, "ушедшие", n10, "sum"),
                        cells(self.src, n2, "C", "клиентов", period, "база", n10, "sum")],
                       "100 * ушедшие / база", "%", period, 2)

    def lines(self):
        path = self.case / "state" / "actions.jsonl"
        if not path.exists():
            return []
        return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]

    def add_recorded(self, data, *extra):
        path = self.request(data)
        code, report, _ = self.call(calc.main, ["add", self.case, path, "--environment", "claude_code",
                                                "--model", "m1", "--access", "subscription", *extra])[:3]
        return code, report, path

    def test_without_environment_the_journal_is_untouched(self):
        self.computed(self.churn())
        self.assertEqual(self.lines(), [])

    def test_a_computed_record_is_a_completed_compute_action(self):
        code, report, _ = self.add_recorded(self.churn())
        self.assertEqual(code, OK, report)
        start, end = self.lines()
        self.assertEqual((start["kind"], start["tool"], start["environment"], start["model"], start["input_refs"]),
                         ("compute", "calc.py add", "claude_code", "m1", []))
        self.assertEqual(start["runtime"], {"version": None, "access": "subscription", "cost_source": None})
        self.assertEqual((end["id"], end["outcome"], end["result_refs"]), (start["id"], "completed", [report["id"]]))
        self.assertEqual(self.validate()[0], [])

    def test_a_refusal_ends_failed_and_a_duplicate_returns_the_old_record(self):
        code, report, _ = self.add_recorded(self.churn() | {"formula": "100 * неизвестное / база"})
        self.assertEqual((code, report["code"]), (REFUSED, "unknown_name"))
        self.assertEqual([line.get("outcome") for line in self.lines()], [None, "failed"])
        first = self.add_recorded(self.churn())[1]
        code, again, _ = self.add_recorded(self.churn())
        self.assertEqual((code, again["id"], again["existing"]), (OK, first["id"], True))
        self.assertEqual(len(self.registry()), 1)
        lines = self.lines()
        self.assertEqual([(line["id"], line["phase"]) for line in lines[2:]],
                         [("ACT-0002", "start"), ("ACT-0002", "end"), ("ACT-0003", "start"), ("ACT-0003", "end")])
        self.assertEqual((lines[5]["outcome"], lines[5]["result_refs"]), ("completed", [first["id"]]))

    def test_a_retry_after_a_failure_is_linked(self):
        self.add_recorded(self.churn() | {"formula": "100 * неизвестное / база"})
        code, report, _ = self.add_recorded(self.churn(), "--retry-of", "ACT-0001", "--retry-reason", "Формула исправлена")
        self.assertEqual(code, OK, report)
        start, end = self.lines()[2:]
        self.assertEqual((start["retry_of"], start["retry_reason"], end["outcome"]),
                         ("ACT-0001", "Формула исправлена", "completed"))
        code, report, _ = self.add_recorded(self.churn(), "--retry-of", "ACT-0009", "--retry-reason", "x")
        self.assertEqual((code, report["status"]), (REFUSED, "refused"))
        self.assertEqual(len(self.lines()), 4)

    def test_a_wrong_call_does_not_touch_the_journal(self):
        path = self.request(self.churn())
        for argv in (["add", self.case, "--environment", "claude_code"],
                     ["add", self.case, path, "--environment", "Bad Name"],
                     ["add", self.case, path, "--environment", "x", "--access", "z"],
                     ["add", self.case.parent / "nope", path, "--environment", "claude_code"]):
            with self.subTest(argv[2:]):
                code, report, _ = self.call(calc.main, argv)[:3]
                self.assertEqual((code, report["status"]), (REFUSED, "refused"))
                self.assertEqual(self.lines(), [])

    def test_session_keys_without_environment_are_refused_before_any_work(self):
        path = self.request(self.churn())
        for key in (("--model", "m1"), ("--runtime-version", "1"), ("--access", "api"), ("--cost-source", "x"),
                    ("--retry-of", "ACT-0001"), ("--retry-reason", "r")):
            with self.subTest(key[0]):
                code, report, _ = self.call(calc.main, ["add", self.case, path, *key])[:3]
                self.assertEqual((code, report["status"]), (REFUSED, "refused"))
                self.assertIn("--environment", json.dumps(report, ensure_ascii=False))
                self.assertEqual(self.lines(), [])

    def test_an_internal_failure_leaves_the_action_without_an_end(self):
        path = self.request(self.churn())
        with mock.patch.object(calc, "evaluate", side_effect=RuntimeError("boom")):
            code, report, _ = self.call(calc.main, ["add", self.case, path, "--environment", "claude_code"])[:3]
        self.assertEqual((code, report["status"]), (5, "internal_error"))
        self.assertEqual([line["phase"] for line in self.lines()], ["start"])


if __name__ == "__main__":
    unittest.main()
