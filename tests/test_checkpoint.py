import hashlib
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))

import validate_project as vp  # noqa: E402

DEMO = ROOT / "agent" / "contracts" / "examples" / "valid" / "demo-case"
CKP_DIR = "state/checkpoints"
JOURNAL = "state/actions.jsonl"


def read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def write_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def tree_hash(root):
    digest = hashlib.sha256()
    for path in sorted(Path(root).rglob("*")):
        digest.update(path.relative_to(root).as_posix().encode("utf-8"))
        if path.is_file():
            digest.update(path.read_bytes())
    return digest.hexdigest()


class CaseTestCase(unittest.TestCase):
    """Each test works on its own copy of the demo case."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.case = self.tmp / "demo-case"
        shutil.copytree(DEMO, self.case)

    def codes(self, case=None):
        report = vp.validate_case(case or self.case)
        return sorted(f.code for f in report.errors), sorted(f.code for f in report.warnings)


class ValidatorCheckpointStatus(CaseTestCase):
    def test_demo_case_lists_two_fit_checkpoints(self):
        report = vp.validate_case(self.case)
        self.assertEqual([(c.name, c.number, c.seq, c.reason, c.missing) for c in report.checkpoints],
                         [("CKP-0001", 1, 1, None, []), ("CKP-0002", 2, 2, None, [])])

    def test_all_missing_artifacts_are_listed(self):
        (self.case / "outputs" / "product-strategy.md").unlink()
        (self.case / "outputs" / "validation-plan.md").unlink()
        report = vp.validate_case(self.case)
        newest = report.checkpoints[-1]
        self.assertEqual(newest.reason, "an artifact is missing or unreadable")
        self.assertEqual(newest.missing, ["outputs/product-strategy.md", "outputs/validation-plan.md"])

    def test_stray_file_has_no_name(self):
        (self.case / CKP_DIR / "latest.json").write_text("{}", encoding="utf-8")
        stray = [c for c in vp.validate_case(self.case).checkpoints if c.name is None]
        self.assertEqual(len(stray), 1)
        self.assertEqual(stray[0].reason, "unexpected file name")
        self.assertIsNone(stray[0].number)

    def test_checkpoints_are_filled_when_none_is_fit(self):
        for name in ("CKP-0001.json", "CKP-0002.json"):
            (self.case / CKP_DIR / name).write_text("not json", encoding="utf-8")
        report = vp.validate_case(self.case)
        self.assertEqual([c.reason for c in report.checkpoints], ["cannot be parsed"] * 2)
        self.assertEqual([c.seq for c in report.checkpoints], [None, None])

    def test_json_output_keys_are_unchanged(self):
        result = subprocess.run([sys.executable, str(ROOT / "tools" / "validate_project.py"), str(self.case), "--json"],
                                capture_output=True, check=False)
        self.assertEqual(set(json.loads(result.stdout)), {"errors", "warnings", "current_checkpoint"})


import checkpoint as ck  # noqa: E402


def run(*argv):
    """Run the CLI in-process; return the exit code and the first output line as JSON."""
    out = io.StringIO()
    with redirect_stdout(out):
        code = ck.main([str(a) for a in argv])
    return code, json.loads(out.getvalue().splitlines()[0])


def journal_lines(case):
    path = Path(case) / JOURNAL
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


class EmptyCaseTestCase(unittest.TestCase):
    """A case folder with case.json only."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.case = self.tmp / "empty-case"
        write_json(self.case / "case.json", {"format_version": 1, "case_id": "empty-case",
                                             "title": "Пустой кейс", "created_at": "2026-11-06T10:00:00+03:00"})
        write_json(self.case / "evidence" / "sources.json", {"format_version": 1, "records": [
            {"id": f"SRC-{n:04d}", "created_at": "2026-11-06T10:00:00+03:00", "record_status": "active",
             "type": "web", "title": f"Страница {n}", "location": {"url": f"https://example.org/{n}"},
             "origin": "public_web", "allowed": True, "retrieved_at": "2026-11-06T10:00:00+03:00", "sha256": None,
             "extraction": {"status": "unavailable", "complete": False, "limits": "Не открывалась."}}
            for n in range(1, 11)]})

    def start(self, *extra, kind="model", tool="analysis"):
        return run("action", "start", self.case, "--kind", kind, "--tool", tool,
                   "--environment", "claude_code", *extra)

    def end(self, action_id, outcome="completed", *extra):
        return run("action", "end", self.case, "--id", action_id, "--outcome", outcome, *extra)


