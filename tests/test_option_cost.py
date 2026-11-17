"""Cost and limit check of an option (E19): the marker `options_format` in case.json, the validator, the gate checks, the example."""
import contextlib
import copy
import io
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))
sys.path.insert(0, str(ROOT / "tests"))

import gate  # noqa: E402
import validate_project as vp  # noqa: E402
from test_contracts import CaseTestCase, EXAMPLES, VALID, codes, read_json  # noqa: E402
from test_gate import Base  # noqa: E402

OPTIONS = "decisions/options.json"
COST_EXAMPLE = EXAMPLES / "valid-cost"
CASE = "case.json"
PLAN = "outputs/validation-plan.md"
BASIS_SENTENCE = "; разброс показателя не посчитан [Q-0002]"

COSTS = {
    "OPT-0001": {"status": "none", "note": "Расходы на аренду прежние, новых затрат нет"},
    "OPT-0002": {"status": "none", "note": "Затрат нет, экономия аренды"},
    "OPT-0003": {"status": "estimated", "calc_ids": ["CALC-0001"],
                 "note": "Учтён объём заявок; стоимость пункта не названа"},
}
LIMITS = {
    "OPT-0001": {"brief_decision": None, "result": "no_limit", "note": "Бриф не называет бюджет, людей или срок"},
    "OPT-0002": {"brief_decision": None, "result": "no_limit", "note": "Бриф не называет бюджет, людей или срок"},
    "OPT-0003": {"brief_decision": None, "result": "unknown", "note": "Нет стоимости пункта, сравнить не с чем"},
}
NOT_ESTIMATED = {"status": "not_estimated", "reason": "Нет цены пункта", "question_id": "Q-0002"}


def make_v2(records):
    for record in records:
        record["cost"] = copy.deepcopy(COSTS[record["id"]])
        record["limit_check"] = copy.deepcopy(LIMITS[record["id"]])
        if record["id"] == "OPT-0003" and "CALC-0001" not in record["basis"]:
            record["basis"].append("CALC-0001")


