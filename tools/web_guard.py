"""Claude Code hook: a web search or page read needs an open action in the journal.

Reads the hook event from standard input. Inside an unfinished case a call without a
started action of the same kind is denied, so the limit of the brief also holds for
calls the agent forgot to record. The hook only reads. It lets the call through when
no case was touched lately, the case is finished, or the journal cannot be read: a broken
guard must not stop the work, and `action start` still counts what is recorded.
`CASE_WEB_GUARD=off` turns it off for development work in a clone with cases.
"""
import json
import os
from pathlib import Path
import sys
import time

from checkpoint import CKP_NUMBER_RE, Refused, has_open_action, read_checkpoint, read_journal

PROJECTS = Path(__file__).resolve().parent.parent / "projects"
KINDS = {"WebSearch": "search", "WebFetch": "web_read"}
JOURNAL = Path("state") / "actions.jsonl"
ACTIVE_SECONDS = 12 * 60 * 60


def active_cases(projects: Path) -> list[Path]:
    """Unfinished cases touched lately. There can be several: a parallel chat must not stop this one."""
    fresh = [path.parents[1] for path in projects.glob(f"*/{JOURNAL.as_posix()}")
             if path.is_file() and time.time() - path.stat().st_mtime < ACTIVE_SECONDS]
    return [root for root in fresh if not is_finished(root)]


def is_finished(root: Path) -> bool:
    names = [path.name for path in (root / "state" / "checkpoints").glob("CKP-*.json")]
    numbers = sorted(match.group(0) for match in map(CKP_NUMBER_RE.search, names) if match)
    return bool(numbers) and read_checkpoint(root, numbers[-1]).get("stage") == "done"


def denied(event: dict, projects: Path) -> str | None:
    tool = event.get("tool_name")
    if tool not in KINDS or os.environ.get("CASE_WEB_GUARD") == "off":
        return None
    roots = active_cases(projects)
    if not roots or any(has_open_action(read_journal(root), KINDS[tool]) for root in roots):
        return None
    folder = roots[0].relative_to(projects.parent).as_posix() if len(roots) == 1 else "projects/<case-id>"
    return (f"Сначала запиши шаг: tools/checkpoint.py action start {folder} --kind {KINDS[tool]} --tool {tool} "
            "--environment claude_code, затем повтори вызов. При отказе budget_exhausted не вызывай инструмент, "
            "остальные отказы разбирай по checkpoint.md и checkpoint-recovery.md.")


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    try:
        reason = denied(json.loads(sys.stdin.buffer.read().decode("utf-8-sig")), PROJECTS)
    except (Refused, OSError, ValueError, KeyError, TypeError, AttributeError):
        return 0
    if reason:
        print(json.dumps({"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny",
                                                 "permissionDecisionReason": reason}}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