class ActionJournal(EmptyCaseTestCase):
    def test_start_and_end_write_valid_lines(self):
        code, result = self.start("--input-ref", "SRC-0001")
        self.assertEqual((code, result["id"]), (0, "ACT-0001"))
        code, _ = self.end("ACT-0001", "completed", "--result-ref", "SRC-0002")
        self.assertEqual(code, 0)
        start, end = journal_lines(self.case)
        self.assertEqual(start["format_version"], 1)
        self.assertEqual((start["phase"], start["model"], start["input_refs"]), ("start", None, ["SRC-0001"]))
        self.assertEqual((end["outcome"], end["result_refs"], end["cost"]), ("completed", ["SRC-0002"], {"basis": "unknown"}))

    def test_second_end_is_refused(self):
        self.start()
        self.end("ACT-0001")
        before = (self.case / JOURNAL).read_bytes()
        code, result = self.end("ACT-0001", "failed")
        self.assertEqual((code, result["refused"]), (1, "already_ended"))
        self.assertEqual((self.case / JOURNAL).read_bytes(), before)

    def test_end_without_start_is_refused(self):
        code, result = self.end("ACT-0005")
        self.assertEqual((code, result["refused"]), (1, "end_without_start"))
        self.assertFalse((self.case / JOURNAL).exists())

    def test_human_writes_both_lines_and_validates(self):
        case = self.tmp / "demo-case"
        shutil.copytree(DEMO, case)
        code, result = run("action", "human", case, "--tool", "chat", "--environment", "codex", "--result-ref", "DEC-0002")
        self.assertEqual((code, result["id"]), (0, "ACT-0010"))
        start, end = journal_lines(case)[-2:]
        self.assertEqual((start["kind"], start["input_refs"], start["id"]), ("human_input", [], "ACT-0010"))
        self.assertEqual((end["outcome"], end["result_refs"], end["id"]), ("completed", ["DEC-0002"], "ACT-0010"))
        self.assertEqual(vp.validate_case(case).errors, [])

    def test_human_needs_a_decision_reference(self):
        for refs in ([], ["CLM-0001"]):
            with self.subTest(refs=refs):
                extra = [item for ref in refs for item in ("--result-ref", ref)]
                code, result = run("action", "human", self.case, "--tool", "chat", "--environment", "codex", *extra)
                self.assertEqual((code, result["refused"]), (1, "human_result_invalid"))
        self.assertFalse((self.case / JOURNAL).exists())

    def test_human_input_with_inputs_fails_the_schema(self):
        code, result = self.start("--input-ref", "SRC-0001", kind="human_input")
        self.assertEqual((code, result["refused"]), (1, "schema"))
        self.assertFalse((self.case / JOURNAL).exists())

    def test_measured_cost_file_is_recorded(self):
        cost = {"basis": "measured", "amounts": {"input_tokens": 10, "output_tokens": 5, "money_usd": None}}
        write_json(self.tmp / "cost.json", cost)
        self.start()
        code, _ = self.end("ACT-0001", "completed", "--cost-file", self.tmp / "cost.json")
        self.assertEqual(code, 0)
        self.assertEqual(journal_lines(self.case)[-1]["cost"], cost)
        self.assertEqual(vp.validate_case(self.case).errors, [])

    def test_invalid_cost_is_refused(self):
        self.start()
        before = (self.case / JOURNAL).read_bytes()
        cases = {
            "amounts with unknown": {"basis": "unknown", "amounts": {"input_tokens": 1, "output_tokens": 1, "money_usd": None}},
            "no amounts when measured": {"basis": "measured"},
            "estimate without source": {"basis": "estimated", "amounts": {"input_tokens": 1, "output_tokens": 1, "money_usd": None}},
        }
        for name, cost in cases.items():
            with self.subTest(name):
                write_json(self.tmp / "cost.json", cost)
                code, result = self.end("ACT-0001", "completed", "--cost-file", self.tmp / "cost.json")
                self.assertEqual((code, result["status"]), (1, "refused"))
        (self.tmp / "cost.json").write_text("not json", encoding="utf-8")
        self.assertEqual(self.end("ACT-0001", "completed", "--cost-file", self.tmp / "cost.json")[0], 1)
        self.assertEqual((self.case / JOURNAL).read_bytes(), before)

    def test_repeat_without_retry_is_refused_for_every_outcome(self):
        for outcome in ("completed", "failed", "unknown", None):
            with self.subTest(outcome=outcome):
                case = self.tmp / f"case-{outcome}"
                shutil.copytree(self.case, case, ignore=None)
                write_json(case / "case.json", {"format_version": 1, "case_id": case.name, "title": "t",
                                                "created_at": "2026-11-06T10:00:00+03:00"})
                args = ["--kind", "web_read", "--tool", "WebFetch", "--environment", "claude_code",
                        "--input-ref", "SRC-0001", "--input-ref", "SRC-0002"]
                self.assertEqual(run("action", "start", case, *args)[0], 0)
                if outcome:
                    run("action", "end", case, "--id", "ACT-0001", "--outcome", outcome)
                swapped = ["--kind", "web_read", "--tool", "WebFetch", "--environment", "claude_code",
                           "--input-ref", "SRC-0002", "--input-ref", "SRC-0001"]
                code, result = run("action", "start", case, *swapped)
                self.assertEqual((code, result["refused"], result["repeat_of"]), (1, "repeat", "ACT-0001"))
                self.assertEqual(len(journal_lines(case)), 1 + bool(outcome))

    def test_subset_and_empty_inputs_are_not_repeats(self):
        self.start("--input-ref", "SRC-0001", "--input-ref", "SRC-0002", kind="file_read", tool="Read")
        self.assertEqual(self.start("--input-ref", "SRC-0001", kind="file_read", tool="Read")[0], 0)
        self.assertEqual(self.start("--input-ref", "SRC-0001", kind="search", tool="Read")[0], 0)
        self.assertEqual(self.start("--input-ref", "SRC-0001", kind="file_read", tool="Other")[0], 0)
        self.assertEqual(self.start()[0], 0)
        self.assertEqual(self.start()[0], 0)

    def test_retry_with_reason_is_accepted_and_visible(self):
        self.start("--input-ref", "SRC-0001")
        self.end("ACT-0001", "failed")
        code, result = self.start("--input-ref", "SRC-0001", "--retry-of", "ACT-0001", "--retry-reason", "Сеть была недоступна")
        self.assertEqual((code, result["id"]), (0, "ACT-0002"))
        line = journal_lines(self.case)[-1]
        self.assertEqual((line["retry_of"], line["retry_reason"]), ("ACT-0001", "Сеть была недоступна"))
        self.assertEqual(vp.validate_case(self.case).errors, [])

    def test_bad_retry_is_refused(self):
        self.start("--input-ref", "SRC-0001")
        for extra, reason in (
            (["--retry-of", "ACT-0001"], "retry_invalid"),
            (["--retry-of", "ACT-0001", "--retry-reason", "  "], "retry_invalid"),
            (["--retry-reason", "Причина"], "retry_invalid"),
            (["--retry-of", "ACT-0042", "--retry-reason", "Причина"], "retry_target_unknown"),
        ):
            with self.subTest(extra=extra):
                code, result = self.start("--input-ref", "SRC-0001", *extra)
                self.assertEqual((code, result["refused"]), (1, reason))
        self.assertEqual(len(journal_lines(self.case)), 1)

    def test_torn_tail_is_cut_and_its_id_is_not_reused(self):
        self.start("--input-ref", "SRC-0001")
        path = self.case / JOURNAL
        with open(path, "ab") as handle:
            handle.write(b'{"format_version": 1, "id": "ACT-0002", "phase": "sta')
        self.assertIn("JOURNAL_TORN", self.codes_of())
        code, result = self.start("--input-ref", "SRC-0009")
        self.assertEqual((code, result["id"]), (0, "ACT-0003"))
        self.assertTrue(path.read_bytes().endswith(b"\n"))
        self.assertEqual([line["id"] for line in journal_lines(self.case)], ["ACT-0001", "ACT-0003"])
        self.assertNotIn("JOURNAL_TORN", self.codes_of())

    def test_complete_line_without_newline_is_kept(self):
        self.start("--input-ref", "SRC-0001")
        path = self.case / JOURNAL
        path.write_bytes(path.read_bytes().rstrip(b"\n"))
        code, _ = self.end("ACT-0001")
        self.assertEqual(code, 0)
        self.assertEqual(len(journal_lines(self.case)), 2)

    def test_corruption_in_the_middle_is_refused_and_changes_nothing(self):
        self.start("--input-ref", "SRC-0001")
        self.end("ACT-0001")
        path = self.case / JOURNAL
        first, second = path.read_bytes().splitlines(keepends=True)
        path.write_bytes(first + b"{broken\n" + second)
        before = path.read_bytes()
        for call in (lambda: self.start("--input-ref", "SRC-0005"), lambda: self.end("ACT-0001")):
            code, result = call()
            self.assertEqual((code, result["refused"]), (1, "journal_corrupt"))
        self.assertEqual(path.read_bytes(), before)

    def test_terminated_unparsable_last_line_is_cut_like_a_torn_tail(self):
        self.start("--input-ref", "SRC-0001")
        path = self.case / JOURNAL
        with open(path, "ab") as handle:
            handle.write(b"{broken\n")
        self.assertIn("JOURNAL_TORN", self.codes_of())
        code, result = self.start("--input-ref", "SRC-0005")
        self.assertEqual((code, result["id"]), (0, "ACT-0002"))
        self.assertEqual([line["id"] for line in journal_lines(self.case)], ["ACT-0001", "ACT-0002"])
        self.assertNotIn("JOURNAL_TORN", self.codes_of())

    def test_references_must_exist(self):
        before_lines = 0
        for extra in (["--input-ref", "SRC-0099"], ["--input-ref", "CLM-0001"], ["--input-ref", "CKP-0001"]):
            with self.subTest(extra=extra):
                code, result = self.start(*extra)
                self.assertEqual((code, result["refused"]), (1, "ref_unknown"))
        self.assertFalse((self.case / JOURNAL).exists())
        self.start("--input-ref", "SRC-0001")
        code, result = self.end("ACT-0001", "completed", "--result-ref", "CLM-0001")
        self.assertEqual((code, result["refused"]), (1, "ref_unknown"))
        self.assertEqual(self.end("ACT-0001", "completed", "--result-ref", "ACT-0001")[0], 0)
        self.assertEqual(len(journal_lines(self.case)), 2 + before_lines)

    def test_checkpoint_files_and_hidden_ones_count_as_existing(self):
        (self.case / CKP_DIR).mkdir(parents=True)
        (self.case / CKP_DIR / ".lost-CKP-0004.json").write_text("x", encoding="utf-8")
        self.assertEqual(self.start("--input-ref", "CKP-0004")[0], 0)

    def test_unreadable_registry_does_not_block_its_prefix(self):
        (self.case / "evidence" / "claims.json").write_text("{nope", encoding="utf-8")
        self.assertEqual(self.start("--input-ref", "CLM-0001")[0], 0)
        self.assertEqual(self.start("--input-ref", "SRC-0099", tool="other")[1]["refused"], "ref_unknown")

    def test_decision_in_human_may_be_written_before_its_record(self):
        code, result = run("action", "human", self.case, "--tool", "chat", "--environment", "codex", "--result-ref", "DEC-0001")
        self.assertEqual(code, 0)

    def test_retry_of_a_repeated_action_must_name_the_same_key(self):
        self.start("--input-ref", "SRC-0001", kind="search", tool="WebSearch")
        self.start("--input-ref", "SRC-0002", kind="search", tool="WebSearch")
        self.end("ACT-0001", "failed")
        self.end("ACT-0002", "failed")
        code, result = self.start("--input-ref", "SRC-0001", "--retry-of", "ACT-0002", "--retry-reason", "причина",
                                  kind="search", tool="WebSearch")
        self.assertEqual((code, result["refused"], result["repeat_of"]), (1, "retry_target_mismatch", "ACT-0001"))
        code, _ = self.start("--input-ref", "SRC-0001", "--retry-of", "ACT-0001", "--retry-reason", "причина",
                             kind="search", tool="WebSearch")
        self.assertEqual(code, 0)

    def test_crash_while_writing_leaves_the_journal_unchanged(self):
        self.start("--input-ref", "SRC-0001")
        before = (self.case / JOURNAL).read_bytes()
        with mock.patch.object(ck, "append_lines", side_effect=OSError("disk")):
            code, result = self.end("ACT-0001")
        self.assertEqual((code, result), (3, {"status": "internal_error"}))
        self.assertEqual((self.case / JOURNAL).read_bytes(), before)

    def codes_of(self):
        report = vp.validate_case(self.case)
        return [f.code for f in report.errors + report.warnings]


