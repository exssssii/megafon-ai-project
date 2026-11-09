"""Report of an isolated live run: what it counts as a trace and what it reads after a failure."""
import hashlib
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("live_report", ROOT / "scripts" / "live_report.py")
report = importlib.util.module_from_spec(spec)
sys.modules["live_report"] = report
spec.loader.exec_module(report)

CLONE = "D:/runs/b11/clone"


def shell(command):
    return report.Call(command, None, True, True)


def codex(command, cwd=None):
    return report.Call(command, cwd, False, True)


class DotDotTraces(unittest.TestCase):
    def traces(self, *calls):
        return report.dotdot_traces(list(calls), CLONE)

    def test_path_inside_the_clone_is_no_trace(self):
        self.assertEqual(self.traces(shell("cd projects/retention-smb && python ../../tools/gate.py check . done")), {})

    def test_cd_carries_to_the_next_call_in_claude_code(self):
        self.assertEqual(self.traces(shell("cd projects/case"), shell("cat ../../tools/calc.py")), {})

    def test_path_out_of_the_clone_is_a_trace(self):
        self.assertEqual(self.traces(shell("cat ../held/saved-page.md")), {"outside": 1})

    def test_going_up_twice_from_the_case_folder_and_one_more(self):
        self.assertEqual(self.traces(shell("cd projects/case && cat ../../../results/x")), {"outside": 1})

    def test_backslashes_count_too(self):
        self.assertEqual(self.traces(shell(r"cd ..\..\results")), {"outside": 1})

    def test_cd_to_a_variable_leaves_it_unresolved(self):
        self.assertEqual(self.traces(shell('cd "$CASE" && cat ../notes.md')), {"unresolved": 1})

    def test_variable_in_the_path_itself_is_unresolved(self):
        self.assertEqual(self.traces(shell("cat $HOME/../secret")), {"unresolved": 1})

    def test_cwd_of_a_codex_event_wins_and_does_not_carry(self):
        calls = [codex("cat ../../tools/calc.py", CLONE + "/projects/case"), codex("cat ../../tools/calc.py")]
        self.assertEqual(self.traces(*calls), {"outside": 1})

    def test_cd_inside_a_shell_wrapper(self):
        command = "powershell.exe -Command 'cd projects/case; Get-Content ../../tools/calc.py'"
        self.assertEqual(self.traces(codex(command)), {})

    def test_git_bash_form_of_the_clone(self):
        self.assertEqual(self.traces(shell("cd /d/runs/b11/clone/projects/case && cat ../../tools/calc.py")), {})

    def test_absolute_path_that_leaves_the_clone(self):
        self.assertEqual(self.traces(shell("cat D:/runs/b11/clone/../results/x")), {"outside": 1})

    def test_ranges_and_addresses_are_not_paths(self):
        self.assertEqual(self.traces(shell("seq 1..5 && curl https://a.example/x/../y")), {})

    def test_a_carried_folder_is_only_a_guess(self):
        self.assertEqual(self.traces(shell("cd /d/runs/b11/clone"), shell("cd ../..")), {"unresolved": 1})

    def test_a_folder_set_in_the_same_call_is_known(self):
        self.assertEqual(self.traces(shell("cd projects/case"), shell(f"cd {CLONE} && cat ../x")), {"outside": 1})

    def test_cd_in_a_subshell_does_not_stay(self):
        self.assertEqual(self.traces(shell("(cd projects/case && ls) && cat ../x")), {"unresolved": 1})

    def test_popd_loses_the_folder(self):
        self.assertEqual(self.traces(shell("pushd projects/case && popd && cat ../x")), {"unresolved": 1})

    def test_bare_cd_loses_the_folder(self):
        self.assertEqual(self.traces(shell("cd projects/case && cd && cat ../x")), {"unresolved": 1})

    def test_redirect_after_cd_loses_the_folder(self):
        self.assertEqual(self.traces(shell("cd projects/case 2>/dev/null && cat ../../../x")), {"unresolved": 1})

    def test_dotdot_after_a_prefix_counts(self):
        self.assertEqual(self.traces(shell("curl -d @../secret x"), shell("ls -Path:..\\secret")), {"outside": 2})

    def test_glob_pattern_counts(self):
        self.assertEqual(self.traces(report.Call("../../**/*.json", None, False, False)), {"outside": 1})

    def test_every_path_counts(self):
        self.assertEqual(self.traces(shell("cat ../a ../b")), {"outside": 2})