class ValidatorMarker(CaseTestCase):
    """`options_format: 2` in case.json allows `cost` and `limit_check`; the registry stays version 1."""

    def v2(self, change=None):
        self.edit(CASE, lambda d: d.update(options_format=2))

        def edit(data):
            make_v2(data["records"])
            if change:
                change(data["records"])
        self.edit(OPTIONS, edit)

    def errors(self):
        return codes(self.run_case())[0]

    def test_case_without_the_marker_stays_valid(self):
        self.assertEqual(self.errors(), [])

    def test_marker_is_valid_with_the_fields(self):
        self.v2()
        self.assertEqual(self.errors(), [])
        self.assertEqual(read_json(self.case / OPTIONS)["format_version"], 1)

    def test_marker_is_valid_without_the_fields(self):
        self.edit(CASE, lambda d: d.update(options_format=2))
        self.assertEqual(self.errors(), [])

    def test_no_marker_refuses_the_new_fields(self):
        self.edit(OPTIONS, lambda d: make_v2(d["records"]))
        self.assertEqual(self.errors(), ["SCHEMA"] * 3)
        self.edit(OPTIONS, lambda d: [r.pop("limit_check") for r in d["records"]])
        self.assertEqual(self.errors(), ["SCHEMA"] * 3)

    def test_marker_value_must_be_two(self):
        for value in (1, 3, "2", True, None, 2.5):
            with self.subTest(value=value):
                self.edit(CASE, lambda d, v=value: d.update(options_format=v))
                self.assertEqual(self.errors(), ["SCHEMA"])

    def test_registry_version_2_is_unsupported_everywhere(self):
        self.edit(OPTIONS, lambda d: d.update(format_version=2))
        self.assertEqual(self.errors(), ["FORMAT_UNSUPPORTED"])
        self.v2()
        self.edit(OPTIONS, lambda d: d.update(format_version=2))
        self.assertEqual(self.errors(), ["FORMAT_UNSUPPORTED"])

    def test_cost_shapes(self):
        valid = [
            {"status": "estimated", "calc_ids": ["CALC-0001"], "note": "что вошло"},
            {"status": "none", "note": "затрат нет"},
            {"status": "not_estimated", "reason": "нет данных", "question_id": "Q-0002"},
        ]
        for cost in valid:
            with self.subTest(cost=cost["status"]):
                self.v2(lambda r, c=cost: r[0].update(cost=c))
                self.assertEqual(self.errors(), [])
        broken = [
            {"status": "estimated", "calc_ids": [], "note": "x"},
            {"status": "estimated", "calc_ids": ["CALC-0001"]},
            {"status": "estimated", "calc_ids": ["CLM-0001"], "note": "x"},
            {"status": "none"},
            {"status": "none", "note": "x", "calc_ids": ["CALC-0001"]},
            {"status": "not_estimated", "reason": "x"},
            {"status": "not_estimated", "question_id": "Q-0002"},
            {"status": "not_estimated", "reason": "x", "question_id": "CLM-0001"},
            {"status": "maybe", "note": "x"},
            {"status": "none", "note": ""},
            "none",
        ]
        for cost in broken:
            with self.subTest(cost=cost):
                self.v2(lambda r, c=cost: r[0].update(cost=c))
                self.assertEqual(self.errors(), ["SCHEMA"])

    def test_limit_check_shapes(self):
        valid = [
            {"brief_decision": "DEC-0002", "result": "fits", "calc_id": "CALC-0001"},
            {"brief_decision": "DEC-0002", "result": "fits"},  # the gate allows it only for cost.status none
            {"brief_decision": "DEC-0002", "result": "exceeds", "calc_id": "CALC-0001", "note": "x"},
            {"brief_decision": "DEC-0002", "result": "unknown", "note": "не хватает цены"},
            {"brief_decision": None, "result": "no_limit", "note": "бриф не называет"},
            {"brief_decision": "DEC-0002", "result": "fits", "calc_id": "CALC-0001", "compared": "direct_costs"},
            {"brief_decision": "DEC-0002", "result": "exceeds", "calc_id": "CALC-0001",
             "compared": "direct_costs_and_lost_revenue", "note": "Бюджет покрывает потери [бриф]"},
            {"brief_decision": "DEC-0002", "result": "fits", "compared": "direct_costs"},
        ]
        for limit in valid:
            with self.subTest(limit=limit["result"]):
                self.v2(lambda r, v=limit: r[0].update(limit_check=v))
                self.assertEqual(self.errors(), [])
        broken = [
            {"brief_decision": "DEC-0002", "result": "exceeds", "note": "x"},
            {"brief_decision": "DEC-0002", "result": "unknown"},
            {"brief_decision": "DEC-0002", "result": "no_limit"},
            {"brief_decision": "DEC-0002", "result": "unknown", "note": "x", "calc_id": "CALC-0001"},
            {"result": "unknown", "note": "x"},
            {"brief_decision": "OPT-0001", "result": "unknown", "note": "x"},
            {"brief_decision": "DEC-0002", "result": "mostly", "note": "x"},
            {"brief_decision": "DEC-0002", "result": "fits", "calc_id": "CLM-0001"},
            {"brief_decision": "DEC-0002", "result": "unknown", "note": "x", "extra": 1},
            {"brief_decision": "DEC-0002", "result": "unknown", "note": "x", "compared": "direct_costs"},
            {"brief_decision": None, "result": "no_limit", "note": "x", "compared": "direct_costs"},
            {"brief_decision": "DEC-0002", "result": "fits", "calc_id": "CALC-0001", "compared": "revenue"},
            {"brief_decision": "DEC-0002", "result": "fits", "calc_id": "CALC-0001", "compared": ""},
        ]
        for limit in broken:
            with self.subTest(limit=limit):
                self.v2(lambda r, v=limit: r[0].update(limit_check=v))
                self.assertEqual(self.errors(), ["SCHEMA"])

    def test_links_must_exist(self):
        cases = {
            "calc_ids": lambda r: r[2]["cost"].update(calc_ids=["CALC-0099"]),
            "question_id": lambda r: r[0].update(cost=dict(NOT_ESTIMATED, question_id="Q-0099")),
            "calc_id": lambda r: r[2].update(
                limit_check={"brief_decision": None, "result": "fits", "calc_id": "CALC-0099"}),
            "brief_decision": lambda r: r[0]["limit_check"].update(brief_decision="DEC-0099"),
        }
        for name, change in cases.items():
            with self.subTest(name):
                self.v2(change)
                self.assertEqual(self.errors(), ["ID_MISSING"])

    def test_links_to_withdrawn_records_are_refused(self):
        def withdraw(records):
            records["records"][2].update(record_status="withdrawn", status_reason="Отозван в тесте")
        self.edit("research/questions.json", withdraw)  
        self.v2(lambda r: r[0].update(cost=dict(NOT_ESTIMATED, question_id="Q-0003")))
        found = [(f.code, f.file) for f in self.run_case().errors]
        self.assertIn(("WITHDRAWN_USED", OPTIONS), found)

    def test_superseded_brief_decision_is_not_a_validator_error(self):
        self.v2(lambda r: r[0]["limit_check"].update(brief_decision="DEC-0001"))  # DEC-0001 is superseded
        self.assertEqual(self.errors(), [])

    def test_withdrawn_option_is_not_link_checked(self):
        def change(records):
            records[1].update(record_status="withdrawn", status_reason="Снят")
            records[1]["limit_check"].update(brief_decision="DEC-0099")
        self.v2(change)
        self.assertNotIn("ID_MISSING", self.errors())  # the text of options.md still names the withdrawn option