REQUEST = {"stage": "research", "open_items": ["Проверить окупаемость"], "next_step": "Согласовать план"}


class DemoCaseTestCase(CaseTestCase):
    def request(self, **fields):
        """Write a save request file and return its path."""
        path = self.tmp / "request.json"
        write_json(path, {**REQUEST, **fields})
        return path

    def save(self, *extra, **fields):
        return run("save", self.case, "--from", self.request(**fields), *extra)

    def checkpoint(self, name):
        return read_json(self.case / CKP_DIR / f"{name}.json")

    def hidden(self):
        return sorted(p.name for p in (self.case / CKP_DIR).iterdir() if p.name.startswith("."))

    def break_checkpoints(self, *names):
        for name in names:
            (self.case / CKP_DIR / f"{name}.json").write_text("not json", encoding="utf-8")


class SaveCheckpoint(DemoCaseTestCase):
    def test_saves_chain_with_facts_from_journal_and_registry(self):
        code, result = self.save()
        self.assertEqual((code, result["id"], result["seq"], result["previous"]), (0, "CKP-0003", 3, "CKP-0002"))
        document = self.checkpoint("CKP-0003")
        self.assertEqual(document["completed_actions"], ["ACT-0001", "ACT-0002", "ACT-0003", "ACT-0004",
                                                         "ACT-0006", "ACT-0007", "ACT-0008", "ACT-0009"])
        decisions = read_json(self.case / "decisions" / "decisions.json")["records"]
        self.assertEqual(document["human_choices"], [d["id"] for d in decisions if d["status"] == "chosen"])
        self.assertEqual(document["goal"], self.checkpoint("CKP-0002")["goal"])
        report = vp.validate_case(self.case)
        self.assertEqual(report.errors, [])
        self.assertEqual(report.current_checkpoint, "CKP-0003")
        code, result = self.save(goal="Новая цель")
        self.assertEqual((code, result["id"], result["previous"]), (0, "CKP-0004", "CKP-0003"))
        self.assertEqual(self.checkpoint("CKP-0004")["goal"], "Новая цель")
        self.assertEqual(vp.validate_case(self.case).current_checkpoint, "CKP-0004")

    def test_snapshots_are_byte_identical_and_cover_only_case_texts(self):
        self.save()
        paths = [a["path"] for a in self.checkpoint("CKP-0003")["artifacts"]]
        self.assertEqual(paths, sorted(paths))
        self.assertIn("brief.md", paths)
        self.assertIn("evidence/sources.json", paths)
        self.assertIn("outputs/product-strategy.md", paths)
        self.assertFalse([p for p in paths if p.startswith(("sources/", "state/"))])
        for artifact in self.checkpoint("CKP-0003")["artifacts"]:
            copy = self.case / "state" / "snapshots" / artifact["sha256"]
            self.assertEqual(copy.read_bytes(), (self.case / artifact["path"]).read_bytes())

    def test_save_does_not_touch_the_journal(self):
        before = (self.case / JOURNAL).read_bytes()
        self.save()
        self.assertEqual((self.case / JOURNAL).read_bytes(), before)

    def test_first_save_in_a_case_without_texts(self):
        case = self.tmp / "empty-case"
        write_json(case / "case.json", {"format_version": 1, "case_id": "empty-case", "title": "t",
                                        "created_at": "2026-11-06T10:00:00+03:00"})
        request = self.tmp / "first.json"
        write_json(request, {**REQUEST, "goal": "Разобраться"})
        code, result = run("save", case, "--from", request)
        self.assertEqual((code, result["id"], result["seq"], result["previous"]), (0, "CKP-0001", 1, None))
        document = read_json(case / CKP_DIR / "CKP-0001.json")
        self.assertEqual((document["artifacts"], document["completed_actions"], document["human_choices"]), ([], [], []))
        self.assertEqual(document["totals"]["elapsed_seconds"], 0)
        self.assertEqual(vp.validate_case(case).errors, [])
        write_json(request, REQUEST)
        shutil.rmtree(case / "state")
        code, result = run("save", case, "--from", request)
        self.assertEqual((code, result["refused"]), (1, "bad_request"))

    def test_elapsed_time_runs_from_the_first_start_to_the_last_line(self):
        lines = [
            {"phase": "start", "at": "2026-11-06T10:00:00+03:00"},
            {"phase": "end", "at": "2026-11-06T10:05:00+03:00"},
            {"phase": "start", "at": "2026-11-06T11:00:00+03:00"},
        ]
        totals = ck.compute_totals(lines)
        self.assertEqual(totals["elapsed_seconds"], 3600)
        self.assertEqual((totals["time_to_first_result_seconds"], totals["human_active_seconds"], totals["cost"]),
                         (None, None, {"basis": "unknown"}))

    def test_bad_requests_are_refused_without_a_write(self):
        before = tree_hash(self.case)
        bad = {
            "completed_actions": {"completed_actions": ["ACT-0001"]},
            "human_choices": {"human_choices": ["DEC-0001"]},
            "unknown key": {"note": "x"},
            "wrong stage": {"stage": "finished"},
            "empty goal": {"goal": "  "},
            "open_items not a list": {"open_items": "text"},
            "blank open item": {"open_items": [""]},
            "null step before done": {"next_step": None},
        }
        for name, fields in bad.items():
            with self.subTest(name):
                code, result = self.save(**fields)
                self.assertEqual((code, result["refused"]), (1, "bad_request"))
        missing = self.tmp / "missing.json"
        broken = self.tmp / "broken.json"
        broken.write_text("{nope", encoding="utf-8")
        for path in (missing, broken):
            code, result = run("save", self.case, "--from", path)
            self.assertEqual((code, result["refused"]), (1, "request_unreadable"))
        partial = self.tmp / "partial.json"
        write_json(partial, {"stage": "research"})
        self.assertEqual(run("save", self.case, "--from", partial)[1]["refused"], "bad_request")
        self.assertEqual(tree_hash(self.case), before)

    def test_refusals_leave_the_case_as_it_was(self):
        (self.case / "decisions" / "decisions.json").write_text("{nope", encoding="utf-8")
        before = tree_hash(self.case)
        code, result = self.save()
        self.assertEqual((code, result["refused"]), (1, "validation_errors"))
        self.assertIn("FILE_CORRUPT", [f["code"] for f in result["findings"]])
        self.assertEqual(tree_hash(self.case), before)
        self.assertFalse((self.case / "state" / "snapshots").exists())

    def test_done_is_refused_while_an_action_has_no_outcome(self):
        self.assertEqual(run("action", "start", self.case, "--kind", "search", "--tool", "WebSearch",
                             "--environment", "claude_code", "--input-ref", "SRC-0001")[0], 0)
        before = tree_hash(self.case)
        code, result = self.save(stage="done", next_step=None)
        self.assertEqual((code, result["refused"], result["actions"]), (1, "unfinished_actions", ["ACT-0010"]))
        self.assertEqual(tree_hash(self.case), before)
        run("action", "end", self.case, "--id", "ACT-0010", "--outcome", "unknown")
        self.assertEqual(self.save(stage="done", next_step=None)[1]["refused"], "unfinished_actions")
        self.assertEqual(self.save(stage="strategies")[0], 0)

    def test_done_is_saved_when_every_action_has_an_outcome(self):
        code, result = self.save(stage="done", next_step=None)
        self.assertEqual(code, 0)
        self.assertEqual(self.checkpoint(result["id"])["stage"], "done")

    def test_stale_temporaries_are_removed(self):
        for folder in ("checkpoints", "snapshots"):
            stale = self.case / "state" / folder / ".tmp-1-leftover"
            stale.parent.mkdir(parents=True, exist_ok=True)
            stale.write_text("half", encoding="utf-8")
        self.assertEqual(self.save()[0], 0)
        self.assertEqual(list(self.case.rglob(".tmp-*")), [])

    def test_unsupported_case_version_is_refused(self):
        case = read_json(self.case / "case.json")
        write_json(self.case / "case.json", {**case, "format_version": 2})
        before = tree_hash(self.case)
        code, result = self.save()
        self.assertEqual((code, result["refused"], result["case_version"], result["supported"]), (1, "unsupported_version", 2, [1]))
        self.assertEqual(tree_hash(self.case), before)

    def test_number_taken_is_refused(self):
        before = (self.case / CKP_DIR / "CKP-0002.json").read_bytes()
        with mock.patch.object(ck, "next_checkpoint_number", return_value=2):
            code, result = self.save()
        self.assertEqual((code, result["refused"]), (1, "number_taken"))
        self.assertEqual((self.case / CKP_DIR / "CKP-0002.json").read_bytes(), before)

    def test_artifact_changed_while_saving_is_refused(self):
        real = ck.collect_artifacts

        def collect_then_edit(root):
            found = real(root)
            (root / "brief.md").write_text("Другой текст", encoding="utf-8")
            return found

        with mock.patch.object(ck, "collect_artifacts", side_effect=collect_then_edit):
            code, result = self.save()
        self.assertEqual((code, result["refused"], result["path"]), (1, "artifact_changed", "brief.md"))
        self.assertEqual(vp.validate_case(self.case).current_checkpoint, "CKP-0002")


