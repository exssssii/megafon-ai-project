import io
import json
import shutil
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))

import accounting as ac  # noqa: E402
import checkpoint as ck  # noqa: E402
import validate_project as vp  # noqa: E402

DEMO = ROOT / "agent" / "contracts" / "examples" / "valid" / "demo-case"
JOURNAL = "state/actions.jsonl"
OPTIONS = "decisions/options.json"
REPORT = "outputs/accounting.md"
UNKNOWN = {"basis": "unknown"}


def at(minute: int) -> str:
    hours, minutes = divmod(minute, 60)
    return f"2026-11-06T{10 + hours:02d}:{minutes:02d}:00+03:00"


def act(action_id, kind, start, end=None, outcome="completed", cost=UNKNOWN, human=None, refs=(), retry=None):
    """Two journal lines of one action; end=None leaves the action without an end."""
    first = {"format_version": 1, "id": action_id, "phase": "start", "kind": kind, "tool": "t",
             "environment": "claude_code", "model": "m", "input_refs": [], "at": at(start)}
    if retry:
        first["retry_of"], first["retry_reason"] = retry
    if end is None:
        return [first]
    last = {"format_version": 1, "id": action_id, "phase": "end", "outcome": outcome, "result_refs": list(refs),
            "cost": cost, "at": at(end)}
    if human is not None:
        last["human"] = human
    return [first, last]


def option(option_id, minute, status="active"):
    return {"id": option_id, "created_at": at(minute), "record_status": status}


def measured(**amounts):
    return {"basis": "measured", "amounts": amounts}


def estimated(source="оценка по длине текста", **amounts):
    return {"basis": "estimated", "estimate_source": source, "amounts": amounts}


def run(*argv, tool=ac):
    out = io.StringIO()
    with redirect_stdout(out):
        code = tool.main([str(a) for a in argv])
    return code, json.loads(out.getvalue().splitlines()[0])


def write_journal(case, lines):
    (case / JOURNAL).write_bytes("".join(json.dumps(line, ensure_ascii=False) + "\n" for line in lines).encode("utf-8"))


