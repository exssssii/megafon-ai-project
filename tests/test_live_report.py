"""Report of an isolated live run: what it counts as a trace and what it reads after a failure."""
import hashlib
import importlib.util
import json
import shutil
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

    def test_powershell_range_is_no_path(self):
        command = "$t = Get-Content x; $t[($start-1)..($end-2)]; $t[0..3]; $t[$a..$b]"
        self.assertEqual(self.traces(codex(command)), {})

    def test_bare_parent_folder_still_counts_next_to_a_range(self):
        self.assertEqual(self.traces(codex("$t[($a)..($b)]; Get-Content ..")), {"outside": 1})

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

    def test_a_redirect_does_not_change_where_cd_goes(self):
        # From the final run: cd with 2>/dev/null and back to the clone root was counted unresolved.
        command = "cd projects/direction-b2c 2>/dev/null; cd ../..; .venv/Scripts/python.exe tools/checkpoint.py x"
        self.assertEqual(self.traces(shell(command)), {})
        self.assertEqual(self.traces(shell("cd projects/case 2>/dev/null && cat ../../../x")), {"outside": 1})
        self.assertEqual(self.traces(shell("cd projects/case >/dev/null 2>&1 && cat ../../x")), {})

    def test_a_heredoc_body_is_text_not_a_path(self):
        command = ("cd projects/case && cat > outputs/brief.md <<'EOF'\n- [pilot](../../inbox/case/pilot.pdf)\nEOF\n"
                   "cat ../x")
        self.assertEqual(self.traces(shell(command)), {"in_text": 1})

    def test_a_powershell_here_string_body_is_text_not_a_path(self):
        command = ("pwsh -Command '@'\ncontent = \"[a](../../inbox/a.csv) [b](../../inbox/b.csv)\"\n'@ | python -'")
        self.assertEqual(self.traces(codex(command)), {"in_text": 2})

    def test_the_command_after_a_heredoc_still_counts(self):
        command = "python - <<EOF\nprint(1)\nEOF\ncat ../../secret"
        self.assertEqual(self.traces(shell(command)), {"outside": 1})

    def test_a_listing_of_the_folder_above_the_clone_is_a_trace(self):
        command = "pwsh -Command 'Get-ChildItem -Path .. -Filter AGENTS.md -Recurse'"
        self.assertEqual(self.traces(codex(command)), {"outside": 1})

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
            self.assertEqual(report.hidden_traces(results, CLONE), ({".. outside": 2}, {}))

    def test_dotdot_in_written_text_does_not_stop_the_run(self):
        with tempfile.TemporaryDirectory() as tmp:
            results = Path(tmp)
            item = {"id": "a", "type": "command_execution", "command": "cat > a.md <<EOF\n[x](../../inbox/x)\nEOF"}
            (results / "c1.jsonl").write_text(json.dumps({"type": "item.completed", "item": item}), encoding="utf-8")
            self.assertEqual(report.hidden_traces(results, CLONE), ({}, {".. in_text": 1}))


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