class SaveAfterLoss(DemoCaseTestCase):
    def test_damaged_newest_checkpoint_needs_acknowledgement(self):
        self.break_checkpoints("CKP-0002")
        before = tree_hash(self.case)
        code, result = self.save()
        self.assertEqual((code, result["refused"], result["lost"]), (1, "loss_not_acknowledged", ["CKP-0002"]))
        self.assertEqual(tree_hash(self.case), before)

        code, result = self.save("--acknowledge-loss")
        self.assertEqual((code, result["id"], result["seq"], result["previous"]), (0, "CKP-0003", 2, "CKP-0001"))
        self.assertEqual(result["lost_hidden"], ["CKP-0002.json"])
        self.assertEqual((self.case / CKP_DIR / ".lost-CKP-0002.json").read_text(encoding="utf-8"), "not json")
        self.assertIn("CKP-0002", self.checkpoint("CKP-0003")["open_items"][-1])
        errors, warnings = self.codes()
        self.assertEqual((errors, warnings), ([], []))

        code, result = self.save()
        self.assertEqual((code, result["id"]), (0, "CKP-0004"))

    def test_two_lost_files_stop_warning_once_hidden(self):
        (self.case / CKP_DIR / "CKP-0003.json").write_text("not json", encoding="utf-8")
        self.break_checkpoints("CKP-0002")
        code, result = self.save("--acknowledge-loss")
        self.assertEqual((code, result["id"]), (0, "CKP-0004"))
        self.assertEqual(self.hidden(), [".lost-CKP-0002.json", ".lost-CKP-0003.json"])
        self.assertEqual(self.codes(), ([], []))
        self.assertEqual(self.save()[1]["id"], "CKP-0005")

    def test_stray_file_counts_as_a_loss(self):
        (self.case / CKP_DIR / "latest.json").write_text("{}", encoding="utf-8")
        self.assertEqual(self.save()[1]["refused"], "loss_not_acknowledged")
        code, result = self.save("--acknowledge-loss")
        self.assertEqual((code, result["lost_hidden"]), (0, ["latest.json"]))
        self.assertTrue((self.case / CKP_DIR / ".lost-latest.json").is_file())
        self.assertEqual(self.codes(), ([], []))

    def test_all_checkpoints_damaged_start_a_new_chain(self):
        self.break_checkpoints("CKP-0001", "CKP-0002")
        code, result = self.save(goal="Начать заново")
        self.assertEqual((code, result["refused"]), (1, "loss_not_acknowledged"))
        code, result = self.save("--acknowledge-loss", goal="Начать заново")
        self.assertEqual((code, result["id"], result["seq"], result["previous"]), (0, "CKP-0003", 1, None))
        self.assertEqual(self.hidden(), [".lost-CKP-0001.json", ".lost-CKP-0002.json"])
        note = self.checkpoint("CKP-0003")["open_items"][-1]
        self.assertTrue("CKP-0001" in note and "CKP-0002" in note)
        self.assertEqual(self.codes(), ([], []))

    def test_all_damaged_without_a_goal_is_refused_before_any_write(self):
        self.break_checkpoints("CKP-0001", "CKP-0002")
        before = tree_hash(self.case)
        code, result = self.save("--acknowledge-loss")
        self.assertEqual((code, result["refused"]), (1, "bad_request"))
        self.assertEqual(tree_hash(self.case), before)

    def test_damaged_checkpoint_is_never_overwritten_or_reused(self):
        self.break_checkpoints("CKP-0002")
        self.save("--acknowledge-loss")
        numbers = sorted(p.name for p in (self.case / CKP_DIR).iterdir())
        self.assertEqual(numbers, [".lost-CKP-0002.json", "CKP-0001.json", "CKP-0003.json"])


class SaveInterrupted(DemoCaseTestCase):
    def assert_prior_checkpoint_is_current(self):
        report = vp.validate_case(self.case)
        self.assertEqual(report.errors, [])
        self.assertEqual(report.current_checkpoint, "CKP-0002")

    def test_crash_while_copying_snapshots(self):
        with mock.patch.object(ck, "write_snapshots", side_effect=OSError("disk")):
            code, result = self.save()
        self.assertEqual((code, result), (3, {"status": "internal_error"}))
        self.assert_prior_checkpoint_is_current()
        self.assertEqual(self.save()[0], 0)

    def test_crash_halfway_through_snapshots(self):
        real = ck.write_durable
        calls = []

        def fail_on_second(path, data):
            calls.append(path)
            if len(calls) == 2:
                raise OSError("disk")
            real(path, data)

        with mock.patch.object(ck, "write_durable", side_effect=fail_on_second):
            self.assertEqual(self.save()[0], 3)
        self.assert_prior_checkpoint_is_current()
        self.assertEqual(self.save()[0], 0)

    def test_crash_while_publishing(self):
        with mock.patch.object(ck, "publish_checkpoint", side_effect=OSError("disk")):
            self.assertEqual(self.save()[0], 3)
        self.assert_prior_checkpoint_is_current()
        code, result = self.save()
        self.assertEqual((code, result["id"]), (0, "CKP-0003"))

    def test_crash_while_hiding_lost_files_keeps_the_loss_visible(self):
        (self.case / CKP_DIR / "CKP-0003.json").write_text("not json", encoding="utf-8")
        self.break_checkpoints("CKP-0002")
        with mock.patch.object(ck, "hide_lost", side_effect=OSError("disk")):
            self.assertEqual(self.save("--acknowledge-loss")[0], 3)
        report = vp.validate_case(self.case)
        self.assertEqual(report.errors, [])
        self.assertEqual(report.current_checkpoint, "CKP-0004")
        self.assertIn("CHECKPOINT_FALLBACK", [f.code for f in report.warnings])
        self.assertEqual(self.save()[1]["refused"], "loss_not_acknowledged")
        code, result = self.save("--acknowledge-loss")
        self.assertEqual((code, result["id"]), (0, "CKP-0005"))
        self.assertEqual(self.codes(), ([], []))

    def test_rejected_publication_is_set_aside(self):
        real = ck.validate_case
        calls = []

        def second_call_fails(root):
            report = real(root)
            calls.append(1)
            if len(calls) == 2:
                report.errors.append(vp.Finding("SCHEMA", "x", None, "y"))
            return report

        with mock.patch.object(ck, "validate_case", side_effect=second_call_fails):
            code, result = self.save()
        self.assertEqual((code, result["refused"], result["id"]), (1, "published_rejected", "CKP-0003"))
        self.assertEqual(self.hidden(), [".rejected-CKP-0003.json"])
        self.assertFalse((self.case / CKP_DIR / "CKP-0003.json").exists())
        self.assert_prior_checkpoint_is_current()
        self.assertEqual(self.save()[1]["id"], "CKP-0004")


