"""Claude Code hook of the `reviewer` subagent: it may read the case folders and its role, nothing else.

The reviewer checks the documents of a case against their bases without the reasoning of the agent
that wrote them. So it reads under `projects/` but never a `state/` folder of a case, where the
checkpoint notes and the action journal keep that reasoning, and it gets no shell, writes or web.
`Glob` and `Grep` must name a folder or file inside a case: without a path they search the
repository root, and on a case root they would reach `state/`. `Read` of the accounting summary is
denied too; a `Grep` over `outputs/` still reaches it, which the role rules forbid. The guard lets in
every case under `projects/`, not only the one of the task: a run has one case. Fails closed, like
the respondent guard.
"""
import json
import os
from pathlib import Path
import sys

from respondent_guard import native, same_file

ROOT = Path(__file__).resolve().parent.parent
ROLE = ROOT / "agent" / "roles" / "reviewer.md"
PROJECTS = ROOT / "projects"
SEARCH_TOOLS = {"Glob", "Grep"}
REASON = ("Проверяющему доступны только agent/roles/reviewer.md и папки кейса в projects/ без state/: "
          "чтение, Glob и Grep с путём внутри кейса. Запись, команды и сеть закрыты.")


def resolve(raw: str, cwd: str | None) -> Path:
    path = Path(native(raw))
    if not path.is_absolute():
        path = Path(cwd or ROOT) / path
    return Path(os.path.realpath(path))


def in_case(path: Path) -> bool:
    """A file or folder inside some projects/<case>/, but not the case root itself and not its state/."""
    try:
        parts = path.relative_to(Path(os.path.realpath(PROJECTS))).parts
    except ValueError:
        return False
    return len(parts) >= 2 and os.path.normcase(parts[1]) != os.path.normcase("state")


def readable(path: Path) -> bool:
    """A case file the reviewer may open: the accounting summary keeps retry reasons and notes on human help."""
    return in_case(path) and path.name.casefold() != "accounting.md"


def allowed(event: dict) -> bool:
    tool, arguments, cwd = event.get("tool_name"), event["tool_input"], event.get("cwd")
    if tool == "Read":
        path = resolve(arguments["file_path"], cwd)
        return same_file(path, ROLE) or readable(path)
    if tool in SEARCH_TOOLS:
        if not arguments.get("path"):
            return False
        for pattern in (arguments.get("pattern") if tool == "Glob" else None, arguments.get("glob")):
            if pattern and (".." in pattern or Path(native(pattern)).is_absolute() or pattern.startswith(("/", "\\"))):
                return False
        return in_case(resolve(arguments["path"], cwd))
    return False


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    try:
        if allowed(json.loads(sys.stdin.buffer.read().decode("utf-8-sig"))):
            return 0
    except Exception:  # noqa: BLE001 - any failure must deny, never let the call through
        pass
    print(json.dumps({"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny",
                                             "permissionDecisionReason": REASON}}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