class Skills(unittest.TestCase):
    def test_skills_seen_at_start_are_named(self):
        event = {"type": "system", "subtype": "init", "skills": ["resuming-case", {"name": "design"}]}
        self.assertEqual(report.skills_of(event), ["resuming-case", "design"])
        self.assertEqual(report.skills_of({"type": "result"}), [])

    def test_skill_calls_in_claude_code_are_counted(self):
        event = {"type": "assistant", "message": {"content": [
            {"type": "tool_use", "name": "Skill", "input": {"skill": "revising-on-new-data"}},
            {"type": "tool_use", "name": "Read", "input": {"file_path": ".claude/skills/resuming-case/SKILL.md"}},
            {"type": "tool_use", "name": "Read", "input": {"file_path": "agent/workflows/revision.md"}},
        ]}}
        self.assertEqual(report.skill_calls(event), ["revising-on-new-data", "resuming-case"])

    def test_codex_reads_the_skill_file(self):
        item = {"type": "command_execution", "command": r"Get-Content .agents\skills\running-interviews\SKILL.md"}
        self.assertEqual(report.skill_calls({"type": "item.completed", "item": item}), ["running-interviews"])
        self.assertEqual(report.skill_calls({"type": "item.started", "item": item}), [])

    def test_report_line_lists_skills(self):
        with tempfile.TemporaryDirectory() as tmp:
            results = Path(tmp)
            (results / "checks.txt").write_text("skills on project=resuming-case,running-interviews seen=see the start event\n",
                                                encoding="utf-8")
            init = {"type": "system", "subtype": "init", "skills": ["resuming-case", "running-interviews", "design"]}
            call = {"type": "assistant", "message": {"content": [
                {"type": "tool_use", "name": "Skill", "input": {"skill": "resuming-case"}}]}}
            (results / "c1.jsonl").write_text(json.dumps(init) + "\n" + json.dumps(call), encoding="utf-8")
            out = []
            report.read_logs(results, out)
            self.assertIn("skills seen=['resuming-case', 'running-interviews', 'design'] other=['design'] "
                          "called=['resuming-case']", out)

    def test_codex_without_a_skill_file_read_is_unknown(self):
        with tempfile.TemporaryDirectory() as tmp:
            results = Path(tmp)
            (results / "checks.txt").write_text("skills on project=resuming-case seen=resuming-case\n", encoding="utf-8")
            item = {"type": "command_execution", "command": "ls"}
            (results / "c1.jsonl").write_text(json.dumps({"type": "item.completed", "item": item}), encoding="utf-8")
            out = []
            report.read_logs(results, out)
            self.assertIn("skills seen=['resuming-case'] other=[] called=unknown", out)


    def skill_report(self, checks, *events):
        with tempfile.TemporaryDirectory() as tmp:
            results = Path(tmp)
            if checks is not None:
                (results / "checks.txt").write_text(checks + "\n", encoding="utf-8")
            (results / "c1.jsonl").write_text("\n".join(json.dumps(e) for e in events), encoding="utf-8")
            out = []
            report.read_logs(results, out)
            return [line for line in out if line.startswith("skills ")]

    def test_failed_codex_probe_is_not_a_clean_list(self):
        self.assertEqual(self.skill_report("skills on project=resuming-case seen=error", {"type": "turn.completed"}),
                         ["skills seen=unknown other=unknown called=unknown"])

    def test_claude_without_a_start_event_is_unknown(self):
        self.assertEqual(self.skill_report("skills on project=resuming-case seen=see the start event",
                                           {"type": "result", "result": "x"}),
                         ["skills seen=unknown other=unknown called=[]"])

    def test_codex_with_skills_off_has_no_calls(self):
        self.assertEqual(self.skill_report("skills off project=resuming-case seen=", {"type": "turn.completed"}),
                         ["skills seen=[] other=[] called=[]"])

if __name__ == "__main__":
    unittest.main()