class Time(unittest.TestCase):
    def test_clock_starts_when_the_case_was_created(self):
        lines = act("ACT-0001", "human_input", 20, 20, refs=["DEC-0001"]) + act("ACT-0002", "search", 21, 40)
        time = ac.summarize(lines, [option("OPT-0001", 30)], at(0))["time"]
        self.assertEqual((time["started_at"], time["elapsed_seconds"], time["time_to_first_result_seconds"]),
                         ("2026-11-06T10:00:00+03:00", 40 * 60, 30 * 60))

    def test_time_written_by_the_agent_does_not_move_the_clock(self):
        time = ac.summarize(act("ACT-0001", "search", 0, 10), [option("OPT-0001", 500)])["time"]
        self.assertEqual((time["elapsed_seconds"], time["working_seconds"]), (10 * 60, 10 * 60))

    def test_interrupted_action_does_not_hide_a_break(self):
        lines = act("ACT-0001", "model", 0, 100)
        lines[-1]["interrupted"] = True
        summary = ac.summarize(lines, [])
        self.assertEqual([pause["seconds"] for pause in summary["time"]["pauses"]], [100 * 60])
        self.assertNotIn("LONG_ACTION_COUNTED", summary["warnings"])

    def test_a_long_step_is_not_a_pause(self):
        lines = act("ACT-0001", "model", 0, 40) + act("ACT-0002", "save", 41, 42)
        time = ac.summarize(lines, [])["time"]
        self.assertEqual((time["pauses"], time["working_seconds"]), ([], 42 * 60))

    def test_first_result_and_full_time_differ(self):
        lines = act("ACT-0001", "file_read", 0, 5) + act("ACT-0002", "model", 10, 25) + act("ACT-0003", "save", 40, 50)
        summary = ac.summarize(lines, [option("OPT-0002", 25), option("OPT-0001", 30)])
        self.assertEqual((summary["time"]["elapsed_seconds"], summary["time"]["time_to_first_result_seconds"]),
                         (50 * 60, 25 * 60))
        self.assertEqual(summary["time"]["first_result"], "OPT-0002")

    def test_withdrawn_option_is_still_the_first_result(self):
        lines = act("ACT-0001", "model", 0, 60)
        summary = ac.summarize(lines, [option("OPT-0001", 10, "withdrawn"), option("OPT-0002", 40)])
        self.assertEqual((summary["time"]["first_result"], summary["time"]["time_to_first_result_seconds"]),
                         ("OPT-0001", 10 * 60))

    def test_no_options_means_no_first_result(self):
        summary = ac.summarize(act("ACT-0001", "model", 0, 5), [])
        self.assertIsNone(summary["time"]["time_to_first_result_seconds"])

    def test_option_before_the_journal_is_zero_with_a_warning(self):
        summary = ac.summarize(act("ACT-0001", "model", 30, 40), [option("OPT-0001", 10)])
        self.assertEqual(summary["time"]["time_to_first_result_seconds"], 0)
        self.assertIn("FIRST_RESULT_BEFORE_JOURNAL", summary["warnings"])

    def test_first_result_is_the_stamp_of_the_action_that_wrote_it(self):
        lines = act("ACT-0001", "model", 10, 40, refs=["OPT-0001", "OPT-0002"]) + act("ACT-0002", "save", 41, 50)
        summary = ac.summarize(lines, [option("OPT-0001", 1), option("OPT-0002", 2)])
        self.assertEqual((summary["time"]["first_result"], summary["time"]["time_to_first_result_seconds"]),
                         ("OPT-0001", 30 * 60))
        self.assertNotIn("FIRST_RESULT_UNSTAMPED", summary["warnings"])

    def test_withdrawn_option_in_an_action_keeps_its_stamp(self):
        lines = act("ACT-0001", "model", 0, 10, refs=["OPT-0001"]) + act("ACT-0002", "model", 11, 20, refs=["OPT-0002"])
        summary = ac.summarize(lines, [option("OPT-0001", 5, "withdrawn"), option("OPT-0002", 15)])
        self.assertEqual((summary["time"]["first_result"], summary["time"]["time_to_first_result_seconds"]),
                         ("OPT-0001", 10 * 60))

    def test_only_a_finished_step_of_the_agent_stamps_the_result(self):
        for lines in (act("ACT-0001", "model", 10, 100, outcome="failed", refs=["OPT-0001"]),
                      act("ACT-0001", "model", 10, 100, outcome="unknown", refs=["OPT-0001"]),
                      act("ACT-0001", "human_input", 10, 100, refs=["OPT-0001"], human={"help": "answer"})):
            summary = ac.summarize(lines, [option("OPT-0001", 20)])
            self.assertEqual(summary["time"]["time_to_first_result_seconds"], 10 * 60)
            self.assertIn("FIRST_RESULT_UNSTAMPED", summary["warnings"])

    def test_earlier_option_no_action_names_keeps_the_written_time(self):
        lines = act("ACT-0001", "model", 10, 100, refs=["OPT-0002"])
        summary = ac.summarize(lines, [option("OPT-0001", 20), option("OPT-0002", 90)])
        self.assertEqual((summary["time"]["first_result"], summary["time"]["time_to_first_result_seconds"]),
                         ("OPT-0001", 10 * 60))
        self.assertIn("FIRST_RESULT_UNSTAMPED", summary["warnings"])

    def test_time_the_agent_wrote_is_marked_in_the_report(self):
        summary = ac.summarize(act("ACT-0001", "model", 0, 40), [option("OPT-0001", 30)])
        self.assertEqual(summary["time"]["time_to_first_result_seconds"], 30 * 60)
        self.assertIn("FIRST_RESULT_UNSTAMPED", summary["warnings"])
        self.assertIn("взято из записи агента", ac.render("c", summary))

    def test_action_closed_as_unknown_does_not_hide_a_break(self):
        lines = act("ACT-0001", "model", 0, 100, outcome="unknown") + act("ACT-0002", "model", 5, 10)
        time = ac.summarize(lines, [])["time"]
        self.assertEqual([pause["seconds"] for pause in time["pauses"]], [90 * 60])

    def test_long_finished_action_is_named_in_the_report(self):
        summary = ac.summarize(act("ACT-0001", "model", 0, 40), [])
        self.assertEqual(summary["time"]["pauses"], [])
        self.assertIn("LONG_ACTION_COUNTED", summary["warnings"])
        self.assertIn("время завышено", ac.render("c", summary))

    def test_long_pause_is_excluded_from_working_time_only(self):
        lines = act("ACT-0001", "model", 0, 10) + act("ACT-0002", "model", 100, 110)
        time = ac.summarize(lines, [])["time"]
        self.assertEqual((time["elapsed_seconds"], time["working_seconds"]), (110 * 60, 20 * 60))
        self.assertEqual([pause["seconds"] for pause in time["pauses"]], [90 * 60])

    def test_pause_of_exactly_thirty_minutes_is_work(self):
        time = ac.summarize(act("ACT-0001", "model", 0, 30), [])["time"]
        self.assertEqual((time["working_seconds"], time["pauses"]), (30 * 60, []))

    def test_empty_journal(self):
        summary = ac.summarize([], [])
        self.assertEqual((summary["time"]["elapsed_seconds"], summary["cost"]["total"]), (0, UNKNOWN))
        self.assertIn("Журнал действий пуст", ac.render("c", summary))