class Resume(DemoCaseTestCase):
    def resume(self):
        return run("resume", self.case)

    def test_fresh_case(self):
        case = self.tmp / "empty-case"
        write_json(case / "case.json", {"format_version": 1, "case_id": "empty-case", "title": "t",
                                        "created_at": "2026-11-06T10:00:00+03:00"})
        code, result = run("resume", case)
        self.assertEqual((code, result["status"], result["id"], result["goal"]), (0, "fresh", None, None))
        self.assertEqual((result["lost"], result["recovery"], result["human_choices"]), ([], [], []))

    def test_every_field_on_the_demo_case(self):
        code, result = self.resume()
        document = self.checkpoint("CKP-0002")
        self.assertEqual((code, result["status"]), (0, "ok"))
        for key in ("id", "created_at", "goal", "stage", "open_items", "next_step", "totals"):
            self.assertEqual(result[key], document[key], key)
        self.assertEqual(result["human_choices"][0], {"id": "DEC-0001", "topic": "direction", "user_statement": "Развиваем сервис"})
        self.assertEqual([c["id"] for c in result["human_choices"]], document["human_choices"])
        self.assertEqual(result["completed_actions"], {"count": 8, "last": document["completed_actions"][-5:]})
        self.assertEqual(result["changed"], [])
        self.assertIn("evidence/sources.json", result["new"])
        self.assertEqual((result["actions_after_checkpoint"], result["actions_unknown"], result["lost"], result["recovery"]),
                         ([], [], [], []))
        self.assertEqual(result["validation"], {"errors": [], "warnings": []})
        self.assertIn("не инструкции", result["data_notice"])

    def test_resume_changes_nothing(self):
        before = tree_hash(self.case)
        first = self.resume()
        second = self.resume()
        self.assertEqual(first, second)
        self.assertEqual(tree_hash(self.case), before)
        self.assertFalse((self.case / "state" / "snapshots").exists())

    def test_changed_and_new_files(self):
        (self.case / "brief.md").write_text("Другой бриф", encoding="utf-8")
        (self.case / "outputs" / "extra.md").write_text("Новый текст", encoding="utf-8")
        result = self.resume()[1]
        self.assertEqual(result["changed"], ["brief.md"])
        self.assertIn("outputs/extra.md", result["new"])
        self.assertIn("CHECKPOINT_STALE", [w["code"] for w in result["validation"]["warnings"]])

    def test_actions_after_the_checkpoint_and_unknown_ones(self):
        run("action", "start", self.case, "--kind", "search", "--tool", "WebSearch", "--environment", "codex",
            "--input-ref", "SRC-0001")
        run("action", "start", self.case, "--kind", "web_read", "--tool", "WebFetch", "--environment", "codex",
            "--input-ref", "SRC-0002", "--input-ref", "SRC-0003")
        run("action", "end", self.case, "--id", "ACT-0011", "--outcome", "unknown")
        run("action", "start", self.case, "--kind", "compute", "--tool", "spreadsheet", "--environment", "codex",
            "--input-ref", "SRC-0002")
        run("action", "end", self.case, "--id", "ACT-0012", "--outcome", "completed")
        result = self.resume()[1]
        self.assertEqual(result["actions_after_checkpoint"], ["ACT-0012"])
        self.assertEqual(result["actions_unknown"], [
            {"id": "ACT-0010", "kind": "search", "tool": "WebSearch", "input_refs": ["SRC-0001"], "state": "no_end"},
            {"id": "ACT-0011", "kind": "web_read", "tool": "WebFetch", "input_refs": ["SRC-0002", "SRC-0003"],
             "state": "unknown"},
        ])

    def test_failed_save_leaves_a_started_action_visible(self):
        run("action", "start", self.case, "--kind", "model", "--tool", "analysis", "--environment", "codex")
        with mock.patch.object(ck, "write_snapshots", side_effect=OSError("disk")):
            self.assertEqual(self.save()[0], 3)
        result = self.resume()[1]
        self.assertEqual((result["status"], result["id"]), ("ok", "CKP-0002"))
        self.assertEqual([a["id"] for a in result["actions_unknown"]], ["ACT-0010"])
        self.assertEqual(self.save()[0], 0)

    def test_later_checkpoint_covers_earlier_actions(self):
        run("action", "start", self.case, "--kind", "compute", "--tool", "spreadsheet", "--environment", "codex",
            "--input-ref", "SRC-0002")
        run("action", "end", self.case, "--id", "ACT-0010", "--outcome", "completed")
        self.assertEqual(self.resume()[1]["actions_after_checkpoint"], ["ACT-0010"])
        self.save()
        result = self.resume()[1]
        self.assertEqual((result["actions_after_checkpoint"], result["completed_actions"]["last"][-1]),
                         ([], "ACT-0010"))

    def test_journal_corrupted_in_the_middle_does_not_crash_resume(self):
        path = self.case / JOURNAL
        lines = path.read_bytes().splitlines(keepends=True)
        lines.insert(3, b"{broken\n")
        path.write_bytes(b"".join(lines))
        code, result = self.resume()
        self.assertEqual(code, 0)
        self.assertIsNone(result["actions_unknown"])
        self.assertIn("FILE_CORRUPT", [e["code"] for e in result["validation"]["errors"]])

    def test_unsupported_case_version_is_blocked(self):
        case = read_json(self.case / "case.json")
        write_json(self.case / "case.json", {**case, "format_version": 2})
        before = tree_hash(self.case)
        code, result = self.resume()
        self.assertEqual((code, result["status"], result["case_version"], result["supported"]), (0, "blocked", 2, [1]))
        self.assertIn("миграция", result["message"])
        self.assertEqual(tree_hash(self.case), before)

    def test_unreadable_case_is_blocked(self):
        (self.case / "case.json").write_text("{nope", encoding="utf-8")
        self.assertEqual(self.resume()[1]["status"], "blocked")
        (self.case / "case.json").unlink()
        self.assertEqual(self.resume()[1]["status"], "blocked")

    def test_checkpoint_of_another_version_is_a_loss(self):
        document = self.checkpoint("CKP-0002")
        write_json(self.case / CKP_DIR / "CKP-0002.json", {**document, "format_version": 2})
        result = self.resume()[1]
        self.assertEqual((result["status"], result["id"]), ("fallback", "CKP-0001"))
        self.assertEqual(result["lost"][0]["name"], "CKP-0002")
        self.assertEqual(result["lost"][0]["reason"], "unsupported format version")
        self.assertNotIn("FORMAT_UNSUPPORTED", [e["code"] for e in result["validation"]["errors"]])

    def test_every_checkpoint_damaged(self):
        self.break_checkpoints("CKP-0001", "CKP-0002")
        code, result = self.resume()
        self.assertEqual((code, result["status"], result["id"], result["recovery"]), (0, "none_valid", None, []))
        self.assertEqual([item["name"] for item in result["lost"]], ["CKP-0001", "CKP-0002"])
        self.assertIn("CHECKPOINT_NONE_VALID", [e["code"] for e in result["validation"]["errors"]])


