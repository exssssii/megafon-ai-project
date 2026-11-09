"""Submission package: what a clean launch needs is in it, nothing local or secret is."""
import importlib.util
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("package", ROOT / "scripts" / "package.py")
package = importlib.util.module_from_spec(spec)
sys.modules["package"] = package
spec.loader.exec_module(package)


class PackageCheck(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.archive = Path(tmp.name) / "p.zip"

    def check(self, extra=None, drop=()):
        files = {rel: "x\n" for rel in package.REQUIRED if rel not in drop}
        files.update(extra or {})
        with zipfile.ZipFile(self.archive, "w") as archive:
            for rel, text in files.items():
                archive.writestr(f"{package.PREFIX}/{rel}" if not rel.startswith("/") else rel[1:], text)
        return sorted({(p["code"], p.get("path")) for p in package.inspect(self.archive, None)["problems"]})

    def test_a_package_of_the_required_files_is_clean(self):
        self.assertEqual(self.check(), [])

    def test_a_missing_required_file_is_named(self):
        self.assertEqual(self.check(drop=("uv.lock",)), [("missing", "uv.lock")])

    def test_local_state_and_user_cases_are_refused(self):
        found = self.check({".work/STATE.md": "", ".venv/pyvenv.cfg": "", "tools/__pycache__/a.pyc": "",
                            ".claude/settings.local.json": "{}", "projects/acme/brief.md": "", "projects/.gitkeep": "",
                            "docs/.env": "A=1", "outside.txt": ""})
        self.assertEqual(found, [("local_file", ".claude/settings.local.json"), ("local_file", ".venv/pyvenv.cfg"),
                                 ("local_file", ".work/STATE.md"), ("local_file", "docs/.env"),
                                 ("local_file", "tools/__pycache__/a.pyc"), ("user_case", "projects/acme/brief.md")])

    def test_a_file_outside_the_package_folder_is_named(self):
        self.assertEqual(self.check({"/stray.txt": ""}), [("outside_prefix", "stray.txt")])

    def test_secrets_and_home_paths_are_found_but_placeholders_are_not(self):
        found = self.check({"a.md": "key sk-ant-" + "a" * 30, "b.md": "token ghp_" + "b" * 36,
                            # Assembled here, or the package check finds this very file.
                            "c.md": "run C:\\" + "Users\\ivanov\\work", "d.md": "cd /" + "home/petr/x",
                            "e.md": r"C:\Users\<name>\x, $env:USERPROFILE, /home/user/run.py, C:\Users\%USERNAME%"})
        self.assertEqual(found, [("home_path", "c.md"), ("home_path", "d.md"), ("secret_like", "a.md"),
                                 ("secret_like", "b.md")])


if __name__ == "__main__":
    unittest.main()