class CostExample(unittest.TestCase):
    def assemble(self):
        folder = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(folder.cleanup)
        case = Path(folder.name) / "demo-case"
        shutil.copytree(VALID, case)
        for path in COST_EXAMPLE.rglob("*"):
            if path.is_file():
                target = case / path.relative_to(COST_EXAMPLE)
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(path, target)
        return case

    def test_the_example_is_a_valid_case_with_the_marker(self):
        case = self.assemble()
        self.assertEqual(read_json(case / CASE)["options_format"], 2)
        self.assertEqual(read_json(case / OPTIONS)["format_version"], 1)
        report = vp.validate_case(case)
        self.assertEqual(report.errors, [])
        expected = {(w["code"], w["file"], w["id"])
                    for w in read_json(EXAMPLES / "valid" / "expected-warnings.json")["warnings"]}
        self.assertEqual({(w.code, w.file, w.record_id) for w in report.warnings}, expected)

    def test_the_example_has_cost_and_limit_check_everywhere(self):
        records = read_json(self.assemble() / OPTIONS)["records"]
        self.assertTrue(all("cost" in r and "limit_check" in r for r in records))
        self.assertIn("estimated", {r["cost"]["status"] for r in records if r["strategy_kind"] == "develop"})

    def test_the_example_passes_the_gate_on_done(self):
        raw = io.BytesIO()
        out = io.TextIOWrapper(raw, encoding="utf-8", newline="\n")
        with contextlib.redirect_stdout(out):
            code = gate.main(["check", str(self.assemble()), "done"])
        out.flush()
        report = json.loads(raw.getvalue().decode("utf-8").splitlines()[0])
        self.assertEqual((code, report["problems"]), (0, []))
        self.assertEqual([w["code"] for w in report["warnings"]], ["BASIS_UNCONFIRMED", "TEAM_NOT_ASKED"])

    def test_the_main_example_has_no_marker(self):
        self.assertNotIn("options_format", read_json(VALID / CASE))
        document = read_json(VALID / OPTIONS)
        self.assertEqual(document["format_version"], 1)
        self.assertTrue(all("cost" not in r and "limit_check" not in r for r in document["records"]))


class RegistryReaders(unittest.TestCase):
    """The tools that read registries accept only version 1, so a case with the marker must work with all of them."""

    def setUp(self):
        folder = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(folder.cleanup)
        self.tmp = Path(folder.name)

    def run_tool(self, tool, *args):
        process = subprocess.run([sys.executable, str(ROOT / "tools" / tool), *map(str, args)], capture_output=True)
        return process.returncode, (process.stdout + process.stderr).decode("utf-8", "replace")

    def not_refused_by_the_registry(self, tool, *args):
        code, output = self.run_tool(tool, *args)
        self.assertNotIn("реестр не прочитан", output, f"{tool} {args}: {output}")
        self.assertNotIn("FORMAT_UNSUPPORTED", output, f"{tool} {args}: {output}")
        return code, output

    def readers(self, case, record_id, request):
        return [
            ("evidence.py", "trace", case, record_id),
            ("evidence.py", "impact", case, record_id),
            ("revision.py", "affected", case, record_id),
            ("checkpoint.py", "save", case, "--from", request),
            ("checkpoint.py", "resume", case),
            ("evidence.py", "withdraw", case, record_id, "--reason", "проверка чтения реестров"),
        ]

    def request(self):
        path = self.tmp / "save.json"
        path.write_text(json.dumps({"stage": "research", "open_items": ["x"], "next_step": "y"}), encoding="utf-8")
        return path

    def test_case_made_by_case_new_is_not_refused_by_the_registry(self):
        import case as case_tool
        projects = self.tmp / "projects"
        with mock.patch.object(case_tool, "PROJECTS", projects):
            request = self.tmp / "new.json"
            request.write_text(json.dumps({"case_id": "fresh", "title": "Новый кейс"}), encoding="utf-8")
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(case_tool.main(["new", "--from", str(request)]), 0)
        case = projects / "fresh"
        self.assertEqual(read_json(case / CASE)["options_format"], 2)
        self.assertEqual(vp.validate_case(case).errors, [])
        for command in self.readers(case, "CLM-0001", self.request()):
            with self.subTest(command=command[:2]):
                self.not_refused_by_the_registry(*command)

    def test_case_with_the_marker_works_with_every_registry_tool(self):
        case = CostExample.assemble(self)
        for command in self.readers(case, "CLM-0001", self.request()):
            with self.subTest(command=command[:2]):
                code, output = self.not_refused_by_the_registry(*command)
                self.assertEqual(code, 0, output)

    def test_impact_of_a_question_lists_the_option_whose_cost_waits_for_it(self):
        case = CostExample.assemble(self)
        path = case / OPTIONS
        document = read_json(path)
        document["records"][2]["cost"] = dict(NOT_ESTIMATED)
        path.write_text(json.dumps(document, ensure_ascii=False, indent=2), encoding="utf-8")
        code, output = self.run_tool("evidence.py", "impact", case, "Q-0002")
        self.assertEqual(code, 0, output)
        self.assertIn("OPT-0003", output)
        document["records"][2]["cost"] = {"status": "none", "note": "Затрат нет"}
        path.write_text(json.dumps(document, ensure_ascii=False, indent=2), encoding="utf-8")
        self.assertNotIn("OPT-0003", self.run_tool("evidence.py", "impact", case, "Q-0002")[1])