class Restore(DemoCaseTestCase):
    def restore(self, *args):
        return run("restore", self.case, *args)

    def saved(self):
        """Save once so that registries and snapshots are pinned."""
        self.assertEqual(self.save()[0], 0)

    def test_damaged_registry_is_named_and_brought_back(self):
        self.saved()
        path = self.case / "decisions" / "decisions.json"
        original = path.read_bytes()
        path.write_text("{nope", encoding="utf-8")
        code, result = self.save()
        self.assertEqual((code, result["refused"]), (1, "validation_errors"))

        code, result = run("resume", self.case)
        self.assertEqual((code, result["status"], result["id"]), (0, "ok", "CKP-0003"))
        self.assertIn("decisions/decisions.json", result["changed"])
        self.assertIn("FILE_CORRUPT", [e["code"] for e in result["validation"]["errors"]])
        self.assertEqual(result["recovery"][0]["command"], "restore")
        self.assertEqual(result["recovery"][0]["checkpoint"], "CKP-0003")
        self.assertEqual(result["recovery"][0]["files"], [{"path": "decisions/decisions.json", "state": "changed", "snapshot": True}])
        self.assertTrue(result["recovery"][0]["recoverable"])
        self.assertEqual([c["topic"] for c in result["human_choices"]], [None])

        before = tree_hash(self.case)
        code, result = self.restore("--checkpoint", "CKP-0003")
        self.assertEqual((code, result["status"], result["would_replace"]), (0, "preview", [{"path": "decisions/decisions.json", "state": "changed"}]))
        self.assertEqual(tree_hash(self.case), before)

        damaged = hashlib.sha256(path.read_bytes()).hexdigest()
        code, result = self.restore("--checkpoint", "CKP-0003", "--confirmed")
        self.assertEqual((code, result["status"]), (0, "done"))
        self.assertEqual(path.read_bytes(), original)
        self.assertEqual(result["preserved_sha256"], {"decisions/decisions.json": damaged})
        self.assertEqual((self.case / "state" / "snapshots" / damaged).read_text(encoding="utf-8"), "{nope")
        self.assertEqual(result["validation"], {"errors": [], "warnings": []})
        self.assertEqual(self.save()[0], 0)

    def test_changed_output_comes_back_byte_for_byte(self):
        self.saved()
        path = self.case / "outputs" / "product-strategy.md"
        original = path.read_bytes()
        path.write_bytes(original + "\nЛишняя строка\n".encode("utf-8"))
        self.assertEqual(run("resume", self.case)[1]["changed"], ["outputs/product-strategy.md"])
        code, result = self.restore("--checkpoint", "CKP-0003", "--path", "outputs/product-strategy.md", "--confirmed")
        self.assertEqual((code, result["status"]), (0, "done"))
        self.assertEqual(path.read_bytes(), original)
        self.assertEqual(run("resume", self.case)[1]["changed"], [])

    def test_unchanged_file_is_left_alone(self):
        self.saved()
        code, result = self.restore("--checkpoint", "CKP-0003", "--confirmed")
        self.assertEqual((code, result["restored"], result["preserved_sha256"]), (0, [], {}))

    def test_registries_are_written_last(self):
        self.saved()
        (self.case / "brief.md").write_text("x", encoding="utf-8")
        (self.case / "decisions" / "decisions.json").write_text("{nope", encoding="utf-8")
        (self.case / "outputs" / "validation-plan.md").write_text("y", encoding="utf-8")
        order = []
        real = ck.replace_file
        with mock.patch.object(ck, "replace_file", side_effect=lambda root, rel, data: (order.append(rel), real(root, rel, data))):
            self.assertEqual(self.restore("--checkpoint", "CKP-0003", "--confirmed")[0], 0)
        self.assertEqual(order, ["brief.md", "outputs/validation-plan.md", "decisions/decisions.json"])

    def test_deleted_artifact_is_listed_and_restored(self):
        extra = self.case / "outputs" / "extra.md"
        extra.write_text("Дополнительный текст\n", encoding="utf-8")
        self.saved()
        extra.unlink()
        result = run("resume", self.case)[1]
        self.assertEqual((result["status"], result["id"]), ("fallback", "CKP-0002"))
        self.assertEqual(result["lost"][0]["name"], "CKP-0003")
        self.assertEqual(result["lost"][0]["missing"], ["outputs/extra.md"])
        self.assertEqual(result["recovery"][0]["checkpoint"], "CKP-0003")
        code, result = self.restore("--checkpoint", "CKP-0003", "--confirmed")
        self.assertEqual((code, result["restored"]), (0, [{"path": "outputs/extra.md", "state": "missing"}]))
        self.assertEqual(result["preserved_sha256"], {})
        self.assertEqual(extra.read_text(encoding="utf-8"), "Дополнительный текст\n")
        result = run("resume", self.case)[1]
        self.assertEqual((result["status"], result["id"], result["lost"]), ("ok", "CKP-0003", []))

    def test_checkpoint_without_snapshots_cannot_restore(self):
        (self.case / "brief.md").write_text("Другой бриф", encoding="utf-8")
        before = tree_hash(self.case)
        code, result = self.restore("--checkpoint", "CKP-0002", "--confirmed")
        self.assertEqual((code, result["refused"], result["refused_paths"]), (1, "snapshot_unavailable", ["brief.md"]))
        self.assertEqual(tree_hash(self.case), before)

    def test_missing_or_mismatched_snapshot_replaces_nothing(self):
        self.saved()
        brief, plan = self.case / "brief.md", self.case / "outputs" / "validation-plan.md"
        stored = {a["path"]: a["sha256"] for a in self.checkpoint("CKP-0003")["artifacts"]}
        brief.write_text("x", encoding="utf-8")
        plan.write_text("y", encoding="utf-8")
        snapshots = self.case / "state" / "snapshots"
        (snapshots / stored["brief.md"]).unlink()
        (snapshots / stored["outputs/validation-plan.md"]).write_text("wrong", encoding="utf-8")
        before = tree_hash(self.case)
        code, result = self.restore("--checkpoint", "CKP-0003", "--confirmed")
        self.assertEqual((code, result["refused"]), (1, "snapshot_unavailable"))
        self.assertEqual(result["refused_paths"], ["brief.md", "outputs/validation-plan.md"])
        self.assertEqual(tree_hash(self.case), before)

    def test_other_refusals(self):
        self.saved()
        self.break_checkpoints("CKP-0001")
        cases = [
            (["--checkpoint", "CKP-0099"], "checkpoint_unknown"),
            (["--checkpoint", "CKP-0001"], "checkpoint_unfit"),
            (["--checkpoint", "CKP-0003", "--path", "../outside.md"], "unsafe_path"),
            (["--checkpoint", "CKP-0003", "--path", "C:/outside.md"], "unsafe_path"),
            (["--checkpoint", "CKP-0003", "--path", "outputs\\product-strategy.md"], "unsafe_path"),
            (["--checkpoint", "CKP-0003", "--path", "sources/service-notes.md"], "path_not_in_checkpoint"),
        ]
        before = tree_hash(self.case)
        for args, reason in cases:
            with self.subTest(args=args):
                code, result = self.restore(*args, "--confirmed")
                self.assertEqual((code, result["refused"]), (1, reason))
        self.assertEqual(tree_hash(self.case), before)

    def test_crash_while_replacing_keeps_the_previous_version_in_snapshots(self):
        self.saved()
        path = self.case / "brief.md"
        path.write_text("Испорченный бриф", encoding="utf-8")
        damaged = hashlib.sha256(path.read_bytes()).hexdigest()
        with mock.patch.object(ck, "replace_file", side_effect=OSError("disk")):
            code, result = self.restore("--checkpoint", "CKP-0003", "--confirmed")
        self.assertEqual((code, result["refused"], result["preserved_sha256"]), (1, "restore_incomplete", {"brief.md": damaged}))
        self.assertEqual(result["pending"], ["brief.md"])
        self.assertEqual(path.read_text(encoding="utf-8"), "Испорченный бриф")
        self.assertTrue((self.case / "state" / "snapshots" / damaged).is_file())
        self.assertEqual(self.restore("--checkpoint", "CKP-0003", "--confirmed")[0], 0)


class MovedCase(unittest.TestCase):
    def test_case_copied_to_a_cyrillic_path_with_spaces_continues(self):
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        parent = tmp / "Мои кейсы" / "папка 1"
        parent.mkdir(parents=True)
        case = parent / "demo-case"
        shutil.copytree(DEMO, case)
        reports = []

        def call(*argv):
            code, report = run(*argv)
            reports.append(json.dumps(report, ensure_ascii=False))
            return code, report

        request = tmp / "запрос.json"
        write_json(request, REQUEST)
        self.assertEqual(call("resume", case)[1]["status"], "ok")
        self.assertEqual(call("action", "start", case, "--kind", "search", "--tool", "WebSearch",
                              "--environment", "codex", "--input-ref", "SRC-0001")[0], 0)
        self.assertEqual(call("action", "end", case, "--id", "ACT-0010", "--outcome", "completed")[0], 0)
        code, result = call("save", case, "--from", request)
        self.assertEqual((code, result["id"]), (0, "CKP-0003"))
        (case / "brief.md").write_text("x", encoding="utf-8")
        self.assertEqual(call("restore", case, "--checkpoint", "CKP-0003", "--confirmed")[0], 0)
        self.assertEqual(call("resume", case)[1]["status"], "ok")
        for text in reports:
            for form in (str(tmp), tmp.as_posix(), "Мои кейсы"):
                self.assertNotIn(form, text)
        for path in case.rglob("*"):
            if path.is_file() and path.suffix in (".json", ".jsonl", ".md"):
                text = path.read_text(encoding="utf-8")
                self.assertNotIn(tmp.as_posix(), text.replace("\\\\", "/"))
                self.assertNotIn("Мои кейсы", text)


class CommandLine(DemoCaseTestCase):
    def test_exit_codes(self):
        self.assertEqual(run("resume", self.case)[0], 0)
        self.assertEqual(run("resume", self.tmp / "nowhere")[0], 2)
        self.assertEqual(run("action", "end", self.case, "--id", "ACT-0001", "--outcome", "completed")[0], 1)
        out = io.StringIO()
        with redirect_stdout(out):
            self.assertEqual(ck.main([]), 2)
        self.assertEqual(json.loads(out.getvalue().splitlines()[0])["status"], "usage_error")

    def test_internal_failure_has_no_exception_text(self):
        out = io.StringIO()
        with mock.patch.object(ck, "resume", side_effect=RuntimeError("секретный текст")), redirect_stdout(out):
            self.assertEqual(ck.main(["resume", str(self.case)]), 3)
        self.assertEqual(out.getvalue().strip(), '{"status": "internal_error"}')

    def test_save_without_a_readable_request_exits_1(self):
        for path in (self.tmp / "none.json", self.tmp):
            code, result = run("save", self.case, "--from", path)
            self.assertEqual((code, result["refused"]), (1, "request_unreadable"))

    def test_script_runs_as_a_process(self):
        result = subprocess.run([sys.executable, str(ROOT / "tools" / "checkpoint.py"), "resume", str(self.case)],
                                capture_output=True, check=False)
        self.assertEqual(result.returncode, 0)
        first = json.loads(result.stdout.decode("utf-8").splitlines()[0])
        self.assertEqual(first["status"], "ok")
        self.assertNotIn(str(self.tmp), result.stdout.decode("utf-8"))