class RunHygiene(unittest.TestCase):
    def test_internal_words_of_the_agent_are_named(self):
        text = "Тексты сохранены через веб-инструмент, по процедуре tools/calc.py; итог в outputs/plan.md."
        self.assertEqual(report.internal_words(text), ["calc.py", "tools/", "веб-инструмент", "процедуре"])
        self.assertEqual(report.internal_words("Отток одиночек 6,27 % за январь–сентябрь."), [])
        self.assertEqual(report.internal_words("Ваш выбор DEC-0005 записан, расчёт CALC-0012."), ["calc-0012", "dec-0005"])

    def test_threshold_lines_count_success_and_failure_with_a_number(self):
        text = ("**Предложенные пороги**\n- **Успех:** отток не выше 4,5%.\n- **Неуспех:** отток не ниже 5,5%.\n"
                "| Технический успех | Снижение минимум на 30% |\nПлан проверки написан.\nУспешно сохранено.")
        self.assertEqual(report.threshold_lines(text), 3)
        self.assertEqual(report.threshold_lines("План проверки написан, пороги покажу вместе с ответственными."), 0)
        self.assertEqual(report.threshold_lines("Успех без числа пока не назван."), 0)

    def test_shown_text_takes_every_message_the_user_sees(self):
        codex = {"type": "item.completed", "item": {"type": "agent_message", "text": "Вы выбрали «только надёжность»."}}
        claude = {"type": "assistant", "message": {"content": [{"type": "text", "text": "Цель"},
                                                               {"type": "tool_use", "name": "Bash"},
                                                               {"type": "text", "text": "выбор"}]}}
        sub = dict(claude, parent_tool_use_id="toolu_1")
        self.assertEqual(report.shown_text(codex), "Вы выбрали «только надёжность».")
        self.assertEqual(report.shown_text(claude), "Цель\nвыбор")
        self.assertEqual(report.shown_text(sub), "")
        self.assertEqual(report.shown_text({"type": "item.completed", "item": {"type": "reasoning", "text": "x"}}), "")

    def test_messages_file_keeps_what_came_before_the_final_answer(self):
        with tempfile.TemporaryDirectory() as folder:
            log = Path(folder) / "c3-1.jsonl"
            events = [{"type": "item.completed", "item": {"type": "agent_message", "text": "Вы выбрали «только надёжность»."}},
                      {"type": "item.completed", "item": {"type": "agent_message", "text": "Кейс доведён до согласования."}}]
            log.write_text("\n".join(json.dumps(e, ensure_ascii=False) for e in events), encoding="utf-8")
            out = []
            report.read_logs(Path(folder), out)
            self.assertEqual((Path(folder) / "c3-1.final.txt").read_text(encoding="utf-8"), "Кейс доведён до согласования.")
            self.assertIn("только надёжность", (Path(folder) / "c3-1.messages.txt").read_text(encoding="utf-8"))
            self.assertIn("messages=2 threshold_lines=0", out[0])

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
            self.assertEqual(report.runtime_lines(case), ["runtime_version given=2 missing=1 session=0"])

    def test_records_of_a_separate_agent_are_matched_to_its_calls(self):
        with tempfile.TemporaryDirectory() as tmp:
            results, case = Path(tmp) / "results", Path(tmp) / "case"
            (case / "state/reviews").mkdir(parents=True)
            (case / "sources/interviews").mkdir(parents=True)
            (case / "state/reviews/review-0001.md").write_text("x", encoding="utf-8")
            (case / "sources/interviews/synthetic-a-1.md").write_text("Изоляция: да\n", encoding="utf-8")
            (case / "sources/interviews/synthetic-b-1.md").write_text("Изоляция: нет\n", encoding="utf-8")
            results.mkdir()
            agent = {"type": "tool_use", "name": "Agent", "input": {"subagent_type": "reviewer"}}
            inner = {"type": "assistant", "parent_tool_use_id": "t1", "message": {"content": [agent]}}
            wait = {"type": "collab_tool_call", "tool": "wait", "receiver_thread_ids": []}
            lines = [{"type": "assistant", "message": {"content": [agent]}}, inner,
                     {"type": "item.completed", "item": wait}]
            (results / "c3-1.jsonl").write_text("\n".join(map(json.dumps, lines)), encoding="utf-8")
            # A wait of Codex with no session files cannot tell whether an agent ran.
            self.assertEqual(report.subagent_lines(results, case), [
                "subagents calls={'reviewer': 1} codex_collab={'wait_for_none': 1} codex_threads=unknown reviews=1"
                " isolated_talks=1 unproven=unknown"])
            (results / "c3-1.jsonl").write_text(json.dumps(lines[0]), encoding="utf-8")
            self.assertEqual(report.subagent_lines(results, case), [
                "subagents calls={'reviewer': 1} codex_collab={} codex_threads={} reviews=1 isolated_talks=1 unproven=1"])

    def test_records_from_the_transfer_archive_are_not_this_run_s(self):
        # From the resume run: the three reviews of the earlier run came in the archive.
        import zipfile
        with tempfile.TemporaryDirectory() as tmp:
            results, case = Path(tmp) / "results", Path(tmp) / "case"
            results.mkdir()
            (case / "state/reviews").mkdir(parents=True)
            for name in ("review-0001.md", "review-0002.md"):
                (case / "state/reviews" / name).write_text("x", encoding="utf-8")
            with zipfile.ZipFile(results / "handoff-received.zip", "w") as archive:
                archive.writestr("case\\state\\reviews\\review-0001.md", "x")
            self.assertEqual(report.subagent_lines(results, case, Path(tmp) / "none"), [
                "subagents calls={} codex_collab={} codex_threads={} reviews=1 isolated_talks=0 unproven=1"])

    def test_codex_subagents_are_read_from_the_session_files(self):
        # From the canary of D46: exec logs only a wait, the subagent writes its own session file.
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "codex-home"
            day = home / "sessions/2026/11/14"
            day.mkdir(parents=True)
            metas = {"a": {"id": "p", "thread_source": "user"},
                     "g": {"id": "g1", "parent_thread_id": "p", "thread_source": "guardian_review"},
                     "b": {"id": "c1", "parent_thread_id": "p", "thread_source": "subagent", "agent_role": "reviewer"},
                     "c": {"id": "c2", "parent_thread_id": "p", "thread_source": "subagent",
                           "agent_path": "/root/respondent_1"}}
            for name, meta in metas.items():
                (day / f"rollout-{name}.jsonl").write_text(
                    json.dumps({"type": "session_meta", "payload": meta}) + "\n{}\n", encoding="utf-8")
            self.assertEqual(report.codex_subagents(home), {"reviewer": 1, "respondent": 1})
            results, case = Path(tmp) / "results", Path(tmp) / "case"
            results.mkdir()
            (case / "state/reviews").mkdir(parents=True)
            (case / "state/reviews/review-0001.md").write_text("x", encoding="utf-8")
            wait = {"type": "item.completed", "item": {"type": "collab_tool_call", "tool": "wait", "receiver_thread_ids": []}}
            (results / "c3-1.jsonl").write_text(json.dumps(wait), encoding="utf-8")
            self.assertEqual(report.subagent_lines(results, case, home), [
                "subagents calls={'reviewer': 1, 'respondent': 1} codex_collab={'wait_for_none': 1}"
                " codex_threads={'reviewer': 1, 'respondent': 1} reviews=1 isolated_talks=0 unproven=0"])

    def test_a_session_record_of_the_cost_is_no_missing_version(self):
        # From the final run: the record of the cost the user reported carries no version of the environment.
        with tempfile.TemporaryDirectory() as tmp:
            case = Path(tmp)
            (case / "state").mkdir()
            lines = [{"phase": "start", "tool": "session", "runtime": {"version": None, "access": "subscription"}},
                     {"phase": "start", "tool": "chat", "runtime": {"version": "2.1.304"}}]
            (case / "state/actions.jsonl").write_text("\n".join(map(json.dumps, lines)), encoding="utf-8")
            self.assertEqual(report.runtime_lines(case), ["runtime_version given=1 missing=0 session=1"])


