import hashlib
import io
import json
import shutil
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import datetime
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))

import case as tool  # noqa: E402
import validate_project as vp  # noqa: E402


def tree_hash(root):
    digest = hashlib.sha256()
    for path in sorted(Path(root).rglob("*")):
        digest.update(path.relative_to(root).as_posix().encode("utf-8"))
        if path.is_file():
            digest.update(path.read_bytes())
    return digest.hexdigest()


class ToolTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.projects = self.tmp / "projects"
        patcher = mock.patch.object(tool, "PROJECTS", self.projects)
        patcher.start()
        self.addCleanup(patcher.stop)

    def run_tool(self, *argv):
        out = io.StringIO()
        with redirect_stdout(out):
            code = tool.main([str(a) for a in argv])
        return code, json.loads(out.getvalue().splitlines()[0])

    def request(self, value, raw=None):
        path = self.tmp / "request.json"
        path.write_bytes(raw if raw is not None else json.dumps(value, ensure_ascii=False).encode("utf-8"))
        return path

    def new(self, value):
        return self.run_tool("new", "--from", self.request(value))


class NewCase(ToolTestCase):
    def test_new_creates_valid_case(self):
        code, result = self.new({"case_id": "retention-2026", "title": "Почему падает удержание"})
        self.assertEqual((code, result["status"], result["case_id"]), (0, "ok", "retention-2026"))
        self.assertEqual(result["path"], "projects/retention-2026")
        folder = self.projects / "retention-2026"
        self.assertEqual(vp.validate_case(folder).errors, [])
        document = json.loads((folder / "case.json").read_text(encoding="utf-8"))
        self.assertEqual(document["title"], "Почему падает удержание")
        self.assertIsNotNone(datetime.fromisoformat(document["created_at"]).utcoffset())

    def test_new_accepts_request_with_bom(self):
        raw = b"\xef\xbb\xbf" + json.dumps({"case_id": "a", "title": "Т"}, ensure_ascii=False).encode("utf-8")
        code, _ = self.run_tool("new", "--from", self.request(None, raw))
        self.assertEqual(code, 0)

    def test_new_refuses_bad_ids(self):
        for bad in ("../x", "x/y", "A", "кейс", "a\n", "", "-", "a b", "a_b", 7):
            with self.subTest(repr(bad)):
                code, result = self.new({"case_id": bad, "title": "Т"})
                self.assertEqual((code, result["refused"]), (1, "case_id_invalid"))
                self.assertFalse(self.projects.exists() and any(self.projects.iterdir()))

    def test_new_refuses_existing_case(self):
        self.new({"case_id": "a", "title": "Первый"})
        before = tree_hash(self.projects)
        code, result = self.new({"case_id": "a", "title": "Второй"})
        self.assertEqual((code, result["refused"]), (1, "case_exists"))
        self.assertEqual(tree_hash(self.projects), before)

    def test_new_refuses_folder_without_case_json(self):
        (self.projects / "a").mkdir(parents=True)
        (self.projects / "a" / "note.txt").write_text("своё", encoding="utf-8")
        before = tree_hash(self.projects)
        code, result = self.new({"case_id": "a", "title": "Т"})
        self.assertEqual((code, result["refused"]), (1, "case_exists"))
        self.assertEqual(tree_hash(self.projects), before)

    def test_new_bad_requests(self):
        for value in (["a"], {"case_id": "a"}, {"title": "Т"}, {"case_id": "a", "title": "Т", "extra": 1},
                      {"case_id": "a", "title": ""}, {"case_id": "a", "title": 7}):
            with self.subTest(repr(value)):
                code, result = self.new(value)
                self.assertEqual((code, result["refused"]), (1, "bad_request"))
                self.assertFalse(self.projects.exists() and any(self.projects.iterdir()))

    def test_new_unreadable_request(self):
        code, result = self.run_tool("new", "--from", self.tmp / "missing.json")
        self.assertEqual((code, result["refused"]), (1, "request_unreadable"))
        code, result = self.run_tool("new", "--from", self.request(None, b"not json"))
        self.assertEqual((code, result["refused"]), (1, "request_unreadable"))


class NewCaseFailures(ToolTestCase):
    def test_new_leaves_nothing_after_a_write_failure(self):
        with mock.patch.object(tool.os, "replace", side_effect=OSError):
            code, result = self.new({"case_id": "a", "title": "Т"})
        self.assertEqual((code, result["status"]), (3, "internal_error"))
        self.assertFalse((self.projects / "a").exists())
        self.assertEqual(self.new({"case_id": "a", "title": "Т"})[0], 0)

    def test_new_refuses_a_too_long_id(self):
        code, result = self.new({"case_id": "a" * 300, "title": "Т"})
        self.assertEqual((code, result["refused"]), (1, "case_id_invalid"))