class ToolCalls(unittest.TestCase):
    def test_claude_bash_carries_and_read_gives_a_path(self):
        event = {"type": "assistant", "message": {"content": [
            {"type": "tool_use", "name": "Bash", "input": {"command": "ls"}},
            {"type": "tool_use", "name": "Read", "input": {"file_path": "../x.md"}},
            {"type": "tool_use", "name": "Write", "input": {"file_path": "a.md", "content": "see ../y"}},
        ]}}
        self.assertEqual(report.tool_calls(event), [
            report.Call("ls", None, True, True),
            report.Call("../x.md", None, False, False),
            report.Call("a.md", None, False, False),
        ])

    def test_glob_gives_its_pattern(self):
        event = {"type": "assistant", "message": {"content": [
            {"type": "tool_use", "name": "Glob", "input": {"pattern": "../*.md"}}]}}
        self.assertEqual(report.tool_calls(event), [report.Call("../*.md", None, False, False)])

    def test_codex_command_counts_once(self):
        item = {"type": "command_execution", "command": "cat ../a", "cwd": CLONE}
        self.assertEqual(report.tool_calls({"type": "item.started", "item": item}), [])
        self.assertEqual(report.tool_calls({"type": "item.completed", "item": item}),
                         [report.Call("cat ../a", CLONE, False, True)])

    def test_codex_file_change_gives_its_paths(self):
        item = {"type": "file_change", "changes": [{"path": "../x.md"}]}
        self.assertEqual(report.tool_calls({"type": "item.completed", "item": item}),
                         [report.Call("../x.md", None, False, False)])


class CutOffCommand(unittest.TestCase):
    def test_command_without_a_completed_event_counts_once(self):
        with tempfile.TemporaryDirectory() as tmp:
            results = Path(tmp)
            cut = {"type": "item.started", "item": {"id": "a", "type": "command_execution", "command": "cat ../a"}}
            done = {"id": "b", "type": "command_execution", "command": "cat ../b"}
            lines = [cut, {"type": "item.started", "item": done}, {"type": "item.completed", "item": done}]
            (results / "c2.jsonl").write_text("\n".join(json.dumps(x) for x in lines), encoding="utf-8")
            self.assertEqual(report.hidden_traces(results, CLONE), {".. outside": 2})


class Canary(unittest.TestCase):
    def test_counts_logs_and_case_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            results, projects = Path(tmp, "results"), Path(tmp, "projects")
            (projects / "case").mkdir(parents=True)
            results.mkdir()
            (results / "c1.jsonl").write_text("value-1 and value-1", encoding="utf-8")
            (projects / "case" / "note.md").write_text("value-1", encoding="utf-8")
            (projects / "case" / "other.md").write_text("nothing", encoding="utf-8")
            self.assertEqual(report.canary_seen(results, projects, "value-1"), 3)

    def test_no_value_counts_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(report.canary_seen(Path(tmp), Path(tmp), ""), 0)