class HumanTime(unittest.TestCase):
    def test_all_reported_gives_the_sum(self):
        lines = (act("ACT-0001", "model", 0, 5)
                 + act("ACT-0002", "human_input", 20, 20, human={"help": "choice", "reported_active_seconds": 300},
                       refs=["DEC-0001"])
                 + act("ACT-0003", "human_input", 30, 30, human={"help": "answer", "reported_active_seconds": 60.5}))
        human = ac.summarize(lines, [])["human"]
        self.assertEqual(human["active_seconds"], 360.5)
        self.assertEqual([item["response_seconds"] for item in human["items"]], [15 * 60, 10 * 60])

    def test_one_unreported_makes_the_total_unknown(self):
        lines = (act("ACT-0001", "human_input", 0, 0, human={"help": "choice", "reported_active_seconds": 300},
                     refs=["DEC-0001"])
                 + act("ACT-0002", "human_input", 5, 5, refs=["DEC-0002"]))
        human = ac.summarize(lines, [])["human"]
        self.assertIsNone(human["active_seconds"])
        self.assertEqual((human["reported_active_seconds"], human["unreported"]), (300, 1))
        self.assertIsNone(ac.totals(lines, [])["human_active_seconds"])

    def test_no_human_input_is_unknown_not_zero(self):
        self.assertIsNone(ac.totals(act("ACT-0001", "model", 0, 5), [])["human_active_seconds"])

    def test_legacy_human_input_counts_as_a_choice(self):
        human = ac.summarize(act("ACT-0001", "human_input", 0, 0, refs=["DEC-0001"]), [])["human"]
        self.assertEqual((human["by_help"]["choice"], human["items"][0]["recorded"]), (1, False))


class Cost(unittest.TestCase):
    def test_all_measured_is_an_exact_sum(self):
        lines = (act("ACT-0001", "model", 0, 1, cost=measured(input_tokens=1000, output_tokens=10, money_usd=0.1))
                 + act("ACT-0002", "model", 2, 3, cost=measured(input_tokens=500, output_tokens=5, money_usd=0.2)))
        self.assertEqual(ac.totals(lines, [])["cost"],
                         {"basis": "measured", "amounts": {"input_tokens": 1500, "output_tokens": 15, "money_usd": 0.3}})

    def test_estimate_gives_a_range_and_the_source(self):
        lines = (act("ACT-0001", "model", 0, 1, cost=measured(input_tokens=1000, output_tokens=10, money_usd=None))
                 + act("ACT-0002", "model", 2, 3, cost=estimated(input_tokens={"min": 100, "max": 300}, output_tokens=5,
                                                                 money_usd=None)))
        total = ac.totals(lines, [])["cost"]
        self.assertEqual(total["basis"], "estimated")
        self.assertEqual(total["amounts"], {"input_tokens": {"min": 1100, "max": 1300}, "output_tokens": 15, "money_usd": None})
        self.assertIn("оценка по длине текста", total["estimate_source"])
        self.assertIsNone(vp.cost_problem(total))

    def test_one_unknown_makes_the_total_unknown_and_keeps_the_known_part(self):
        lines = (act("ACT-0001", "model", 0, 1, cost=measured(input_tokens=1000, output_tokens=10, money_usd=None))
                 + act("ACT-0002", "web_read", 2, 3))
        summary = ac.summarize(lines, [])
        self.assertEqual(summary["cost"]["total"], UNKNOWN)
        self.assertEqual(summary["cost"]["fields"]["input_tokens"]["measured"], 1000)
        self.assertEqual(summary["cost"]["actions"], {"measured": 1, "estimated": 0, "unknown": 1})
        text = ac.render("c", summary)
        self.assertIn("итог неизвестен", text)
        self.assertIn("не меньше 1 000", text)

    def test_null_amount_stays_null_and_never_becomes_zero(self):
        lines = act("ACT-0001", "model", 0, 1, cost=measured(input_tokens=1000, output_tokens=None, money_usd=None))
        amounts = ac.totals(lines, [])["cost"]["amounts"]
        self.assertEqual((amounts["output_tokens"], amounts["money_usd"]), (None, None))
        text = ac.render("c", ac.summarize(lines, []))
        self.assertNotIn("$0", text)
        self.assertIn("| Деньги, USD | — | — | 1 |", text)
        self.assertIn("| Токены на выходе | — | — | 1 |", text)

    def test_failed_and_retried_actions_are_still_paid_for(self):
        lines = (act("ACT-0001", "model", 0, 1, outcome="failed", cost=measured(input_tokens=10, output_tokens=1, money_usd=None))
                 + act("ACT-0002", "model", 2, 3, cost=measured(input_tokens=20, output_tokens=2, money_usd=None),
                       retry=("ACT-0001", "Сбой сети")))
        self.assertEqual(ac.totals(lines, [])["cost"]["amounts"]["input_tokens"], 30)

    def test_decimal_sums(self):
        lines = sum((act(f"ACT-000{i}", "model", i, i, cost=measured(money_usd=0.1)) for i in range(1, 4)), [])
        self.assertEqual(ac.totals(lines, [])["cost"]["amounts"]["money_usd"], 0.3)

    def test_money_estimate_needs_a_tariff_date(self):
        self.assertIsNotNone(vp.cost_problem(estimated("тариф API", money_usd=0.5)))
        self.assertIsNone(vp.cost_problem(estimated("тариф API на 2026-11-06", money_usd=0.5)))
        self.assertIsNone(vp.cost_problem(estimated("по длине", input_tokens=10, money_usd=None)))


