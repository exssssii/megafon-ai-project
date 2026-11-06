"""Create a case folder and report how much of the research budget is used.

Commands (run from the repository root):
  new --from <file>      create projects/<case_id>/ with case.json
  usage <case folder>    count searches, page reads and active sources (reads only)

The request for `new` is a JSON file {"case_id", "title"} in UTF-8: a title is
case text and never goes through the shell. The first output line is a JSON
report without absolute paths or exception text. Exit codes: 0 done, 1 refused
on the merits (nothing changed), 2 wrong invocation, 3 internal failure. The
procedure for the agent is in agent/workflows/run-case.md.
"""
import argparse
import json
import os
import re
import sys
from pathlib import Path

from checkpoint import EXIT_INTERNAL, EXIT_OK, EXIT_REFUSED, EXIT_USAGE, Parser, Refused, UsageError
from checkpoint import case_version, emit, now, open_case, read_journal
from validate_project import parse_json

PROJECTS = Path(__file__).resolve().parent.parent / "projects"
CASE_ID_RE = re.compile(r"[a-z0-9]+(-[a-z0-9]+)*")
REQUEST_KEYS = {"case_id", "title"}
FORMAT_VERSION = 1
MAX_CASE_ID = 64


def read_request(path: Path) -> dict:
    try:
        document = parse_json(path.read_bytes())
    except (OSError, ValueError):
        raise Refused("request_unreadable", "файл запроса не найден или не JSON") from None
    if not isinstance(document, dict) or set(document) != REQUEST_KEYS:
        raise Refused("bad_request", "нужен объект с полями case_id и title")
    return document


def cmd_new(args) -> int:
    request = read_request(args.request)
    case_id, title = request["case_id"], request["title"]
    if not isinstance(case_id, str) or len(case_id) > MAX_CASE_ID or not CASE_ID_RE.fullmatch(case_id):
        raise Refused("case_id_invalid", "case_id: строчные латинские буквы, цифры и дефис")
    if not isinstance(title, str) or not title.strip():
        raise Refused("bad_request", "title должен быть непустой строкой")
    PROJECTS.mkdir(parents=True, exist_ok=True)
    folder = PROJECTS / case_id
    try:
        folder.mkdir()
    except FileExistsError:
        raise Refused("case_exists", "папка с таким case_id уже есть", case_id=case_id) from None
    document = {"format_version": FORMAT_VERSION, "case_id": case_id, "title": title, "created_at": now()}
    draft = folder / ".case.json.tmp"
    try:
        draft.write_text(json.dumps(document, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        os.replace(draft, folder / "case.json")
    except BaseException:
        draft.unlink(missing_ok=True)
        folder.rmdir()
        raise
    emit({"status": "ok", "case_id": case_id, "path": f"projects/{case_id}"})
    return EXIT_OK


def count_sources(root: Path) -> int:
    path = root / "evidence" / "sources.json"
    if not path.exists():
        return 0
    try:
        records = parse_json(path.read_bytes())["records"]
        return sum(record["record_status"] == "active" for record in records)
    except (OSError, ValueError, KeyError, TypeError):
        raise Refused("sources_unreadable", "реестр источников не читается") from None


def cmd_usage(args) -> int:
    root = open_case(args.case)
    case_version(root)
    starts = [line for line in read_journal(root) if isinstance(line, dict) and line.get("phase") == "start"]
    emit({"status": "ok",
          "searches": sum(line.get("kind") == "search" for line in starts),
          "web_reads": sum(line.get("kind") == "web_read" for line in starts),
          "sources": count_sources(root)})
    return EXIT_OK


def build_parser() -> Parser:
    parser = Parser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    commands = parser.add_subparsers(dest="command", required=True, parser_class=Parser)
    new = commands.add_parser("new")
    new.add_argument("--from", dest="request", type=Path, required=True)
    new.set_defaults(run=cmd_new)
    usage = commands.add_parser("usage")
    usage.add_argument("case", type=Path)
    usage.set_defaults(run=cmd_usage)
    return parser


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    try:
        args = build_parser().parse_args(argv)
        return args.run(args)
    except UsageError as error:
        emit({"status": "usage_error", "message": str(error)})
        return EXIT_USAGE
    except Refused as error:
        emit({"status": "refused", "refused": error.reason, "message": error.message} | error.details)
        return EXIT_REFUSED
    except Exception:  # noqa: BLE001 - the text of an exception could quote case data
        emit({"status": "internal_error"})
        return EXIT_INTERNAL


if __name__ == "__main__":
    sys.exit(main())
