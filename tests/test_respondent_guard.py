import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import unittest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))

import respondent_guard as guard  # noqa: E402

WRAPPER = ROOT / ".claude" / "agents" / "respondent.md"


def read(path, cwd=None):
    event = {"tool_name": "Read", "tool_input": {"file_path": str(path)}}
    if cwd is not None:
        event["cwd"] = str(cwd)
    return event


class Allowed(unittest.TestCase):
    def test_role_file_is_readable(self):
        self.assertTrue(guard.allowed(read(guard.ROLE)))

    def test_relative_path_is_resolved_against_cwd(self):
        self.assertTrue(guard.allowed(read("agent/roles/respondent.md", cwd=ROOT)))
        self.assertFalse(guard.allowed(read("respondent.md", cwd=ROOT)))

    @unittest.skipUnless(os.name == "nt", "Git Bash paths exist only on Windows")
    def test_git_bash_spelling_of_the_role_file(self):
        drive, rest = guard.ROLE.drive, guard.ROLE.as_posix()[len(guard.ROLE.drive):]
        self.assertTrue(guard.allowed(read(f"/{drive[0].lower()}{rest}")))
        self.assertFalse(guard.allowed(read(f"/{drive[0].lower()}{ROOT.as_posix()[2:]}/README.md")))

    def test_case_folder_and_other_files_are_denied(self):
        for path in (ROOT / "projects" / "demo" / "evidence" / "claims.json",
                     ROOT / "agent" / "roles" / ".." / "workflows" / "interviews.md",
                     ROOT / "README.md"):
            self.assertFalse(guard.allowed(read(path)), path)

    def test_other_tools_are_denied(self):
        for tool in ("Grep", "Glob", "Bash", "WebFetch"):
            self.assertFalse(guard.allowed({"tool_name": tool, "tool_input": {"file_path": str(guard.ROLE)}}))


class Hook(unittest.TestCase):
    def run_hook(self, stdin: bytes):
        result = subprocess.run([sys.executable, str(ROOT / "tools" / "respondent_guard.py")], input=stdin,
                                capture_output=True, timeout=30)
        self.assertEqual(result.returncode, 0)
        return result.stdout.decode("utf-8")

    def decision(self, stdin: bytes):
        output = self.run_hook(stdin)
        return json.loads(output)["hookSpecificOutput"]["permissionDecision"] if output else "allow"

    def test_role_file_passes_silently(self):
        self.assertEqual(self.run_hook(json.dumps(read(guard.ROLE)).encode("utf-8")), "")

    def test_other_file_is_denied(self):
        self.assertEqual(self.decision(json.dumps(read(ROOT / "README.md")).encode("utf-8")), "deny")

    def test_broken_event_is_denied(self):
        for stdin in (b"not json", b"{}", json.dumps({"tool_name": "Read"}).encode("utf-8")):
            self.assertEqual(self.decision(stdin), "deny", stdin)


class Wrapper(unittest.TestCase):
    def test_wrapper_runs_the_guard_on_every_tool(self):
        text = WRAPPER.read_text(encoding="utf-8")
        header = text.split("---")[1]
        self.assertIn("tools: Read\n", header)
        self.assertIn("PreToolUse:", header)
        self.assertIn('matcher: "*"', header)
        self.assertIn("tools/respondent_guard.py", header)

    @unittest.skipUnless(shutil.which("bash"), "bash is not available")
    def test_hook_command_denies_without_a_venv(self):
        command = next(line.split("command: ", 1)[1] for line in WRAPPER.read_text(encoding="utf-8").splitlines()
                       if "command: " in line)
        command = json.loads(command)  # the YAML scalar is a double-quoted string with JSON-compatible escapes
        empty = ROOT / "tests" / "fixtures"
        # The full path: on Windows a bare "bash" resolves to the WSL launcher in System32.
        result = subprocess.run([shutil.which("bash"), "-c", command], input=json.dumps(read(guard.ROLE)).encode("utf-8"),
                                capture_output=True, timeout=30, env={**os.environ, "CLAUDE_PROJECT_DIR": str(empty)})
        self.assertEqual(result.returncode, 2)


if __name__ == "__main__":
    unittest.main()