class Actions(unittest.TestCase):
    def test_errors_retries_and_open_actions_are_visible(self):
        lines = (act("ACT-0001", "compute", 0, 1, outcome="failed")
                 + act("ACT-0002", "compute", 2, 3, retry=("ACT-0001", "Исправлен [диапазон]"))
                 + act("ACT-0003", "web_read", 4, 5, outcome="unknown")
                 + act("ACT-0004", "search", 6))
        actions = ac.summarize(lines, [])["actions"]
        self.assertEqual((actions["total"], actions["completed"], actions["failed"], actions["unknown"], actions["no_end"]),
                         (4, 1, 1, 1, 1))
        self.assertEqual({p["id"]: p["settled_by"] for p in actions["problems"]},
                         {"ACT-0001": "ACT-0002", "ACT-0003": None, "ACT-0004": None})
        self.assertEqual(actions["open"], ["ACT-0003", "ACT-0004"])
        self.assertEqual(actions["retries"][0]["reason"], "Исправлен [диапазон]")
        text = ac.render("c", ac.summarize(lines, []))
        self.assertIn("ACT-0002 повторяет ACT-0001: Исправлен (диапазон)", text)
        self.assertNotIn("[", text)



class Sessions(unittest.TestCase):
    def test_human_action_is_not_an_unknown_model_cost(self):
        lines = (act("ACT-0001", "human_input", 0, 0, refs=["DEC-0001"])
                 + act("ACT-0002", "model", 1, 2, cost=measured(input_tokens=10, output_tokens=5, money_usd=None)))
        total = ac.totals(lines, [])["cost"]
        self.assertEqual(total, {"basis": "measured",
                                 "amounts": {"input_tokens": 10, "output_tokens": 5, "money_usd": None}})

    def test_only_human_actions_leave_the_cost_unknown(self):
        self.assertEqual(ac.totals(act("ACT-0001", "human_input", 0, 0, refs=["DEC-0001"]), [])["cost"], UNKNOWN)

    def session(self, action_id, start, end):
        lines = act(action_id, "model", start, end, cost=measured(input_tokens=100, output_tokens=None, money_usd=None))
        lines[0]["tool"] = "session"
        return lines

    def test_session_figure_covers_the_steps_before_it(self):
        lines = (act("ACT-0001", "file_read", 0, 1) + act("ACT-0002", "compute", 2, 3)
                 + self.session("ACT-0003", 4, 4))
        summary = ac.summarize(lines, [])
        self.assertEqual(summary["cost"]["total"]["basis"], "measured")
        self.assertEqual(summary["cost"]["total"]["amounts"]["input_tokens"], 100)
        self.assertEqual(summary["cost"]["covered_by_session"], 2)

    def test_steps_after_the_session_stay_unknown(self):
        lines = self.session("ACT-0001", 0, 0) + act("ACT-0002", "file_read", 1, 2)
        summary = ac.summarize(lines, [])
        self.assertEqual(summary["cost"]["total"], UNKNOWN)
        self.assertEqual((summary["cost"]["covered_by_session"], summary["cost"]["actions"]["unknown"]), (0, 1))

    def test_a_step_with_its_own_cost_still_adds_up(self):
        lines = (act("ACT-0001", "search", 0, 1, cost=measured(input_tokens=7, output_tokens=None, money_usd=None))
                 + self.session("ACT-0002", 2, 2))
        self.assertEqual(ac.totals(lines, [])["cost"]["amounts"]["input_tokens"], 107)

    def test_a_session_without_a_figure_covers_nothing(self):
        lines = act("ACT-0001", "file_read", 0, 1) + act("ACT-0002", "model", 2, 2)
        lines[2]["tool"] = "session"
        summary = ac.summarize(lines, [])
        self.assertEqual((summary["cost"]["total"], summary["cost"]["actions"]["unknown"]), (UNKNOWN, 2))

    def test_estimate_is_not_called_known(self):
        lines = (act("ACT-0001", "model", 0, 1, cost=measured(input_tokens=100, output_tokens=None, money_usd=None))
                 + act("ACT-0002", "model", 2, 3, cost=estimated(input_tokens={"min": 50, "max": 70},
                                                                 output_tokens=None, money_usd=None))
                 + act("ACT-0003", "model", 4, 5))
        line = ac.cost_line(ac.summarize(lines, [])["cost"])
        self.assertIn("измерено не меньше 100, оценка ещё от 50 до 70", line)
        self.assertNotIn("150", line)


class Environments(unittest.TestCase):
    def with_runtime(self, lines, **runtime):
        lines[0]["runtime"] = {"version": None, "access": "unknown", "cost_source": None} | runtime
        return lines

    def test_unique_combinations_and_unrecorded_actions(self):
        lines = (self.with_runtime(act("ACT-0001", "model", 0, 1), version="2.1.317", access="subscription",
                                   cost_source="не показывается | [в чате]")
                 + self.with_runtime(act("ACT-0002", "search", 2, 3), version="2.1.317", access="subscription",
                                     cost_source="не показывается | [в чате]")
                 + act("ACT-0003", "compute", 4, 5)
                 + act("ACT-0004", "compute", 6, 7))
        summary = ac.summarize(lines, [])
        rows = summary["actions"]["runtimes"]
        self.assertEqual(sorted((row["recorded"], row["actions"]) for row in rows), [(False, 2), (True, 2)])
        recorded = next(row for row in rows if row["recorded"])
        self.assertEqual((recorded["version"], recorded["access"]), ("2.1.317", "subscription"))
        text = ac.render("c", summary)
        self.assertLess(text.index("## Действия"), text.index("## Среды и доступ"))
        self.assertLess(text.index("## Среды и доступ"), text.index("## Ошибки и повторы"))
        self.assertIn("не показывается / (в чате)", text)
        self.assertIn("| claude_code | m | не записано | не записано | не записано | 2 |", text)

    def test_different_versions_are_different_rows(self):
        lines = (self.with_runtime(act("ACT-0001", "model", 0, 1), version="1")
                 + self.with_runtime(act("ACT-0002", "model", 2, 3), version="2"))
        self.assertEqual(len(ac.summarize(lines, [])["actions"]["runtimes"]), 2)