class LossRegressions(DemoCaseTestCase):
    """The sample journal names CKP-0002 as a result; losing that file must not break the case."""

    def test_untouched_demo_case_survives_the_loss_of_its_newest_checkpoint(self):
        self.break_checkpoints("CKP-0002")
        self.assertEqual(self.save()[1]["refused"], "loss_not_acknowledged")
        code, result = self.save("--acknowledge-loss")
        self.assertEqual((code, result["id"]), (0, "CKP-0003"))
        self.assertEqual(self.hidden(), [".lost-CKP-0002.json"])
        self.assertEqual(self.codes(), ([], []))
        self.assertEqual(run("resume", self.case)[1]["status"], "ok")
        self.assertEqual(self.save()[1]["id"], "CKP-0004")

    def test_untouched_demo_case_with_every_checkpoint_lost(self):
        self.break_checkpoints("CKP-0001", "CKP-0002")
        code, result = self.save("--acknowledge-loss", goal="Начать заново")
        self.assertEqual((code, result["id"], result["seq"]), (0, "CKP-0003", 1))
        self.assertEqual(self.codes(), ([], []))
        self.assertEqual(self.save()[0], 0)

    def test_hidden_checkpoint_ids_stay_taken(self):
        (self.case / CKP_DIR / "CKP-0002.json").rename(self.case / CKP_DIR / ".rejected-CKP-0002.json")
        self.assertEqual(self.codes()[0], [])
        self.assertEqual(self.save()[1]["id"], "CKP-0003")

    def test_rejected_publication_gives_the_lost_files_back(self):
        (self.case / CKP_DIR / "CKP-0003.json").write_text("not json", encoding="utf-8")
        self.break_checkpoints("CKP-0002")
        real = ck.validate_case
        calls = []

        def second_call_fails(root):
            report = real(root)
            calls.append(1)
            if len(calls) == 2:
                report.errors.append(vp.Finding("SCHEMA", "x", None, "y"))
            return report

        with mock.patch.object(ck, "validate_case", side_effect=second_call_fails):
            code, result = self.save("--acknowledge-loss")
        self.assertEqual((code, result["refused"]), (1, "published_rejected"))
        self.assertEqual(self.hidden(), [".rejected-CKP-0004.json"])
        self.assertTrue((self.case / CKP_DIR / "CKP-0002.json").is_file())
        self.assertTrue((self.case / CKP_DIR / "CKP-0003.json").is_file())
        self.assertEqual(self.save()[1]["refused"], "loss_not_acknowledged")
        code, result = self.save("--acknowledge-loss")
        self.assertEqual((code, result["id"]), (0, "CKP-0005"))
        self.assertEqual(self.codes(), ([], []))

    def test_resume_is_not_fresh_when_only_hidden_files_remain(self):
        for name in (".lost-CKP-0001.json", ".rejected-CKP-0002.json"):
            (self.case / CKP_DIR / name).write_text("x", encoding="utf-8")
        for name in ("CKP-0001.json", "CKP-0002.json"):
            (self.case / CKP_DIR / name).unlink()
        result = run("resume", self.case)[1]
        self.assertEqual(result["status"], "none_valid")
        self.assertEqual(result["hidden_checkpoints"], [".lost-CKP-0001.json", ".rejected-CKP-0002.json"])

    def test_single_lost_file_stays_visible_after_a_crash_while_hiding(self):
        self.break_checkpoints("CKP-0002")
        with mock.patch.object(ck, "hide_lost", side_effect=OSError("disk")):
            self.assertEqual(self.save("--acknowledge-loss")[0], 3)
        report = vp.validate_case(self.case)
        self.assertEqual((report.errors, report.current_checkpoint), ([], "CKP-0003"))
        self.assertIn("CHECKPOINT_FALLBACK", [f.code for f in report.warnings])
        self.assertEqual(self.save()[1]["refused"], "loss_not_acknowledged")
        self.assertEqual(self.save("--acknowledge-loss")[0], 0)
        self.assertEqual(self.codes(), ([], []))

    def test_unreadable_previous_checkpoint_does_not_break_the_chain(self):
        self.break_checkpoints("CKP-0002")
        self.save("--acknowledge-loss")
        self.assertEqual(self.checkpoint("CKP-0003")["seq"], 2)
        self.assertEqual(self.save()[1]["id"], "CKP-0004")
        self.assertEqual(self.checkpoint("CKP-0004")["seq"], 3)
        self.break_checkpoints("CKP-0003")
        report = vp.validate_case(self.case)
        self.assertEqual((report.errors, report.current_checkpoint), ([], "CKP-0004"))


class ResolvedActions(DemoCaseTestCase):
    def act(self, *args):
        self.assertEqual(run("action", *args)[0], 0)

    def start(self, *extra):
        self.act("start", self.case, "--kind", "search", "--tool", "WebSearch", "--environment", "codex",
                 "--input-ref", "SRC-0001", *extra)

    def end(self, action_id, outcome):
        self.act("end", self.case, "--id", action_id, "--outcome", outcome)

    def test_completed_retry_settles_an_unknown_action(self):
        self.start()
        self.end("ACT-0010", "unknown")
        self.assertEqual(self.save(stage="done", next_step=None)[1]["refused"], "unfinished_actions")
        self.start("--retry-of", "ACT-0010", "--retry-reason", "проверка исхода")
        self.assertEqual(self.save(stage="done", next_step=None)[1]["refused"], "unfinished_actions")
        self.end("ACT-0011", "completed")
        result = run("resume", self.case)[1]
        self.assertEqual(result["actions_unknown"], [])
        self.assertEqual(result["actions_resolved"], [{"id": "ACT-0010", "resolved_by": "ACT-0011"}])
        code, result = self.save(stage="done", next_step=None)
        self.assertEqual(code, 0)
        self.assertEqual(self.checkpoint(result["id"])["stage"], "done")

    def test_failed_retry_does_not_settle(self):
        self.start()
        self.end("ACT-0010", "unknown")
        self.start("--retry-of", "ACT-0010", "--retry-reason", "проверка")
        self.end("ACT-0011", "failed")
        self.assertEqual([a["id"] for a in run("resume", self.case)[1]["actions_unknown"]], ["ACT-0010"])
        self.assertEqual(self.save(stage="done", next_step=None)[1]["actions"], ["ACT-0010"])

    def test_resolution_follows_the_retry_chain(self):
        self.start()
        self.end("ACT-0010", "unknown")
        self.start("--retry-of", "ACT-0010", "--retry-reason", "первая проверка")
        self.end("ACT-0011", "unknown")
        self.start("--retry-of", "ACT-0011", "--retry-reason", "вторая проверка")
        self.end("ACT-0012", "completed")
        result = run("resume", self.case)[1]
        self.assertEqual(result["actions_unknown"], [])
        self.assertEqual([item["id"] for item in result["actions_resolved"]], ["ACT-0010", "ACT-0011"])
        self.assertEqual(self.save(stage="done", next_step=None)[0], 0)


class CaseVersions(DemoCaseTestCase):
    def test_unsupported_registry_is_named_and_can_be_restored(self):
        self.assertEqual(self.save()[0], 0)
        path = self.case / "decisions" / "decisions.json"
        original = path.read_bytes()
        write_json(path, {**read_json(path), "format_version": 2})
        code, result = run("resume", self.case)
        self.assertEqual((code, result["status"]), (0, "blocked"))
        self.assertEqual(result["files"], [{"file": "decisions/decisions.json", "version": 2}])
        self.assertEqual((result["case_version"], result["supported"]), (1, [1]))
        code, result = run("restore", self.case, "--checkpoint", "CKP-0003", "--confirmed")
        self.assertEqual((code, result["status"]), (0, "done"))
        self.assertEqual(path.read_bytes(), original)
        self.assertEqual(run("resume", self.case)[1]["status"], "ok")

    def test_unsupported_case_file_names_itself(self):
        case = read_json(self.case / "case.json")
        write_json(self.case / "case.json", {**case, "format_version": 2})
        result = run("resume", self.case)[1]
        self.assertEqual((result["file"], result["case_version"], result["supported"]), ("case.json", 2, [1]))