class Routing(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.results, self.case = self.root / "results", self.root / "case"
        self.results.mkdir()
        for folder in ("sources/interviews", "state/interviews", "state"):
            (self.case / folder).mkdir(parents=True, exist_ok=True)

    def talk(self, folder, name, *header):
        text = "\n".join(["# Беседа", *header, "", "## Вопрос 1", "Модель: не шапка", ""])
        (self.case / folder / name).write_text(text, encoding="utf-8")

    def calls(self, *models, name="s1.jsonl"):
        events = []
        for model in models:
            given = {"subagent_type": "respondent", **({"model": model} if model else {})}
            part = {"type": "tool_use", "name": "Agent", "input": given}
            events.append({"type": "assistant", "parent_tool_use_id": None, "message": {"content": [part]}})
        (self.results / name).write_text("\n".join(map(json.dumps, events)), encoding="utf-8")

    def line(self, home=None):
        (line,) = report.routing_lines(self.results, self.case, home)
        return line

    def test_routing_line_counts_a_routed_talk(self):
        self.talk("sources/interviews", "a.md", "Модель: claude-haiku-5-5 (маршрут)", "Проверка роли: пройдена")
        self.calls("haiku")
        self.assertEqual(self.line(), "routing talks=1 cheap=1 retried=0 failed_twice=0 called_with_model=1"
                                      " cost_by_model=unknown")

    def test_routing_line_counts_a_retry_and_a_rejected_file(self):
        self.talk("state/interviews", "a.md", "Модель: claude-haiku-5-5 (маршрут)")
        self.talk("sources/interviews", "a.md", "Модель: claude-sonnet-5-5 (повтор)", "Проверка роли: пройдена")
        lines = [{"phase": "start", "retry_of": "A1", "retry_reason": "role_check: TOO_LONG"},
                 {"phase": "start", "retry_of": "A2", "retry_reason": "роль не загружена"}, {"phase": "end"}]
        (self.case / "state/actions.jsonl").write_text("\n".join(map(json.dumps, lines)), encoding="utf-8")
        self.calls("haiku")
        self.assertIn("talks=1 cheap=1 retried=1 failed_twice=0", self.line())

    def test_routing_line_counts_a_twice_failed_talk(self):
        self.talk("sources/interviews", "a.md", "Проверка роли: не пройдена (TOO_LONG)")
        self.talk("state/interviews", "b.md", "Проверка роли: не пройдена (TOO_LONG)")
        self.assertIn("failed_twice=1", self.line())

    def test_routing_line_flags_a_model_written_without_a_call(self):
        for name in ("a.md", "b.md"):
            self.talk("sources/interviews", name, "Модель: claude-haiku-5-5 (маршрут)")
        self.calls("haiku", None)
        line = self.line()
        self.assertIn("cheap=2 retried=0 failed_twice=0 called_with_model=1 flag=cheap_without_call", line)

    def test_routing_line_without_the_routing_file(self):
        self.talk("sources/interviews", "a.md", "Изоляция: да")
        self.calls(None)
        line = self.line()
        self.assertIn("talks=1 cheap=0", line)
        self.assertIn("called_with_model=0", line)
        self.assertNotIn("flag=", line)

    def test_routing_ignores_talks_from_the_transfer_archive(self):
        import zipfile
        self.talk("sources/interviews", "old.md", "Модель: x (маршрут)", "Проверка роли: не пройдена (TOO_LONG)")
        self.talk("state/interviews", "old2.md", "Модель: x (маршрут)")
        with zipfile.ZipFile(self.results / "handoff-received.zip", "w") as archive:
            archive.writestr(r"case\sources\interviews\old.md", "x")
            archive.writestr("case/state/interviews/old2.md", "x")
        self.assertIn("talks=0 cheap=0 retried=0 failed_twice=0", self.line())

    def test_cost_by_model_from_the_canary_sample(self):
        sample = (ROOT / "tests/fixtures/e02-routing-canary.jsonl").read_text(encoding="utf-8")
        (self.results / "s1.jsonl").write_text(sample, encoding="utf-8")
        result = next(e for e in map(json.loads, sample.splitlines()) if e["type"] == "result")
        expected = {m: round(u["costUSD"], 4) for m, u in result["modelUsage"].items()}
        self.assertEqual(set(expected), {"claude-sonnet-5-5", "claude-haiku-5-5"})
        line = self.line()
        self.assertIn("called_with_model=1", line)
        self.assertTrue(line.endswith(f"cost_by_model={expected}"), line)

    def test_cost_by_model_is_unknown_without_the_result_event(self):
        self.calls("haiku")
        self.assertTrue(self.line().endswith("cost_by_model=unknown"))
        events = [{"type": "result", "modelUsage": {"m": {"inputTokens": 1}}}]
        (self.results / "s2.jsonl").write_text("\n".join(map(json.dumps, events)), encoding="utf-8")
        self.assertTrue(self.line().endswith("cost_by_model=unknown"))

    def codex_results(self):
        event = {"type": "item.completed", "item": {"type": "collab_tool_call", "tool": "wait", "receiver_thread_ids": []}}
        (self.results / "s1.jsonl").write_text(json.dumps(event), encoding="utf-8")

    def codex_home(self):
        home = self.root / "codex-home"
        shutil.copytree(ROOT / "tests/fixtures/e02-routing-codex-sessions", home)
        return home, next(home.glob("sessions/**/rollout-*parent.jsonl"))

    def test_codex_called_with_model_from_the_session_files(self):
        self.codex_results()
        home, _ = self.codex_home()
        self.assertIn("called_with_model=1", self.line(home))
        self.assertTrue(self.line(home).endswith("cost_by_model=unknown"))
        self.assertNotIn("flag=", self.line(home))

    def test_codex_spawn_without_model_or_with_another_task_is_not_counted(self):
        self.codex_results()
        home, parent = self.codex_home()
        lines = parent.read_text(encoding="utf-8").splitlines()
        spawn = json.loads(lines[2])
        args = json.loads(spawn["payload"]["arguments"])
        spawn["payload"]["arguments"] = json.dumps({**args, "task_name": "reviewer_1"})
        lines[2] = json.dumps(spawn)
        lines.insert(1, "{broken")
        lines.append(json.dumps({"type": "response_item", "payload": {"type": "function_call", "name": "spawn_agent",
                                                                      "arguments": "not json"}}))
        parent.write_text("\n".join(lines), encoding="utf-8")
        self.assertIn("called_with_model=0", self.line(home))

    def test_codex_without_the_session_files_is_unknown(self):
        self.codex_results()
        self.assertIn("called_with_model=unknown", self.line())


class Timing(unittest.TestCase):
    def test_seconds_of_each_reply_and_their_sum(self):
        with tempfile.TemporaryDirectory() as tmp:
            results = Path(tmp)
            self.assertEqual(report.timing_lines(results), ["time none"])
            rows = ["c1-1\t2026-11-12T10:00:00Z\t2026-11-12T10:02:00Z\t120\texit 0",
                    "c2\t2026-11-12T10:05:00Z\t2026-11-12T10:06:30Z\t90\tbroken inside the stage",
                    "c3-1\tbad"]
            # Windows PowerShell 5.1 writes the first line with a byte order mark.
            (results / "timing.tsv").write_text("\n".join(rows), encoding="utf-8-sig")
            self.assertEqual(report.timing_lines(results), [
                "time c1-1 120s exit 0",
                "time c2 90s broken inside the stage",
                "time unreadable " + repr("c3-1\tbad"),
                "time_total 210s",
            ])


class SpentByReply(unittest.TestCase):
    """The cost and the tokens in a log are totals of the whole chat so far, not of the reply."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.results = Path(tmp.name)

    def log(self, name, *events):
        (self.results / f"{name}.jsonl").write_text("\n".join(map(json.dumps, events)), encoding="utf-8")

    def order(self, *names):
        # The run order is the one of timing.tsv, not of the names: c1-5a comes before c1-5.
        rows = [f"{name}\t2026-11-12T10:00:00Z\t2026-11-12T10:01:00Z\t60\texit 0" for name in names]
        (self.results / "timing.tsv").write_text("\n".join(rows), encoding="utf-8")

    def claude(self, name, chat, cost, model_cost=None, *calls):
        assistant = {"type": "assistant", "message": {"content": [
            {"type": "tool_use", "id": f"{name}-{i}", "name": tool, "input": given} for i, (tool, given) in enumerate(calls)]}}
        result = {"type": "result", "session_id": chat, "total_cost_usd": cost,
                  "modelUsage": {"m": {"costUSD": model_cost if model_cost is not None else cost}}}
        self.log(name, assistant, result)

    def codex(self, name, thread, tokens):
        self.log(name, {"type": "thread.started", "thread_id": thread},
                 {"type": "turn.completed", "usage": {"input_tokens": tokens, "cached_input_tokens": tokens // 2,
                                                      "output_tokens": tokens // 100}})

    def test_claude_code_two_chats_and_a_cut_off_reply(self):
        self.claude("c1-1", "A", 1.0)
        self.claude("c1-2", "A", 3.0)
        self.claude("c1-3", "A", 3.5)
        self.log("c2", {"type": "system", "subtype": "init", "session_id": "B"})
        self.claude("c3-1", "C", 2.0)
        self.claude("c3-2", "C", 2.25)
        self.order("c1-1", "c1-2", "c1-3", "c2", "c3-1", "c3-2")
        rows, sums, unknown = report.spent_by_reply(self.results)
        self.assertEqual(rows, [("c1-1", {"usd": 1.0, "model:m": 1.0}), ("c1-2", {"usd": 2.0, "model:m": 2.0}),
                                ("c1-3", {"usd": 0.5, "model:m": 0.5}), ("c2", None),
                                ("c3-1", {"usd": 2.0, "model:m": 2.0}), ("c3-2", {"usd": 0.25, "model:m": 0.25})])
        # The sum of the last totals of the chats: 3.5 + 2.25, not the sum of every total (12.75).
        self.assertEqual((sums["usd"], unknown), (5.75, ["c2"]))
        lines = report.usage_lines(self.results)
        self.assertIn("spent c2 unknown", lines)
        self.assertIn("spent_total usd=5.75 complete=False unknown=['c2']", lines)

    def test_a_reply_is_taken_in_the_order_of_the_run(self):
        self.claude("c1-5", "A", 5.0)
        self.claude("c1-5a", "A", 4.0)
        self.order("c1-5a", "c1-5")
        rows, sums, _ = report.spent_by_reply(self.results)
        self.assertEqual([(name, spent["usd"]) for name, spent in rows], [("c1-5a", 4.0), ("c1-5", 1.0)])
        self.assertEqual(sums["usd"], 5.0)

    def test_codex_two_threads_and_a_cut_off_reply(self):
        self.codex("c1-1", "T1", 400)
        self.codex("c1-2", "T1", 1000)
        self.log("c2", {"type": "thread.started", "thread_id": "T2"}, {"type": "turn.started"})
        self.codex("c3-1", "T3", 300)
        self.codex("c3-2", "T3", 900)
        self.order("c1-1", "c1-2", "c2", "c3-1", "c3-2")
        rows, sums, unknown = report.spent_by_reply(self.results)
        self.assertEqual([spent and spent["input_tokens"] for _, spent in rows], [400, 600, None, 300, 600])
        self.assertEqual((sums["input_tokens"], unknown), (1900, ["c2"]))
        self.assertIn("spent c1-2 input=600 cached_input=300 output=6", report.usage_lines(self.results))

    def test_a_complete_run_is_marked_complete(self):
        self.claude("c1-1", "A", 1.0)
        self.assertIn("spent_total usd=1.0 complete=True unknown=[]", report.usage_lines(self.results))

    def test_no_totals_at_all_is_none(self):
        self.log("c1-1", {"type": "system"})
        self.assertIn("spent_total none complete=False unknown=['c1-1']", report.usage_lines(self.results))

    def test_cost_by_model_of_the_routing_line_is_the_sum_of_the_last_values(self):
        self.claude("c1-1", "A", 1.0, 0.75)
        self.claude("c1-2", "A", 3.0, 2.5)
        self.order("c1-1", "c1-2")
        self.assertEqual(report.spent_by_reply(self.results)[1]["model:m"], 2.5)

    def test_work_counters_per_reply(self):
        long_text = "x" * 40
        self.log("c1-1",
                 {"type": "assistant", "message": {"content": [
                     {"type": "tool_use", "id": "t1", "name": "Read", "input": {"file_path": r"C:\run\clone\agent\contracts\README.md"}},
                     {"type": "tool_use", "id": "t2", "name": "Bash", "input": {
                         "command": "$PY tools/checkpoint.py action start projects/x && cat agent/workflows/a.md"}},
                     {"type": "tool_use", "id": "t3", "name": "Read", "input": {"file_path": r"C:\run\clone\projects\x\brief.md"}}]}},
                 {"type": "assistant", "message": {"content": [{"type": "tool_use", "id": "t1", "name": "Read", "input": {}}]}},
                 {"type": "user", "message": {"content": [
                     {"type": "tool_result", "tool_use_id": "t1", "content": long_text},
                     {"type": "tool_result", "tool_use_id": "t2", "content": [{"type": "text", "text": "ab"}]},
                     {"type": "tool_result", "tool_use_id": "t3", "content": long_text}]}})
        self.log("c1-2",
                 {"type": "item.completed", "item": {"type": "command_execution", "aggregated_output": "ab" * 5,
                                                     "command": "pwsh -Command 'Get-Content agent/workflows/b.md'"}},
                 {"type": "item.completed", "item": {"type": "command_execution", "aggregated_output": "zz",
                                                     "command": "python tools/case.py usage projects/x; python tools/calc.py add"}},
                 {"type": "item.completed", "item": {"type": "file_change", "changes": []}},
                 {"type": "item.completed", "item": {"type": "agent_message", "text": "done"}})
        self.order("c1-1", "c1-2")
        lines = report.usage_lines(self.results)
        # The Read of agent/ and the cat of agent/ are two reads; the output of the second is 2 bytes.
        self.assertIn("work c1-1 actions=3 agent_reads=2 agent_bytes=42 utilities={'checkpoint.py action': 1}", lines)
        self.assertIn("work c1-2 actions=3 agent_reads=1 agent_bytes=10"
                      " utilities={'calc.py add': 1, 'case.py usage': 1}", lines)
        self.assertIn("work_total actions=6 agent_reads=3 agent_bytes=52"
                      " utilities={'calc.py add': 1, 'case.py usage': 1, 'checkpoint.py action': 1}", lines)