class Groups(unittest.TestCase):
    LINES = (act("ACT-0001", "model", 0, 1, cost=measured(input_tokens=10, output_tokens=5, money_usd=None))
             + act("ACT-0002", "search", 2, 3, cost=measured(input_tokens=7, output_tokens=None, money_usd=None))
             + act("ACT-0003", "web_read", 4, 5)
             + act("ACT-0004", "compute", 6, 7)
             + act("ACT-0005", "human_input", 8, 8, refs=["DEC-0001"]))

    def test_groups_split_the_cost_and_leave_the_total_alone(self):
        cost = ac.summarize(self.LINES, [])["cost"]
        groups = cost["groups"]
        self.assertEqual(list(groups), ["model", "search_web", "other"])
        self.assertEqual((groups["model"]["actions"], groups["model"]["measured"]["input_tokens"]), (1, 10))
        self.assertEqual((groups["search_web"]["actions"], groups["search_web"]["not_recorded"],
                          groups["search_web"]["measured"]["input_tokens"]), (2, 1, 7))
        self.assertEqual((groups["other"]["actions"], groups["other"]["not_recorded"],
                          groups["other"]["measured"]["input_tokens"]), (1, 1, None))
        self.assertEqual(cost["total"], {"basis": "unknown"})
        self.assertEqual(cost["actions"], {"measured": 2, "estimated": 0, "unknown": 2})
        self.assertEqual((cost["fields"]["input_tokens"]["measured"], cost["fields"]["input_tokens"]["not_recorded"]),
                         (17, 2))

    def test_totals_are_the_same_as_before(self):
        lines = (act("ACT-0001", "model", 0, 1, cost=measured(input_tokens=10, output_tokens=5, money_usd=None))
                 + act("ACT-0002", "search", 2, 3, cost=measured(input_tokens=7, output_tokens=None, money_usd=None)))
        self.assertEqual(ac.totals(lines, [])["cost"], {"basis": "measured", "amounts": {
            "input_tokens": 17, "output_tokens": None, "money_usd": None}})
        self.assertEqual(set(ac.totals(lines, [])), {"elapsed_seconds", "time_to_first_result_seconds",
                                                     "human_active_seconds", "cost"})

    def test_session_cover_is_counted_apart_from_unrecorded(self):
        session = act("ACT-0003", "model", 4, 4, cost=measured(input_tokens=100, output_tokens=None, money_usd=None))
        session[0]["tool"] = "session"
        lines = act("ACT-0001", "file_read", 0, 1) + act("ACT-0002", "search", 2, 3) + session
        cost = ac.summarize(lines, [])["cost"]
        self.assertEqual((cost["groups"]["other"]["covered_by_session"], cost["groups"]["other"]["not_recorded"]), (1, 0))
        self.assertEqual((cost["groups"]["search_web"]["covered_by_session"],
                          cost["groups"]["search_web"]["not_recorded"]), (1, 0))
        self.assertEqual(cost["groups"]["model"]["measured"]["input_tokens"], 100)
        self.assertEqual(cost["total"]["basis"], "measured")

    def test_report_has_the_group_table(self):
        text = ac.render("c", ac.summarize(self.LINES, []))
        self.assertIn("| Группа | Действий |", text)
        self.assertIn("| Модель | 1 | 0 | 0 | 10 | 5 | — |", text)
        self.assertIn("| Поиск и чтение сайтов | 2 | 1 | 0 | 7 | — | — |", text)


class Totals(unittest.TestCase):
    def test_nobody_named_a_time(self):
        human = ac.summarize(act("ACT-0001", "human_input", 0, 0, refs=["DEC-0001"]), [])["human"]
        self.assertIsNone(human["reported_active_seconds"])

    def test_a_total_for_the_case_closes_earlier_answers(self):
        lines = (act("ACT-0001", "human_input", 0, 0, refs=["DEC-0001"])
                 + act("ACT-0002", "human_input", 5, 5, human={"help": "answer", "reported_active_seconds": 100})
                 + act("ACT-0003", "human_input", 9, 9, human={"help": "answer", "reported_active_seconds": 1800,
                                                               "covers_case": True}))
        self.assertEqual(ac.summarize(lines, [])["human"]["active_seconds"], 1800)

    def test_help_after_the_total_counts_again(self):
        lines = (act("ACT-0001", "human_input", 0, 0, human={"help": "answer", "reported_active_seconds": 1800,
                                                             "covers_case": True})
                 + act("ACT-0002", "human_input", 5, 5, refs=["DEC-0001"]))
        human = ac.summarize(lines, [])["human"]
        self.assertIsNone(human["active_seconds"])
        self.assertEqual((human["reported_active_seconds"], human["unreported"]), (1800, 1))

    def test_a_damaged_id_does_not_stop_the_summary(self):
        lines = act("ACT-0001", "human_input", 0, 0, refs=["DEC-0001"])
        lines[1]["id"] = ["x"]
        lines[0]["id"] = ["x"]
        self.assertEqual(ac.summarize(lines, [])["human"]["count"], 1)