class Gate(Base):
    """The gate on a case with the marker. Base is a finished case without it: nothing here is expected of it."""

    def v2(self, cost=None, limit=None):
        """Put the marker `options_format` into case.json and the new fields into the options; cost and limit map an option ID to a replacement (None drops it)."""
        def edit(records):
            make_v2(records)
            for record in records:
                for key, changes in (("cost", cost), ("limit_check", limit)):
                    if changes and record["id"] in changes:
                        if changes[record["id"]] is None:
                            record.pop(key)
                        else:
                            record[key] = copy.deepcopy(changes[record["id"]])
        self.change("OPT", edit)
        path = self.case / CASE
        document = json.loads(path.read_text(encoding="utf-8"))
        document["options_format"] = 2
        path.write_text(json.dumps(document, ensure_ascii=False, indent=2), encoding="utf-8")
        text = (self.case / PLAN).read_text(encoding="utf-8")
        if BASIS_SENTENCE not in text:
            self.replace(PLAN, "Не ниже 110 (допущение).", "Не ниже 110 (допущение)" + BASIS_SENTENCE + ".")

    def problem(self, code, stage="done"):
        _, report = self.check(stage)
        return [p for p in report["problems"] if p["code"] == code]

    def warning_codes(self, stage="done"):
        return {w["code"] for w in self.check(stage)[1]["warnings"]}

    def add_brief(self, number="DEC-0007", act="ACT-0015", supersedes=None):
        self.add_approval("brief", number, act, supersedes=supersedes)

    # --- the switch ---

    def test_case_without_the_marker_is_checked_as_before(self):
        for stage in ("options", "decision", "strategies", "validation_plan", "done"):
            with self.subTest(stage):
                self.assertEqual(self.codes(stage), set())

    def test_failures_without_the_marker_are_the_same_as_before(self):
        # What the parent commit (before E19) reports for this broken version-1 case.
        self.change("OPT", lambda r: r[0].update(risks=[]))
        self.replace(PLAN, "Не ниже 110 (допущение).", "Не ниже 110.")
        expected = [("OPTION_FIELD_EMPTY", "decisions/options.json", "OPT-0001", None),
                    ("NUMBER_UNLINKED", PLAN, None, 27)]
        for stage in ("validation_plan", "done"):
            with self.subTest(stage):
                _, report = self.check(stage)
                self.assertEqual([(p["code"], p["file"], p["id"], p.get("line")) for p in report["problems"]],
                                 expected)
                self.assertEqual([w["code"] for w in report["warnings"]], ["TEAM_NOT_ASKED"])

    def test_case_with_the_marker_passes_every_stage(self):
        self.v2()
        for stage in ("options", "decision", "strategies", "validation_plan", "done"):
            with self.subTest(stage):
                self.assertEqual(self.codes(stage), set())
        self.assertEqual(self.warning_codes(), {"TEAM_NOT_ASKED"})

    # --- cost ---

    def test_cost_missing(self):
        self.assertEqual(self.problem("OPTION_COST_MISSING"), [])
        self.v2(cost={"OPT-0002": None})
        found = self.problem("OPTION_COST_MISSING", "options")
        self.assertEqual([p["id"] for p in found], ["OPT-0002"])
        self.assertIn("cost", found[0]["note"])
        self.assertIn("estimated", found[0]["note"])
        self.assertEqual([p["id"] for p in self.problem("OPTION_COST_MISSING", "done")], ["OPT-0002"])

    def test_cost_unfounded(self):
        self.v2()
        self.assertEqual(self.problem("OPTION_COST_UNFOUNDED"), [])
        broken = {
            "not computed": ["CALC-0002"],
            "one of two not computed": ["CALC-0001", "CALC-0002"],
        }
        for name, calc_ids in broken.items():
            with self.subTest(name):
                self.v2(cost={"OPT-0003": {"status": "estimated", "calc_ids": calc_ids, "note": "x"}})
                self.change("OPT", lambda r: r[2]["basis"].append("CALC-0002"))
                found = self.problem("OPTION_COST_UNFOUNDED", "options")
                self.assertEqual([p["id"] for p in found], ["OPT-0003"])
                self.assertIn("CALC-0002", found[0]["note"])
                self.assertIn("computed", found[0]["note"])

    def test_cost_unfounded_when_the_calculation_is_not_in_the_basis(self):
        self.v2()
        self.change("OPT", lambda r: r[2]["basis"].remove("CALC-0001"))
        found = self.problem("OPTION_COST_UNFOUNDED", "options")
        self.assertEqual([p["id"] for p in found], ["OPT-0003"])
        self.assertIn("basis", found[0]["note"])

    def test_cost_gap(self):
        self.v2(cost={"OPT-0003": NOT_ESTIMATED})
        self.assertEqual(self.problem("OPTION_COST_GAP", "options"), [])
        self.assertEqual(self.problem("OPTION_COST_GAP", "done"), [])
        cases = {
            "answered": ("Q-0001", lambda q: None),
            "no gap": ("Q-0002", lambda q: q[1].update(gap=None)),
            "blank gap": ("Q-0002", lambda q: q[1].update(gap="  ")),
            "open": ("Q-0002", lambda q: q[1].update(status="open", gap="Нет цены")),
        }
        for name, (question, change) in cases.items():
            with self.subTest(name):
                self.v2(cost={"OPT-0003": dict(NOT_ESTIMATED, question_id=question)})
                self.change("Q", change)
                found = self.problem("OPTION_COST_GAP", "options")
                self.assertEqual([p["id"] for p in found], ["OPT-0003"])
                self.assertIn("gap", found[0]["note"])

    def test_gap_not_shown(self):
        self.change("Q", lambda q: q.append(dict(q[1], id="Q-0003", question="Сколько стоит новый пункт")))
        self.v2(cost={"OPT-0003": dict(NOT_ESTIMATED, question_id="Q-0003")})
        found = self.problem("OPTION_GAP_NOT_SHOWN", "options")
        self.assertEqual([p["id"] for p in found], ["OPT-0003"])
        self.assertIn("Чего не хватает", found[0]["note"])
        self.assertIn("Q-0003", found[0]["note"])
        self.replace("outputs/options.md", "Доходы неизвестны [Q-0002].",
                     "Доходы неизвестны [Q-0002]. Цена пункта [Q-0003].")
        self.assertEqual(self.problem("OPTION_GAP_NOT_SHOWN", "options"), [])
        self.assertEqual(self.problem("OPTION_GAP_NOT_SHOWN", "done"), [])

    def test_gap_named_in_another_section_is_not_shown(self):
        self.v2(cost={"OPT-0003": NOT_ESTIMATED})
        self.assertEqual(self.problem("OPTION_GAP_NOT_SHOWN", "options"), [])
        self.replace("outputs/options.md", "Доходы неизвестны [Q-0002].", "Доходы неизвестны.")
        self.replace("outputs/options.md", "Падение заявок.", "Падение заявок [Q-0002].")
        self.assertEqual(len(self.problem("OPTION_GAP_NOT_SHOWN", "options")), 1)

    # --- limit check ---

    def test_limit_missing(self):
        self.assertEqual(self.problem("OPTION_LIMIT_MISSING"), [])
        self.v2(limit={"OPT-0001": None})
        found = self.problem("OPTION_LIMIT_MISSING", "options")
        self.assertEqual([p["id"] for p in found], ["OPT-0001"])
        self.assertIn("limit_check", found[0]["note"])
        self.assertEqual([p["id"] for p in self.problem("OPTION_LIMIT_MISSING", "done")], ["OPT-0001"])

    def test_limit_unfounded(self):
        fits = {"brief_decision": None, "result": "fits", "calc_id": "CALC-0001"}
        for result in ("fits", "exceeds"):
            self.v2(limit={"OPT-0003": dict(fits, result=result)})
            self.assertEqual(self.problem("OPTION_LIMIT_UNFOUNDED", "options"), [])
        self.v2(limit={"OPT-0003": dict(fits, calc_id="CALC-0002")})
        self.change("OPT", lambda r: r[2]["basis"].append("CALC-0002"))
        found = self.problem("OPTION_LIMIT_UNFOUNDED", "options")
        self.assertEqual([p["id"] for p in found], ["OPT-0003"])
        self.assertIn("calc_id", found[0]["note"])
        self.assertIn("CALC-0002", found[0]["note"])

    def test_limit_unfounded_when_the_calculation_is_not_in_the_basis(self):
        self.v2(limit={"OPT-0002": {"brief_decision": None, "result": "fits", "calc_id": "CALC-0001"}},
                cost={"OPT-0002": {"status": "none", "note": "x"}})
        found = self.problem("OPTION_LIMIT_UNFOUNDED", "options")
        self.assertEqual([p["id"] for p in found], ["OPT-0002"])
        self.assertIn("basis", found[0]["note"])

    def test_fits_without_a_calculation_is_honest_for_a_cost_of_none(self):
        none = {"status": "none", "note": "Затрат нет"}
        self.v2(cost={"OPT-0002": none}, limit={"OPT-0002": {"brief_decision": None, "result": "fits"}})
        self.assertEqual(self.problem("OPTION_LIMIT_UNFOUNDED", "options"), [])
        self.assertEqual(self.codes("done"), set())

    def test_fits_without_a_calculation_needs_a_cost_of_none(self):
        self.v2(limit={"OPT-0003": {"brief_decision": None, "result": "fits"}})
        found = self.problem("OPTION_LIMIT_UNFOUNDED", "options")
        self.assertEqual([p["id"] for p in found], ["OPT-0003"])
        self.assertIn("calc_id", found[0]["note"])
        self.v2(cost={"OPT-0003": NOT_ESTIMATED}, limit={"OPT-0003": {"brief_decision": None, "result": "fits"}})
        self.assertEqual([p["id"] for p in self.problem("OPTION_LIMIT_UNFOUNDED", "options")], ["OPT-0003"])

    def test_exceeds_with_a_cost_of_none_is_unfounded(self):
        none = {"status": "none", "note": "Затрат нет"}
        self.v2(cost={"OPT-0002": none},
                limit={"OPT-0002": {"brief_decision": None, "result": "exceeds", "calc_id": "CALC-0001"}})
        self.change("OPT", lambda r: r[1]["basis"].append("CALC-0001"))
        found = self.problem("OPTION_LIMIT_UNFOUNDED", "options")
        self.assertEqual([p["id"] for p in found], ["OPT-0002"])
        self.assertIn("none", found[0]["note"])

    def test_limit_unfounded_when_the_cost_is_not_estimated(self):
        for result in ("fits", "exceeds"):
            with self.subTest(result):
                self.v2(cost={"OPT-0003": NOT_ESTIMATED},
                        limit={"OPT-0003": {"brief_decision": None, "result": result, "calc_id": "CALC-0001"}})
                found = self.problem("OPTION_LIMIT_UNFOUNDED", "options")
                self.assertEqual([p["id"] for p in found], ["OPT-0003"])
                self.assertIn("not_estimated", found[0]["note"])
        self.v2(cost={"OPT-0003": NOT_ESTIMATED})  # unknown is the only honest result there
        self.assertEqual(self.problem("OPTION_LIMIT_UNFOUNDED", "options"), [])

    def test_limit_stale_after_a_new_brief_decision(self):
        self.v2()
        for stage in ("options", "decision", "strategies", "validation_plan", "done"):
            self.assertEqual(self.problem("OPTION_LIMIT_STALE", stage), [])
        self.add_brief()
        for stage in ("options", "decision", "strategies", "validation_plan", "done"):
            with self.subTest(stage):
                found = self.problem("OPTION_LIMIT_STALE", stage)
                self.assertEqual([p["id"] for p in found], ["OPT-0001", "OPT-0002", "OPT-0003"])
        self.assertIn("DEC-0007", found[0]["note"])
        self.assertIn("brief_decision", found[0]["note"])

    def test_limit_stale_goes_away_after_the_checks_are_redone(self):
        self.v2()
        self.add_brief()
        self.assertTrue(self.problem("OPTION_LIMIT_STALE", "done"))
        self.change("OPT", lambda r: [o["limit_check"].update(brief_decision="DEC-0007") for o in r])
        self.assertEqual(self.codes("done"), set())

    def test_limit_stale_when_the_brief_changes_again(self):
        self.v2()
        self.add_brief()
        self.change("OPT", lambda r: [o["limit_check"].update(brief_decision="DEC-0007") for o in r])
        self.add_brief("DEC-0008", "ACT-0016", supersedes="DEC-0007")
        self.assertEqual(len(self.problem("OPTION_LIMIT_STALE", "options")), 3)
        self.change("OPT", lambda r: [o["limit_check"].update(brief_decision="DEC-0008") for o in r])
        self.assertEqual(self.codes("done"), set())

    def test_limit_stale_when_the_decision_is_not_the_brief(self):
        self.v2(limit={"OPT-0001": {"brief_decision": "DEC-0003", "result": "no_limit", "note": "x"}})
        self.assertEqual([p["id"] for p in self.problem("OPTION_LIMIT_STALE", "options")], ["OPT-0001"])

    def test_limit_stale_when_a_brief_decision_exists_and_the_check_names_none(self):
        self.add_brief()
        self.v2()
        self.assertEqual(len(self.problem("OPTION_LIMIT_STALE", "options")), 3)

    def test_limit_stale_is_not_asked_of_a_case_without_the_marker(self):
        self.add_brief()
        self.assertEqual(self.problem("OPTION_LIMIT_STALE", "done"), [])

    # --- warnings ---

    def test_no_develop_option_estimated_is_a_warning(self):
        self.v2()
        self.assertNotIn("OPTIONS_COST_UNKNOWN", self.warning_codes("options"))
        self.v2(cost={"OPT-0003": NOT_ESTIMATED})
        self.assertIn("OPTIONS_COST_UNKNOWN", self.warning_codes("options"))
        self.assertEqual(self.codes("options"), set())

    def test_chosen_option_over_the_limit_is_a_warning(self):
        self.v2()
        self.assertNotIn("CHOICE_EXCEEDS_LIMIT", self.warning_codes())
        self.v2(cost={"OPT-0001": {"status": "estimated", "calc_ids": ["CALC-0001"], "note": "x"}},
                limit={"OPT-0001": {"brief_decision": None, "result": "exceeds", "calc_id": "CALC-0001",
                                            "compared": "direct_costs"}})
        self.change("OPT", lambda r: r[0]["basis"].append("CALC-0001") if "CALC-0001" not in r[0]["basis"] else None)
        _, report = self.check("done")
        self.assertEqual(report["problems"], [])
        found = [w for w in report["warnings"] if w["code"] == "CHOICE_EXCEEDS_LIMIT"]
        self.assertEqual([w["id"] for w in found], ["OPT-0001"])

    def test_option_that_nobody_chose_gives_no_warning(self):
        self.v2(limit={"OPT-0003": {"brief_decision": None, "result": "exceeds", "calc_id": "CALC-0001"}})
        self.assertNotIn("CHOICE_EXCEEDS_LIMIT", self.warning_codes("options"))

    # --- threshold basis ---

    def test_threshold_basis_missing(self):
        self.v2()
        self.replace(PLAN, BASIS_SENTENCE, "")
        for stage in ("validation_plan", "done"):
            with self.subTest(stage):
                found = self.problem("THRESHOLD_BASIS_MISSING", stage)
                self.assertEqual([p["file"] for p in found], [PLAN])
                self.assertIn("Условие успеха и неуспеха", found[0]["note"])
                self.assertIn("CALC", found[0]["note"])
        self.assertEqual(self.problem("THRESHOLD_BASIS_MISSING", "strategies"), [])

    def test_threshold_basis_by_a_calculation_or_a_question(self):
        self.v2()
        self.assertEqual(self.problem("THRESHOLD_BASIS_MISSING"), [])
        self.replace(PLAN, "разброс показателя не посчитан [Q-0002]", "разброс от 100 до 140 [CALC-0001]")
        self.assertEqual(self.problem("THRESHOLD_BASIS_MISSING"), [])

    def test_threshold_basis_elsewhere_does_not_count(self):
        self.v2()
        self.replace(PLAN, BASIS_SENTENCE, "")
        self.replace(PLAN, "Очереди в пунктах.", "Очереди в пунктах [Q-0002].")
        self.assertEqual(len(self.problem("THRESHOLD_BASIS_MISSING")), 1)

    def test_threshold_basis_is_not_asked_of_a_case_without_the_marker(self):
        self.assertEqual(self.problem("THRESHOLD_BASIS_MISSING", "validation_plan"), [])

    def test_stale_message_when_the_brief_decision_waits_for_the_person(self):
        self.v2()
        self.add_brief()

        def propose(records):
            brief = next(r for r in records if r["id"] == "DEC-0007")
            brief["status"] = "proposed"
            del brief["choice"]
            brief["proposal"] = {"option_id": "OPT-0001", "rationale": "Новое ограничение брифа",
                                 "proposed_at": brief["created_at"], "action_id": "ACT-0007"}
        self.change("DEC", propose)
        self.assertEqual(self.problem("OPTION_LIMIT_STALE", "options"), [], "a proposed brief is not current yet")
        self.change("OPT", lambda r: [o["limit_check"].update(brief_decision="DEC-0007") for o in r])
        found = self.problem("OPTION_LIMIT_STALE", "options")
        self.assertEqual(len(found), 3)
        note = found[0]["note"]
        self.assertIn("confirmation", note)
        self.assertIn("DEC-0007", note)
        self.assertNotIn("write its ID in brief_decision", note)

    def test_stale_while_a_proposed_brief_replaces_the_chosen_one(self):
        self.v2()
        self.add_brief()
        self.change("OPT", lambda r: [o["limit_check"].update(brief_decision="DEC-0007") for o in r])
        self.assertEqual(self.codes("done"), set())
        self.add_brief("DEC-0008", "ACT-0016", supersedes="DEC-0007")
        saved = {}

        def propose(records):
            brief = next(r for r in records if r["id"] == "DEC-0008")
            saved["choice"] = brief.pop("choice")
            brief["status"] = "proposed"
            brief["proposal"] = {"option_id": "OPT-0001", "rationale": "Бюджет вдвое меньше",
                                 "proposed_at": brief["created_at"], "action_id": "ACT-0007"}
        self.change("DEC", propose)
        statuses = {r["id"]: r["status"] for r in read_json(self.case / "decisions/decisions.json")["records"]}
        self.assertEqual((statuses["DEC-0007"], statuses["DEC-0008"]), ("superseded", "proposed"))
        found = self.problem("OPTION_LIMIT_STALE", "options")
        self.assertEqual(len(found), 3)
        self.assertIn("confirmation", found[0]["note"])
        self.assertIn("DEC-0008", found[0]["note"])

        def confirm(records):
            brief = next(r for r in records if r["id"] == "DEC-0008")
            brief["status"], brief["proposal"], brief["choice"] = "chosen", None, saved["choice"]
        self.change("DEC", confirm)
        self.assertEqual(len(self.problem("OPTION_LIMIT_STALE", "options")), 3)
        self.change("OPT", lambda r: [o["limit_check"].update(brief_decision="DEC-0008") for o in r])
        self.assertEqual(self.codes("done"), set())

    # --- what was compared (limit_check.compared) ---

    LINKED = {"brief_decision": None, "result": "exceeds", "calc_id": "CALC-0001",
              "compared": "direct_costs_and_lost_revenue", "note": "Бюджет покрывает и потери выручки [бриф]"}

    def test_compared_missing(self):
        for result in ("fits", "exceeds"):
            with self.subTest(result):
                self.v2(limit={"OPT-0003": {"brief_decision": None, "result": result, "calc_id": "CALC-0001"}})
                for stage in ("options", "done"):
                    found = self.problem("OPTION_LIMIT_COMPARED_MISSING", stage)
                    self.assertEqual([p["id"] for p in found], ["OPT-0003"])
                    self.assertIn("limit_check.compared", found[0]["note"])
                self.assertEqual(self.problem("OPTION_LIMIT_COMPARED_MISSING", "research"), [])

    def test_compared_direct_costs_is_enough(self):
        for result in ("fits", "exceeds"):
            with self.subTest(result):
                self.v2(limit={"OPT-0003": {"brief_decision": None, "result": result, "calc_id": "CALC-0001",
                                            "compared": "direct_costs"}})
                self.assertEqual(self.problem("OPTION_LIMIT_COMPARED_MISSING", "done"), [])
                self.assertEqual(self.problem("OPTION_LIMIT_COVERAGE_UNFOUNDED", "done"), [])

    def test_compared_is_not_asked_when_nothing_was_compared(self):
        none = {"status": "none", "note": "Затрат нет"}
        self.v2(cost={"OPT-0002": none}, limit={"OPT-0002": {"brief_decision": None, "result": "fits"}})
        self.assertEqual(self.problem("OPTION_LIMIT_COMPARED_MISSING", "done"), [])
        self.v2()  # unknown and no_limit
        self.assertEqual(self.problem("OPTION_LIMIT_COMPARED_MISSING", "done"), [])

    def test_coverage_needs_a_link_in_the_note(self):
        self.v2(limit={"OPT-0003": dict(self.LINKED, note="Бюджет покрывает и потери выручки")})
        found = self.problem("OPTION_LIMIT_COVERAGE_UNFOUNDED", "options")
        self.assertEqual([p["id"] for p in found], ["OPT-0003"])
        self.assertIn("limit_check.note", found[0]["note"])
        self.v2(limit={"OPT-0003": {k: v for k, v in self.LINKED.items() if k != "note"}})
        self.assertEqual([p["id"] for p in self.problem("OPTION_LIMIT_COVERAGE_UNFOUNDED", "done")], ["OPT-0003"])

    def test_coverage_accepts_brief_claim_or_source_link(self):
        for ref in ("[бриф]", "[CLM-0002]", "[SRC-0001]", "[CLM-0002, CLM-0003]"):
            with self.subTest(ref):
                self.v2(limit={"OPT-0003": dict(self.LINKED, note=f"Бюджет покрывает потери {ref}")})
                self.assertEqual(self.problem("OPTION_LIMIT_COVERAGE_UNFOUNDED", "done"), [])
                self.assertEqual(self.problem("OPTION_LIMIT_COMPARED_MISSING", "done"), [])

    def test_compared_is_not_asked_of_a_case_without_the_marker(self):
        for stage in ("options", "done"):
            self.assertEqual(self.problem("OPTION_LIMIT_COMPARED_MISSING", stage), [])
            self.assertEqual(self.problem("OPTION_LIMIT_COVERAGE_UNFOUNDED", stage), [])

    def test_withdrawn_option_is_not_asked_for_compared(self):
        self.v2(limit={"OPT-0003": {"brief_decision": None, "result": "fits", "calc_id": "CALC-0001"}})
        self.change("OPT", lambda r: r[2].update(record_status="withdrawn", status_reason="не нужен"))
        self.assertEqual(self.problem("OPTION_LIMIT_COMPARED_MISSING", "options"), [])

    def test_messages_name_the_field_they_are_about(self):
        def stale():
            self.add_brief()
            self.v2()

        def basis_cleared():
            self.v2()
            self.replace(PLAN, BASIS_SENTENCE, "")

        def not_shown():
            self.change("Q", lambda q: q.append(dict(q[1], id="Q-0003", question="Сколько стоит новый пункт")))
            self.v2(cost={"OPT-0003": dict(NOT_ESTIMATED, question_id="Q-0003")})

        def unfounded_cost():
            self.v2(cost={"OPT-0003": {"status": "estimated", "calc_ids": ["CALC-0002"], "note": "x"}})
            self.change("OPT", lambda r: r[2]["basis"].append("CALC-0002"))

        def unfounded_limit():
            self.v2(limit={"OPT-0003": {"brief_decision": None, "result": "fits", "calc_id": "CALC-0002"}})
            self.change("OPT", lambda r: r[2]["basis"].append("CALC-0002"))

        table = [
            ("OPTION_COST_MISSING", "cost", lambda: self.v2(cost={"OPT-0001": None})),
            ("OPTION_COST_UNFOUNDED", "cost.calc_ids", unfounded_cost),
            ("OPTION_COST_GAP", "cost.question_id", lambda: self.v2(cost={"OPT-0003": dict(NOT_ESTIMATED, question_id="Q-0001")})),
            ("OPTION_LIMIT_MISSING", "limit_check", lambda: self.v2(limit={"OPT-0002": None})),
            ("OPTION_LIMIT_STALE", "limit_check.brief_decision", stale),
            ("OPTION_LIMIT_UNFOUNDED", "limit_check.calc_id", unfounded_limit),
            ("OPTION_LIMIT_COMPARED_MISSING", "limit_check.compared",
             lambda: self.v2(limit={"OPT-0003": {"brief_decision": None, "result": "fits", "calc_id": "CALC-0001"}})),
            ("OPTION_LIMIT_COVERAGE_UNFOUNDED", "limit_check.note",
             lambda: self.v2(limit={"OPT-0003": dict(self.LINKED, note="покрывает")})),
            ("OPTION_GAP_NOT_SHOWN", "cost.question_id", not_shown),
            ("THRESHOLD_BASIS_MISSING", "Условие успеха и неуспеха", basis_cleared),
        ]
        for code, field, build in table:
            with self.subTest(code):
                self.setUp()
                build()
                found = self.problem(code, "done")
                self.assertTrue(found, code)
                for item in found:
                    self.assertIn(field, item["note"], item)


if __name__ == "__main__":
    unittest.main()