class Usage(ToolTestCase):
    def setUp(self):
        super().setUp()
        self.new({"case_id": "a", "title": "Т"})
        self.case = self.projects / "a"

    def journal(self, *kinds, tail=b""):
        path = self.case / "state" / "actions.jsonl"
        path.parent.mkdir(exist_ok=True)
        lines = [json.dumps({"format_version": 1, "id": f"ACT-{n:04d}", "phase": "start", "kind": kind, "tool": "t",
                             "environment": "codex", "model": None, "input_refs": [],
                             "at": "2026-11-06T10:00:00+03:00"})
                 for n, kind in enumerate(kinds, start=1)]
        path.write_bytes("".join(line + "\n" for line in lines).encode("utf-8") + tail)

    def sources(self, *statuses):
        path = self.case / "evidence" / "sources.json"
        path.parent.mkdir(exist_ok=True)
        records = [{"id": f"SRC-{n:04d}", "record_status": status, "type": "web", "extraction": {"status": "ok"}}
                   for n, status in enumerate(statuses, start=1)]
        records.append({"id": "SRC-9000", "record_status": "active", "type": "md", "extraction": {"status": "ok"}})
        records.append({"id": "SRC-9001", "record_status": "active", "type": "web", "extraction": {"status": "unavailable"}})
        path.write_text(json.dumps({"format_version": 1, "records": records}), encoding="utf-8")

    def test_usage_empty_case(self):
        self.assertEqual(self.run_tool("usage", self.case),
                         (0, {"status": "ok", "searches": 0, "web_reads": 0, "sources": 0, "limits": None}))

    def test_usage_shows_the_limits_of_the_brief(self):
        (self.case / "brief.md").write_text("# Бриф\n\n## Бюджет исследования\n\n"
                                            "8 поисковых запросов, 20 чтений страниц, 12 источников\n", encoding="utf-8")
        self.assertEqual(self.run_tool("usage", self.case)[1]["limits"],
                         {"searches": 8, "web_reads": 20, "sources": 12})

    def test_usage_counts_every_attempt(self):
        self.journal("search", "web_read", "search", "web_read", "web_read", "model", "file_read", "human_input")
        path = self.case / "state" / "actions.jsonl"
        end = {"format_version": 1, "id": "ACT-0001", "phase": "end", "outcome": "failed", "result_refs": [],
               "cost": {"basis": "unknown"}, "at": "2026-11-06T10:00:00+03:00"}
        with path.open("ab") as stream:
            stream.write((json.dumps(end) + "\n").encode("utf-8"))
        _, result = self.run_tool("usage", self.case)
        self.assertEqual((result["searches"], result["web_reads"]), (2, 3))

    def test_usage_counts_active_sources(self):
        self.sources("active", "withdrawn", "active")
        self.assertEqual(self.run_tool("usage", self.case)[1]["sources"], 2)

    def test_usage_ignores_torn_tail(self):
        self.journal("search", tail=b'{"id": "ACT-0002", "phase": "sta')
        self.assertEqual(self.run_tool("usage", self.case)[1]["searches"], 1)

    def test_usage_refuses_corrupt_middle(self):
        (self.case / "state").mkdir()
        (self.case / "state" / "actions.jsonl").write_bytes(b'{"id": "x"\n{"phase": "start"}\n')
        code, result = self.run_tool("usage", self.case)
        self.assertEqual((code, result["refused"]), (1, "journal_corrupt"))

    def test_usage_refuses_unreadable_sources(self):
        (self.case / "evidence").mkdir()
        (self.case / "evidence" / "sources.json").write_text("not json", encoding="utf-8")
        code, result = self.run_tool("usage", self.case)
        self.assertEqual((code, result["refused"]), (1, "sources_unreadable"))

    def test_usage_writes_nothing(self):
        self.journal("search")
        before = tree_hash(self.case)
        self.run_tool("usage", self.case)
        self.assertEqual(tree_hash(self.case), before)

    def test_missing_folder_is_usage_error(self):
        code, result = self.run_tool("usage", self.tmp / "nope")
        self.assertEqual((code, result["status"]), (2, "usage_error"))


if __name__ == "__main__":
    unittest.main()