class ReviewRegressions(unittest.TestCase):
    def test_action_without_end_makes_the_cost_unknown(self):
        lines = (act("ACT-0001", "model", 0, 1, cost=measured(input_tokens=100, output_tokens=1, money_usd=0.5))
                 + act("ACT-0002", "web_read", 2))
        summary = ac.summarize(lines, [])
        self.assertEqual(summary["cost"]["total"], UNKNOWN)
        self.assertEqual(summary["cost"]["actions"]["unknown"], 1)

    def test_odd_numbers_are_not_recorded_and_do_not_crash(self):
        for odd in (float("inf"), float("nan"), True, "12", -5, {"min": "a", "max": 3}):
            lines = act("ACT-0001", "model", 0, 1, cost=estimated("тариф 2026-11-06", input_tokens=odd, money_usd=None))
            summary = ac.summarize(lines, [])
            self.assertEqual(summary["cost"]["fields"]["input_tokens"]["not_recorded"], 1, odd)
            self.assertIsNone(ac.totals(lines, [])["cost"]["amounts"]["input_tokens"], odd)
            ac.render("c", summary)

    def test_huge_and_tiny_sums_stay_exact(self):
        lines = (act("ACT-0001", "model", 0, 1, cost=measured(input_tokens=1e22, money_usd=0.0000001))
                 + act("ACT-0002", "model", 2, 3, cost=measured(input_tokens=0.1, money_usd=0.004)))
        amounts = ac.totals(lines, [])["cost"]["amounts"]
        self.assertEqual(amounts["money_usd"], 0.0040001)
        self.assertIn("$0.0040001", ac.render("c", ac.summarize(lines, [])))

    def test_odd_human_time_and_timestamps(self):
        lines = (act("ACT-0001", "human_input", 0, 0, human={"help": "answer", "reported_active_seconds": True})
                 + [{"phase": "start", "id": "ACT-0002", "kind": "model", "at": "2026-11-06T12:00:00"}])
        summary = ac.summarize(lines, [{"id": "OPT-0001", "created_at": "2026-11-06T11:00:00"}])
        self.assertIsNone(summary["human"]["active_seconds"])
        self.assertIsNone(summary["time"]["first_result"])
        self.assertEqual(summary["time"]["elapsed_seconds"], 0)

    def test_named_zero_is_named(self):
        lines = (act("ACT-0001", "human_input", 0, 0, human={"help": "answer", "reported_active_seconds": 0})
                 + act("ACT-0002", "human_input", 1, 1, human={"help": "answer"}))
        self.assertIn("не названо для 1 из 2", ac.human_time(ac.summarize(lines, [])["human"]))

    def test_validator_rejects_infinite_amounts_and_time(self):
        self.assertIsNotNone(vp.cost_problem(measured(input_tokens=float("inf"))))
        self.assertIsNotNone(vp.cost_problem(estimated("2026-11-06", input_tokens={"min": 1, "max": float("inf")})))


