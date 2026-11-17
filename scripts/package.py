"""Submission package: a zip of one commit and the check of what it holds (B14).

    python scripts/package.py build <out.zip> [--rev HEAD]
    python scripts/package.py check <zip> [--rev HEAD] [--smoke]

build takes the commit from Git, so local files that are not committed (.work/, .venv/,
projects/, personal settings) never get in. check reads the zip itself: the files a clean
launch needs are there, nothing local or secret is, and with --rev the file list equals the
commit, which makes a live run from a clean clone of that commit a run of this package.
--smoke unpacks it into a temporary folder and runs the first launch without a model:
uv sync --locked, the validator on the sample case and the reading of a sample file.

The first line of output is JSON. Exit code 0 - fine, 1 - problems in the package, 2 - bad call.
"""
import argparse
import json
import re
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path, PurePosixPath

PREFIX = "product-strategy-agent"
REQUIRED = ("README.md", "START.md", "AGENTS.md", "CLAUDE.md", "pyproject.toml", "uv.lock", ".python-version",
            "agent/instructions/product-mode.md", "agent/contracts/README.md", "tools/case.py", "tools/checkpoint.py",
            "tools/validate_project.py", ".claude/settings.json",
            ".claude/skills/resuming-case/SKILL.md", ".claude/skills/running-interviews/SKILL.md", ".claude/skills/revising-on-new-data/SKILL.md",
            ".agents/skills/resuming-case/SKILL.md", ".agents/skills/running-interviews/SKILL.md", ".agents/skills/revising-on-new-data/SKILL.md",
            "agent/roles/reviewer.md", ".claude/agents/reviewer.md", ".codex/agents/reviewer.toml", "tools/reviewer_guard.py",
            "agent/workflows/interview-routing.md", "tools/role_check.py")
# Local state of a clone: work notes, environments, cases of a user, caches and personal settings.
FORBIDDEN_PARTS = (".work", ".venv", "venv", "__pycache__", ".superpowers", ".pytest_cache", "node_modules", ".git")
FORBIDDEN_NAMES = re.compile(r"(?i)^(?:\.env(?:\..*)?|settings\.local\.json|auth\.json|credentials(?:\..*)?|"
                             r".*\.(?:pem|key|p12|pfx)|id_(?:rsa|ed25519|ecdsa)(?:\.pub)?|\.netrc|\.git-credentials)$")
SECRETS = re.compile(rb"sk-ant-[A-Za-z0-9_-]{20,}|sk-(?:proj-)?[A-Za-z0-9]{32,}|gh[pousr]_[A-Za-z0-9]{30,}"
                     rb"|github_pat_[A-Za-z0-9_]{40,}|AKIA[0-9A-Z]{16}|-----BEGIN [A-Z ]*PRIVATE KEY-----")
# An absolute path into a home folder names a person and a machine; variables and placeholders do not.
# An ellipsis in place of the name (C:/Users/...) is an example in a docstring, not a path.
PLACEHOLDER = rb"(?![$%<{.])(?!(?:user|username|name|example|me|you|public|default)(?![\w.-]))"
HOME_PATH = re.compile(rb"(?i)\b[A-Z]:[\\/]+Users[\\/]+" + PLACEHOLDER + rb"[^\\/\s\"'`]+"
                       rb"|/home/" + PLACEHOLDER + rb"[a-z_][\w.-]*|/Users/" + PLACEHOLDER + rb"[A-Za-z][\w.-]*")
SAMPLE_CASE = "agent/contracts/examples/valid/demo-case"
SAMPLE_FILE = "examples/retention-smb/extra/finance-note.docx"


def git(*args: str) -> bytes:
    return subprocess.run(["git", *args], check=True, capture_output=True).stdout


def build(out: Path, rev: str) -> dict:
    commit = git("rev-parse", "--verify", f"{rev}^{{commit}}").decode().strip()
    out.parent.mkdir(parents=True, exist_ok=True)
    git("archive", "--format=zip", f"--prefix={PREFIX}/", "-o", str(out), commit)
    return {"status": "built", "commit": commit, "path": str(out), "bytes": out.stat().st_size}