class AfterFailure(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.results = self.root / "results"
        self.case = self.root / "projects" / "case"
        (self.case / "state/checkpoints").mkdir(parents=True)
        self.results.mkdir()

    def sources(self, folder, count):
        path = folder / "evidence/sources.json"
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps({"records": [{"id": f"SRC-{i}"} for i in range(count)]}), encoding="utf-8")

    def test_damaged_checkpoint_does_not_stop_the_report(self):
        path = self.case / "state/checkpoints/CKP-0001.json"
        path.write_text('{"stage": "do', encoding="utf-8")
        self.assertEqual(report.stage_of(path), "unreadable")
        path.write_text('{"stage": "done"}', encoding="utf-8")
        self.assertEqual(report.stage_of(path), "done")

    def test_sources_before_and_after_the_tool_failure(self):
        self.sources(self.results / "after-final", 7)
        self.sources(self.results / "before-damage", 8)
        lines = report.fault_lines(self.results, self.case)
        self.assertIn("src_before 7", lines)
        self.assertIn("src_after 8", lines)

    def test_damaged_registry_does_not_stop_the_report(self):
        folder = self.results / "after-final/evidence"
        folder.mkdir(parents=True)
        (folder / "sources.json").write_text('{"records": [', encoding="utf-8")
        self.assertIn("src_before unreadable", report.fault_lines(self.results, self.case))

    def test_missing_folder_is_shown_not_counted_as_zero(self):
        self.assertIn("src_before none", report.fault_lines(self.results, self.case))

    def test_damaged_file_found_under_any_name(self):
        damaged = b'{"stage"'
        sha = hashlib.sha256(damaged).hexdigest().upper()
        (self.results / "checks.txt").write_text(f"validate_final 0\ndamaged_sha {sha}\n", encoding="utf-8-sig")
        self.assertIn("damaged_kept False", report.fault_lines(self.results, self.case))
        (self.case / "state/checkpoints/.lost-CKP-0003.json").write_bytes(damaged)
        lines = report.fault_lines(self.results, self.case)
        self.assertIn("damaged_kept True", lines)
        self.assertIn("validate_final 0", lines)

    def test_break_state_is_reported(self):
        (self.results / "c2.break.txt").write_text("broken inside the stage", encoding="utf-8-sig")
        self.assertIn("break c2: broken inside the stage", report.fault_lines(self.results, self.case))


if __name__ == "__main__":
    unittest.main()


class RunHygiene(unittest.TestCase):
    def test_internal_words_of_the_agent_are_named(self):
        text = "Тексты сохранены через веб-инструмент, по процедуре tools/calc.py; итог в outputs/plan.md."
        self.assertEqual(report.internal_words(text), ["calc.py", "tools/", "веб-инструмент", "процедуре"])
        self.assertEqual(report.internal_words("Отток одиночек 6,27 % за январь–сентябрь."), [])
        self.assertEqual(report.internal_words("Ваш выбор DEC-0005 записан, расчёт CALC-0012."), ["calc-0012", "dec-0005"])

    def test_plugins_that_ship_with_the_environment_are_not_the_user_s(self):
        plugins = [{"name": "cc-plugin-telemetry", "path": "builtin", "source": "cc-plugin-telemetry@builtin"},
                   {"name": "superpowers", "path": "C:/cache/superpowers", "source": "superpowers@market"}, "loose"]
        self.assertEqual(report.user_plugins(plugins), ["superpowers", "loose"])
        self.assertEqual(report.user_plugins(None), [])

    def test_runtime_version_is_counted_on_the_starts_that_carry_the_environment(self):
        with tempfile.TemporaryDirectory() as tmp:
            case = Path(tmp)
            self.assertEqual(report.runtime_lines(case), ["runtime_version none"])
            (case / "state").mkdir()
            lines = [{"phase": "start", "runtime": {"version": None, "access": "unknown"}},
                     {"phase": "end"}, {"phase": "start", "runtime": {"version": "2.1.304"}}, "broken"]
            (case / "state/actions.jsonl").write_text(
                "\n".join(x if isinstance(x, str) else json.dumps(x) for x in lines), encoding="utf-8")
            self.assertEqual(report.runtime_lines(case), ["runtime_version given=2 missing=1"])