class CaseCopy(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.case = self.tmp / "demo-case"
        shutil.copytree(DEMO, self.case)
        (self.case / REPORT).unlink()

    def codes(self):
        report = vp.validate_case(self.case)
        return sorted(f.code for f in report.errors), sorted(f.code for f in report.warnings)

    def journal(self):
        return [json.loads(line) for line in (self.case / JOURNAL).read_text(encoding="utf-8").splitlines() if line.strip()]

    def human(self, *extra):
        return run("action", "human", self.case, "--tool", "chat", "--environment", "codex", *extra, tool=ck)


class HumanHelpCommand(CaseCopy):
    def test_each_kind_of_help(self):
        cases = [("choice", ["--result-ref", "DEC-0002", "--active-minutes", "2.5"]),
                 ("answer", ["--result-ref", "SRC-0001", "--note", "Уточнил период"]),
                 ("material", ["--result-ref", "SRC-0002"]),
                 ("workaround", ["--note", "Сам скачал отчёт: сайт требовал вход"])]
        for kind, extra in cases:
            code, result = self.human("--help-kind", kind, *extra)
            self.assertEqual(code, 0, result)
            self.assertEqual(self.journal()[-1]["human"]["help"], kind)
        helped = [line["human"] for line in self.journal() if "human" in line]
        self.assertEqual(helped[-4]["reported_active_seconds"], 150)
        self.assertEqual(self.codes()[0], [])
        summary = ac.summarize(self.journal(), [])
        self.assertEqual(summary["human"]["by_help"], {"choice": 6, "answer": 1, "material": 1, "workaround": 1})

    def test_a_total_for_the_whole_case(self):
        code, _ = self.human("--help-kind", "answer", "--active-minutes", "45", "--covers-case")
        self.assertEqual((code, self.journal()[-1]["human"]),
                         (0, {"help": "answer", "reported_active_seconds": 2700, "covers_case": True}))
        self.assertEqual(self.codes()[0], [])
        self.assertEqual(ac.summarize(self.journal(), [])["human"]["active_seconds"], 2700)

    def test_a_total_needs_the_time(self):
        code, result = self.human("--help-kind", "answer", "--covers-case")
        self.assertEqual((code, result["refused"]), (ck.EXIT_REFUSED, "human_total_invalid"))

    def test_a_total_without_time_is_caught_by_the_validator(self):
        lines = self.journal() + act("ACT-0013", "human_input", 0, 0, human={"help": "answer", "covers_case": True})
        write_journal(self.case, lines)
        self.assertIn("ACTION_HUMAN", self.codes()[0])

    def test_money_estimate_without_a_date_is_refused_by_the_command(self):
        code, result = run("action", "start", self.case, "--kind", "model", "--tool", "t", "--environment", "codex",
                           tool=ck)
        self.assertEqual(code, 0, result)
        cost = self.tmp / "cost.json"
        cost.write_text(json.dumps(estimated("тариф API", input_tokens=None, output_tokens=None, money_usd=1)),
                        encoding="utf-8")
        code, refused = run("action", "end", self.case, "--id", result["id"], "--outcome", "completed",
                            "--cost-file", cost, tool=ck)
        self.assertEqual((code, refused["refused"]), (ck.EXIT_REFUSED, "cost_invalid"))

    def test_impossible_or_glued_dates_do_not_count(self):
        for source, valid in (("тариф 9999-99-99", False), ("id 1234567890-12-3456", False), ("тариф 2026-11-06", True)):
            problem = vp.cost_problem(estimated(source, input_tokens=None, output_tokens=None, money_usd=1))
            self.assertEqual(problem is None, valid, source)

    def test_only_a_choice_can_stand_behind_a_decision(self):
        lines = self.journal()
        for line in lines:
            if line["phase"] == "end" and "DEC-0002" in line.get("result_refs", []) and line["id"] in self.humans(lines):
                line["human"] = {"help": "workaround", "note": "Перезапустил"}
        write_journal(self.case, lines)
        self.assertIn("DECISION_NOT_HUMAN", self.codes()[0])

    @staticmethod
    def humans(lines):
        return {line["id"] for line in lines if line.get("kind") == "human_input"}

    def test_default_is_a_choice_as_before(self):
        code, _ = self.human("--result-ref", "DEC-0002")
        self.assertEqual((code, self.journal()[-1]["human"]), (0, {"help": "choice"}))

    def test_refusals_write_nothing(self):
        before = (self.case / JOURNAL).read_bytes()
        for extra, reason in ((["--help-kind", "choice"], "human_result_invalid"),
                              (["--help-kind", "choice", "--result-ref", "SRC-0001"], "human_result_invalid"),
                              (["--help-kind", "workaround"], "human_note_missing"),
                              (["--help-kind", "workaround", "--note", "  "], "human_note_missing"),
                              (["--help-kind", "answer", "--result-ref", "SRC-0099"], "ref_unknown"),
                              (["--help-kind", "answer", "--active-minutes", "-1"], "human_time_invalid"),
                              (["--help-kind", "answer", "--active-minutes", "nan"], "human_time_invalid"),
                              (["--help-kind", "answer", "--active-minutes", "1e308"], "human_time_invalid"),
                              (["--help-kind", "answer", "--note", "x" * 301], "schema")):
            code, result = self.human(*extra)
            self.assertEqual((code, result["refused"]), (1, reason), extra)
        self.assertEqual((self.case / JOURNAL).read_bytes(), before)

    def test_validator_catches_hand_edits(self):
        lines = self.journal()
        for line in lines:
            if line["id"] == "ACT-0008" and line["phase"] == "end":
                line["human"] = {"help": "choice"}
                line["result_refs"] = []
        write_journal(self.case, lines)
        errors = self.codes()[0]
        self.assertIn("ACTION_HUMAN", errors)

    def test_validator_rejects_infinite_reported_time(self):
        lines = self.journal()
        for line in lines:
            if line["id"] == "ACT-0008" and line["phase"] == "end":
                line["human"] = {"help": "choice", "reported_active_seconds": 12345}
        write_journal(self.case, lines)
        data = (self.case / JOURNAL).read_bytes().replace(b"12345", b"1e400")
        (self.case / JOURNAL).write_bytes(data)
        self.assertEqual(self.codes()[0], ["ACTION_HUMAN"])
        code, result = run("summary", self.case)
        self.assertEqual((code, result["human"]["active_seconds"]), (0, None))

    def test_validator_rejects_an_undated_money_estimate(self):
        lines = self.journal()
        lines[-1]["cost"] = estimated("тариф API", money_usd=0.5)
        write_journal(self.case, lines)
        self.assertEqual(self.codes()[0], ["ACTION_COST"])


class SaveAndReport(CaseCopy):
    def test_save_writes_totals_from_the_accounting_module(self):
        request = self.tmp / "save.json"
        request.write_text(json.dumps({"stage": "strategies", "open_items": [], "next_step": "План проверки"}),
                           encoding="utf-8")
        code, result = run("save", self.case, "--from", request, tool=ck)
        self.assertEqual(code, 0, result)
        document = json.loads((self.case / "state/checkpoints" / f"{result['id']}.json").read_text(encoding="utf-8"))
        records = json.loads((self.case / OPTIONS).read_text(encoding="utf-8"))["records"]
        self.assertEqual(document["totals"], ac.totals(self.journal(), records, ck.case_created_at(self.case)))
        self.assertEqual(document["totals"]["cost"], UNKNOWN)
        self.assertEqual(self.codes()[0], [])

    def test_save_keeps_an_estimated_total_valid(self):
        source = "тариф API на 2026-11-06"
        write_journal(self.case, self.journal() + act(
            "ACT-0013", "model", 0, 1, cost=estimated(source, input_tokens=None, output_tokens=None,
                                                      money_usd={"min": 1, "max": 2})))
        lines = self.journal()
        for line in lines:
            if line["phase"] == "end" and line["id"] != "ACT-0013" and "human" not in line:
                line["cost"] = measured(input_tokens=None, output_tokens=None, money_usd=None)
            if line["phase"] == "end" and line.get("cost", {}).get("basis") == "unknown":
                line["cost"] = measured(input_tokens=None, output_tokens=None, money_usd=None)
        write_journal(self.case, lines)
        request = self.tmp / "save.json"
        request.write_text(json.dumps({"stage": "strategies", "open_items": [], "next_step": "План проверки"}),
                           encoding="utf-8")
        code, result = run("save", self.case, "--from", request, tool=ck)
        self.assertEqual(code, 0, result)
        document = json.loads((self.case / "state/checkpoints" / f"{result['id']}.json").read_text(encoding="utf-8"))
        self.assertEqual(document["totals"]["cost"]["basis"], "estimated")
        self.assertIn("2026-11-06", document["totals"]["cost"]["estimate_source"])
        self.assertEqual(self.codes()[0], [])

    def test_report_names_the_sources(self):
        code, result = run("report", self.case)
        self.assertEqual(code, 0, result)
        records = json.loads((self.case / "evidence/sources.json").read_text(encoding="utf-8"))["records"]
        active = sum(record["record_status"] == "active" for record in records)
        self.assertEqual(result["sources"], active)
        self.assertIn(f"Источников: {active}", (self.case / REPORT).read_text(encoding="utf-8"))

    def test_report_is_deterministic_and_passes_the_validator(self):
        lines = self.journal() + act("ACT-0013", "compute", 0, 1, outcome="failed") + act(
            "ACT-0014", "compute", 2, 3, retry=("ACT-0013", "Ссылка [CLM-0001] была неверной | таблица"))
        write_journal(self.case, lines)
        code, result = run("report", self.case)
        self.assertEqual((code, result["report"]), (0, REPORT), result)
        first = (self.case / REPORT).read_bytes()
        run("report", self.case)
        self.assertEqual((self.case / REPORT).read_bytes(), first)
        text = first.decode("utf-8")
        self.assertNotIn("[", text)
        for heading in ("## Итог", "## Время", "## Действия", "## Ошибки и повторы", "## Помощь человека", "## Расход",
                        "## Как считалось"):
            self.assertIn(heading, text)
        self.assertEqual(self.codes()[0], [])

    def test_summary_reads_only(self):
        before = sorted(p.as_posix() for p in self.case.rglob("*"))
        code, result = run("summary", self.case)
        self.assertEqual((code, result["status"]), (0, "done"))
        self.assertIn("data_notice", result)
        self.assertEqual(sorted(p.as_posix() for p in self.case.rglob("*")), before)

    def test_torn_last_line_does_not_stop_the_summary(self):
        with open(self.case / JOURNAL, "ab") as handle:
            handle.write(b'{"format_version": 1, "id": "ACT-00')
        code, result = run("summary", self.case)
        self.assertEqual((code, result["actions"]["total"]), (0, 12))
        self.assertIn("JOURNAL_TORN", result["warnings"])

    def test_corrupt_journal_and_missing_case_are_refused(self):
        code, result = run("summary", self.tmp / "nope")
        self.assertEqual((code, result["refused"]), (4, "case_missing"))
        data = (self.case / JOURNAL).read_bytes()
        (self.case / JOURNAL).write_bytes(b"not json\n" + data)
        code, result = run("report", self.case)
        self.assertEqual((code, result["refused"]), (4, "journal_corrupt"))
        self.assertFalse((self.case / REPORT).exists())

    def test_report_path_that_is_not_a_file_is_refused(self):
        (self.case / REPORT).mkdir(parents=True)
        code, result = run("report", self.case)
        self.assertEqual((code, result["refused"]), (4, "unsafe_path"))


if __name__ == "__main__":
    unittest.main()