class ArtifactFolders(DemoCaseTestCase):
    def test_dot_files_are_not_artifacts_and_not_checked(self):
        (self.case / "outputs" / ".draft.md").write_text("Битая ссылка [CLM-9]\n", encoding="utf-8")
        self.assertEqual(self.codes()[0], [])
        self.assertNotIn("outputs/.draft.md", ck.collect_artifacts(self.case))
        self.assertEqual(self.save()[0], 0)

    def test_stale_temporaries_are_removed_from_artifact_folders(self):
        stale = [self.case / ".tmp-1-brief.md", self.case / "outputs" / ".tmp-1-x.md",
                 self.case / "evidence" / ".tmp-1-claims.json"]
        for path in stale:
            path.write_text("half", encoding="utf-8")
        self.assertEqual(self.save()[0], 0)
        self.assertEqual([p for p in stale if p.exists()], [])

    def test_refusal_does_not_clear_temporaries(self):
        stale = self.case / "outputs" / ".tmp-1-x.md"
        stale.write_text("half", encoding="utf-8")
        (self.case / "decisions" / "decisions.json").write_text("{nope", encoding="utf-8")
        before = tree_hash(self.case)
        self.assertEqual(self.save()[1]["refused"], "validation_errors")
        self.assertEqual(tree_hash(self.case), before)

    def test_snapshot_folder_outside_the_case_is_refused(self):
        before = tree_hash(self.case)
        with mock.patch.object(ck, "inside_case", return_value=False):
            code, result = self.save()
        self.assertEqual((code, result["refused"]), (1, "outside_case"))
        self.assertEqual(tree_hash(self.case), before)

    def test_damaged_snapshot_is_healed_by_the_next_save(self):
        self.save()
        brief = self.checkpoint("CKP-0003")
        digest = next(a["sha256"] for a in brief["artifacts"] if a["path"] == "brief.md")
        copy = self.case / "state" / "snapshots" / digest
        copy.write_text("повреждено", encoding="utf-8")
        code, result = self.save()
        self.assertEqual((code, result["snapshots_written"]), (0, 1))
        self.assertEqual(copy.read_bytes(), (self.case / "brief.md").read_bytes())

    def test_crash_between_hiding_and_the_final_check(self):
        (self.case / CKP_DIR / "CKP-0003.json").write_text("not json", encoding="utf-8")
        self.break_checkpoints("CKP-0002")
        real = ck.validate_case
        calls = []

        def crash_on_second(root):
            calls.append(1)
            if len(calls) == 2:
                raise OSError("disk")
            return real(root)

        with mock.patch.object(ck, "validate_case", side_effect=crash_on_second):
            self.assertEqual(self.save("--acknowledge-loss")[0], 3)
        result = run("resume", self.case)[1]
        self.assertEqual((result["status"], result["id"], result["lost"]), ("ok", "CKP-0004", []))
        self.assertEqual(self.save()[0], 0)


class ReportsStayClean(DemoCaseTestCase):
    def test_refusals_carry_no_absolute_paths(self):
        outside = str(self.tmp / "elsewhere" / "file.md")
        calls = [
            ("restore", self.case, "--checkpoint", "CKP-0002", "--path", outside, "--confirmed"),
            ("restore", self.case, "--checkpoint", "CKP-0002", "--path", "C:/x/y.md"),
            ("save", self.case, "--from", self.tmp / "absent.json"),
            ("action", "end", self.case, "--id", "ACT-0099", "--outcome", "completed", "--cost-file", self.tmp / "no.json"),
            ("action", "start", self.case, "--kind", "model", "--tool", "t", "--environment", "codex",
             "--input-ref", outside),
        ]
        for call in calls:
            out = io.StringIO()
            with redirect_stdout(out):
                code = ck.main([str(a) for a in call])
            self.assertIn(code, (1, 2))
            for form in (str(self.tmp), self.tmp.as_posix(), outside, "elsewhere"):
                self.assertNotIn(form, out.getvalue())


KILL_WRAPPER = """
import os, sys
sys.path.insert(0, __TOOLS__)
k = int(sys.argv[1]); n = [0]
real_fsync, real_replace = os.fsync, os.replace
def tick():
    n[0] += 1
    if n[0] == k:
        os._exit(9)
def fsync(fd):
    tick(); real_fsync(fd)
def replace(a, b):
    tick(); real_replace(a, b); tick()
os.fsync, os.replace = fsync, replace
import checkpoint as ck
code = ck.main(sys.argv[2:])
sys.stderr.write("TICKS " + str(n[0]))
sys.exit(code)
""".replace("__TOOLS__", repr(str(ROOT / "tools")))


class KillMatrix(unittest.TestCase):
    """Kill the tool with os._exit at chosen fsync/replace steps, then check the case and repeat the command."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.counter = 0

    def fresh(self, prepare=None):
        self.counter += 1
        case = self.tmp / f"run-{self.counter}" / "demo-case"
        shutil.copytree(DEMO, case)
        write_json(case.parent / "request.json", REQUEST)
        if prepare:
            prepare(case)
        return case

    def kill(self, k, argv):
        env = {**os.environ, "PYTHONUTF8": "1", "PYTHONDONTWRITEBYTECODE": "1"}
        result = subprocess.run([sys.executable, "-c", KILL_WRAPPER, str(k), *map(str, argv)],
                                capture_output=True, env=env, check=False)
        found = re.search(r"TICKS (\d+)", result.stderr.decode("utf-8", "replace"))
        return result.returncode, int(found.group(1)) if found else None

    def matrix(self, prepare, argv, check, points=lambda total: (1, total)):
        total = self.kill(0, argv(self.fresh(prepare)))[1]
        self.assertTrue(total)
        chosen = sorted({p for p in points(total) if 1 <= p <= total})
        for k in chosen:
            with self.subTest(killed_at=k, of=total):
                case = self.fresh(prepare)
                args = argv(case)
                self.assertEqual(self.kill(k, args)[0], 9)
                check(case, args)

    def healthy(self, case):
        code, result = run("resume", case)
        self.assertEqual(code, 0)
        report = vp.validate_case(case)
        self.assertEqual(report.errors, [])
        return result

    def test_save(self):
        def check(case, args):
            result = self.healthy(case)
            self.assertEqual(result["status"], "ok")
            self.assertIn(result["id"], ("CKP-0002", "CKP-0003"))
            self.assertEqual(run(*args)[0], 0)
            self.assertEqual(vp.validate_case(case).errors, [])

        self.matrix(None, lambda c: ["save", c, "--from", c.parent / "request.json"], check,
                    lambda total: (1, total // 2, total - 1, total))

    def test_save_with_two_lost_files(self):
        def prepare(case):
            for name in ("CKP-0002", "CKP-0003"):
                (case / CKP_DIR / f"{name}.json").write_text("not json", encoding="utf-8")

        def check(case, args):
            result = self.healthy(case)
            self.assertIn(result["status"], ("ok", "fallback"))
            self.assertEqual(run(*args)[0], 0)
            self.assertEqual(vp.validate_case(case).errors, [])
            self.assertEqual(run("resume", case)[1]["lost"], [])

        self.matrix(prepare, lambda c: ["save", c, "--from", c.parent / "request.json", "--acknowledge-loss"], check,
                    lambda total: (total // 2, total - 4, total - 2, total))

    def test_restore(self):
        saved = {}

        def prepare(case):
            self.assertEqual(run("save", case, "--from", case.parent / "request.json")[0], 0)
            for rel in ("brief.md", "decisions/decisions.json", "outputs/validation-plan.md"):
                saved[rel] = (case / rel).read_bytes()
                (case / rel).write_text("{повреждено", encoding="utf-8")

        def check(case, args):
            self.assertEqual(run("resume", case)[0], 0)
            code, result = run(*args)
            self.assertEqual((code, result["status"]), (0, "done"))
            for rel, content in saved.items():
                self.assertEqual((case / rel).read_bytes(), content)
            self.assertEqual(vp.validate_case(case).errors, [])

        self.matrix(prepare, lambda c: ["restore", c, "--checkpoint", "CKP-0003", "--confirmed"], check,
                    lambda total: (total // 3, 2 * total // 3, total))

    def test_action_start_and_end(self):
        def check(case, args):
            self.healthy(case)
            code, result = run(*args)
            self.assertTrue(code == 0 or result["refused"] == "repeat")

        start = ["--kind", "search", "--tool", "WebSearch", "--environment", "codex", "--input-ref", "SRC-0001"]
        self.matrix(None, lambda c: ["action", "start", c, *start], check)

    def test_action_human_pair(self):
        def check(case, args):
            self.healthy(case)
            before = len((case / JOURNAL).read_bytes().splitlines())
            self.assertIn(before, (18, 20))
            self.assertEqual(run(*args)[0], 0)
            self.assertEqual(vp.validate_case(case).errors, [])

        self.matrix(None, lambda c: ["action", "human", c, "--tool", "chat", "--environment", "codex",
                                     "--result-ref", "DEC-0002"], check)


if __name__ == "__main__":
    unittest.main()
