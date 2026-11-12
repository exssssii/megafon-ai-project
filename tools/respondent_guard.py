"""Claude Code hook of the `respondent` subagent: it may read only the rules of its role.

The persona must not see the research it is interviewed for, so the subagent definition
gives it the `Read` tool for one file and this hook denies every other call. Unlike the
web guard it fails closed: an unreadable event or any error is a denial, because a broken
guard would silently turn isolation into a promise in the text.
"""
import json
import os
from pathlib import Path
import re
import sys

ROOT = Path(__file__).resolve().parent.parent
ROLE = ROOT / "agent" / "roles" / "respondent.md"
REASON = ("Собеседнику доступен только файл agent/roles/respondent.md. Карточка персонажа и вопросы "
          "переданы в задании, больше ничего не читай.")


def same_file(left: Path, right: Path) -> bool:
    return os.path.normcase(os.path.realpath(left)) == os.path.normcase(os.path.realpath(right))


def native(raw: str) -> str:
    """Git Bash spelling of a Windows path, /c/Users/..., as C:/Users/...; other paths unchanged."""
    match = re.fullmatch(r"/([A-Za-z])(/.*)?", raw) if os.name == "nt" else None
    return f"{match.group(1)}:{match.group(2) or '/'}" if match else raw


def allowed(event: dict) -> bool:
    if event.get("tool_name") != "Read":
        return False
    path = Path(native(event["tool_input"]["file_path"]))
    if not path.is_absolute():
        path = Path(event.get("cwd") or ROOT) / path
    return same_file(path, ROLE)


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
