import json
import os
from pathlib import Path
import subprocess
import sys
import unittest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))
sys.path.insert(0, str(ROOT / "tests"))

import reviewer_guard as guard  # noqa: E402
from git_bash import git_bash  # noqa: E402

WRAPPER = ROOT / ".claude" / "agents" / "reviewer.md"
CODEX = ROOT / ".codex" / "agents" / "reviewer.toml"
CASE = ROOT / "projects" / "demo"


def read(path, cwd=None):
    event = {"tool_name": "Read", "tool_input": {"file_path": str(path)}}
    if cwd is not None:
        event["cwd"] = str(cwd)
    return event


def search(tool, path=None, pattern="*.md", glob=None):
    arguments = {"pattern": pattern}
    if path is not None:
        arguments["path"] = str(path)
    if glob is not None:
        arguments["glob"] = glob
    return {"tool_name": tool, "tool_input": arguments}


class Allowed(unittest.TestCase):
    def test_role_and_case_files_are_readable(self):
        for path in (guard.ROLE, CASE / "brief.md", CASE / "outputs" / "product-strategy.md",
                     CASE / "evidence" / "claims.json", CASE / "sources" / "extracted" / "SRC-0001.jsonl"):
            self.assertTrue(guard.allowed(read(path)), path)

    def test_relative_paths_are_resolved_against_cwd(self):
        self.assertTrue(guard.allowed(read("projects/demo/outputs/validation-plan.md", cwd=ROOT)))
        self.assertTrue(guard.allowed(read("agent/roles/reviewer.md", cwd=ROOT)))
        self.assertFalse(guard.allowed(read("reviewer.md", cwd=ROOT)))

    def test_state_of_the_case_is_denied(self):
        for path in (CASE / "state" / "actions.jsonl", CASE / "state" / "checkpoints" / "CKP-0001.json",
                     CASE / "outputs" / ".." / "state" / "reviews" / "review-0001.md"):
            self.assertFalse(guard.allowed(read(path)), path)

    def test_accounting_summary_is_denied(self):
        self.assertFalse(guard.allowed(read(CASE / "outputs" / "accounting.md")))
        self.assertFalse(guard.allowed(read(CASE / "outputs" / "ACCOUNTING.md")))

    def test_files_outside_the_cases_are_denied(self):
        for path in (ROOT / "README.md", ROOT / "agent" / "roles" / "respondent.md",
                     ROOT / "agent" / "workflows" / "strategies.md", ROOT / "projects",
                     CASE / ".." / ".." / "tools" / "gate.py", ROOT / ".env"):
            self.assertFalse(guard.allowed(read(path)), path)

    @unittest.skipUnless(os.name == "nt", "Git Bash paths exist only on Windows")
    def test_git_bash_spelling(self):
        drive, rest = ROOT.drive, ROOT.as_posix()[len(ROOT.drive):]
        bash_root = f"/{drive[0].lower()}{rest}"
        self.assertTrue(guard.allowed(read(f"{bash_root}/projects/demo/brief.md")))
        self.assertFalse(guard.allowed(read(f"{bash_root}/projects/demo/state/actions.jsonl")))

    def test_search_inside_a_case_folder_is_allowed(self):
        for tool in ("Glob", "Grep"):
            self.assertTrue(guard.allowed(search(tool, CASE / "outputs")), tool)
            self.assertTrue(guard.allowed(search(tool, CASE / "sources" / "extracted")), tool)
        self.assertTrue(guard.allowed(search("Grep", CASE / "brief.md", pattern="Бюджет")))

    def test_search_without_a_path_or_too_wide_is_denied(self):
        for tool in ("Glob", "Grep"):
            for path in (None, ROOT, ROOT / "projects", CASE, CASE / "state", ROOT / "agent"):
                self.assertFalse(guard.allowed(search(tool, path)), (tool, path))

    def test_search_patterns_cannot_leave_the_folder(self):
        self.assertFalse(guard.allowed(search("Glob", CASE / "outputs", pattern="../state/*")))
        self.assertFalse(guard.allowed(search("Glob", CASE / "outputs", pattern=str(ROOT / "*.md"))))
        self.assertFalse(guard.allowed(search("Glob", CASE / "outputs", pattern="/etc/*")))
        self.assertFalse(guard.allowed(search("Grep", CASE / "outputs", pattern="x", glob="../state/*")))

    def test_other_tools_are_denied(self):
        for tool in ("Bash", "Write", "Edit", "WebFetch", "WebSearch", "Agent"):
            event = {"tool_name": tool, "tool_input": {"file_path": str(CASE / "brief.md"), "path": str(CASE / "outputs")}}
            self.assertFalse(guard.allowed(event), tool)


class Hook(unittest.TestCase):
    def run_hook(self, stdin: bytes):
        result = subprocess.run([sys.executable, str(ROOT / "tools" / "reviewer_guard.py")], input=stdin,
                                capture_output=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout.decode("utf-8")

    def decision(self, stdin: bytes):
        output = self.run_hook(stdin)
        return json.loads(output)["hookSpecificOutput"]["permissionDecision"] if output else "allow"

    def test_case_file_passes_silently(self):
        self.assertEqual(self.run_hook(json.dumps(read(CASE / "brief.md")).encode("utf-8")), "")

    def test_state_is_denied(self):
        self.assertEqual(self.decision(json.dumps(read(CASE / "state" / "actions.jsonl")).encode("utf-8")), "deny")

    def test_broken_event_is_denied(self):
        for stdin in (b"not json", b"{}", json.dumps({"tool_name": "Read"}).encode("utf-8"),
                      json.dumps({"tool_name": "Glob", "tool_input": {"path": 5}}).encode("utf-8")):
            self.assertEqual(self.decision(stdin), "deny", stdin)


class Wrappers(unittest.TestCase):
    def test_claude_wrapper_runs_the_guard_on_every_tool(self):
        header = WRAPPER.read_text(encoding="utf-8").split("---")[1]
        self.assertIn("name: reviewer\n", header)
        self.assertIn("tools: Read, Glob, Grep\n", header)
        self.assertIn('matcher: "*"', header)
        self.assertIn("tools/reviewer_guard.py", header)

    def test_wrappers_point_to_the_role_and_do_not_copy_it(self):
        role = guard.ROLE.read_text(encoding="utf-8")
        for wrapper in (WRAPPER, CODEX):
            text = wrapper.read_text(encoding="utf-8")
            self.assertIn("agent/roles/reviewer.md", text, wrapper)
            self.assertIn("Роль не загружена", text, wrapper)
            for line in role.splitlines():
                if len(line) > 60:
                    self.assertNotIn(line, text, wrapper)

    @unittest.skipUnless(git_bash(), "Git Bash is not available")
    def test_hook_command_denies_without_a_venv(self):
        command = next(line.split("command: ", 1)[1] for line in WRAPPER.read_text(encoding="utf-8").splitlines()
                       if "command: " in line)
        command = json.loads(command)
        empty = ROOT / "tests" / "fixtures"
        result = subprocess.run([git_bash(), "-c", command], input=json.dumps(read(guard.ROLE)).encode("utf-8"),
                                capture_output=True, timeout=30, env={**os.environ, "CLAUDE_PROJECT_DIR": str(empty)})
        self.assertEqual(result.returncode, 2)


if __name__ == "__main__":
    unittest.main()