def relative(name: str) -> str | None:
    parts = PurePosixPath(name).parts
    if not parts or parts[0] != PREFIX:
        return None
    return "/".join(parts[1:])


def inspect(archive: Path, rev: str | None) -> dict:
    problems = []
    with zipfile.ZipFile(archive) as package:
        names = [n for n in package.namelist() if not n.endswith("/")]
        files = {}
        for name in names:
            rel = relative(name)
            if rel is None:
                problems.append({"code": "outside_prefix", "path": name})
                continue
            files[rel] = name
        for rel, name in sorted(files.items()):
            parts = PurePosixPath(rel).parts
            if any(part in FORBIDDEN_PARTS for part in parts) or FORBIDDEN_NAMES.match(parts[-1]):
                problems.append({"code": "local_file", "path": rel})
            if parts[0] == "projects" and parts[-1] != ".gitkeep":
                problems.append({"code": "user_case", "path": rel})
            data = package.read(name)
            if SECRETS.search(data):
                problems.append({"code": "secret_like", "path": rel})
            match = HOME_PATH.search(data)
            if match:
                problems.append({"code": "home_path", "path": rel, "text": match.group(0).decode("utf-8", "replace")})
    for rel in REQUIRED:
        if rel not in files:
            problems.append({"code": "missing", "path": rel})
    report = {"files": len(files), "problems": problems}
    if rev is not None:
        commit = git("rev-parse", "--verify", f"{rev}^{{commit}}").decode().strip()
        tracked = set(git("ls-tree", "-r", "--name-only", "-z", commit).decode().split("\0")) - {""}
        report["commit"] = commit
        report["not_in_commit"] = sorted(set(files) - tracked)
        report["missing_from_package"] = sorted(tracked - set(files))
        if report["not_in_commit"] or report["missing_from_package"]:
            problems.append({"code": "differs_from_commit"})
    return report


def smoke(archive: Path) -> dict:
    """First launch from the unpacked package, without a model: what a new user does first."""
    steps = []
    with tempfile.TemporaryDirectory() as tmp:
        with zipfile.ZipFile(archive) as package:
            package.extractall(tmp)
        root = Path(tmp) / PREFIX
        python = root / ".venv" / ("Scripts/python.exe" if sys.platform == "win32" else "bin/python")
        for label, command in (
                ("uv sync --locked", ["uv", "sync", "--locked", "--quiet"]),
                ("validate the sample case", [str(python), "tools/validate_project.py", SAMPLE_CASE]),
                ("read a sample file", [str(python), "tools/read_material.py", "inspect", SAMPLE_FILE])):
            run = subprocess.run(command, cwd=root, capture_output=True, text=True, encoding="utf-8", errors="replace")
            steps.append({"step": label, "code": run.returncode})
            if run.returncode != 0:
                steps[-1]["tail"] = (run.stdout + run.stderr).strip().splitlines()[-3:]
                break
    return {"steps": steps, "passed": len(steps) == 3 and all(s["code"] == 0 for s in steps)}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    commands = parser.add_subparsers(dest="command", required=True)
    make = commands.add_parser("build")
    make.add_argument("out", type=Path)
    make.add_argument("--rev", default="HEAD")
    check = commands.add_parser("check")
    check.add_argument("archive", type=Path)
    check.add_argument("--rev")
    check.add_argument("--smoke", action="store_true")
    args = parser.parse_args()
    try:
        if args.command == "build":
            print(json.dumps(build(args.out, args.rev), ensure_ascii=False))
            return 0
        report = inspect(args.archive, args.rev)
        if args.smoke and not report["problems"]:
            report["smoke"] = smoke(args.archive)
            if not report["smoke"]["passed"]:
                report["problems"].append({"code": "smoke_failed"})
    except (OSError, zipfile.BadZipFile, subprocess.CalledProcessError) as error:
        print(json.dumps({"status": "error", "message": str(error)}, ensure_ascii=False))
        return 2
    report["status"] = "problems" if report["problems"] else "ok"
    print(json.dumps(report, ensure_ascii=False))
    return 1 if report["problems"] else 0


if __name__ == "__main__":
    sys.exit(main())
