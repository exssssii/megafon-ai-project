"""Create a case folder and report how much of the research budget is used.

Commands (run from the repository root):
  new --from <file>      create projects/<case_id>/ with case.json
  usage <case folder>    count searches, page reads and active sources against the brief (reads only)
  show <case folder> <ID>... [--from <char>]  print registry records by number, one JSON per line;
                         a record longer than the output limit goes alone in parts, `next` gives --from (reads only)
  list <case folder> <registry>     one short line per record: number, name, value, status (reads only)

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

from material_extract import load_limits
from read_material import DATA_NOTICE

from checkpoint import EXIT_INTERNAL, EXIT_OK, EXIT_REFUSED, EXIT_USAGE, Parser, Refused, UsageError
from checkpoint import budget_used, case_version, emit, now, open_case, read_journal, research_budget, web_sources_used
from validate_project import parse_json

PROJECTS = Path(__file__).resolve().parent.parent / "projects"
CASE_ID_RE = re.compile(r"[a-z0-9]+(-[a-z0-9]+)*")
REQUEST_KEYS = {"case_id", "title"}
FORMAT_VERSION = 1
MAX_CASE_ID = 64
# registry name -> (ID prefix, file in the case); the names are those of the case folder contract
REGISTRIES = {
    "sources": ("SRC", "evidence/sources.json"),
    "claims": ("CLM", "evidence/claims.json"),
    "calculations": ("CALC", "evidence/calculations.json"),
    "questions": ("Q", "research/questions.json"),
    "options": ("OPT", "decisions/options.json"),
    "decisions": ("DEC", "decisions/decisions.json"),
}
RECORD_ID = re.compile(r"(SRC|CLM|CALC|Q|OPT|DEC)-[0-9]{4,}")
NAME_CHARS = 120


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
        return web_sources_used(parse_json(path.read_bytes())["records"])
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        raise Refused("sources_unreadable", "реестр источников не читается") from None


def cmd_usage(args) -> int:
    root = open_case(args.case)
    case_version(root)
    lines = read_journal(root)
    emit({"status": "ok",
          "searches": budget_used(lines, "search"),
          "web_reads": budget_used(lines, "web_read"),
          "sources": count_sources(root),
          "limits": research_budget(root)})
    return EXIT_OK


def read_registry(root: Path, name: str) -> list[dict]:
    path = root / REGISTRIES[name][1]
    if not path.exists():
        return []
    try:
        records = parse_json(path.read_bytes())["records"]
        if not isinstance(records, list) or not all(isinstance(r, dict) and isinstance(r.get("id"), str)
                                                    for r in records):
            raise ValueError
        return records
    except (OSError, ValueError, KeyError, TypeError):
        raise Refused("registry_unreadable", "реестр не читается", registry=name) from None


def short(text, limit: int = NAME_CHARS):
    if not isinstance(text, str):
        return text
    text = " ".join(text.split())
    return text if len(text) <= limit else text[:limit - 1] + "…"


def summary(name: str, record: dict) -> dict:
    """The short line of `list`: number, name, value with unit (calculations), status."""
    row = {"id": record["id"]}
    if name == "sources":
        row |= {"name": short(record.get("title")), "type": record.get("type"), "status": record.get("record_status")}
    elif name == "claims":
        row |= {"name": short(record.get("text")), "kind": record.get("kind"), "status": record.get("status")}
    elif name == "calculations":
        method, result = record.get("method"), record.get("result")
        method = method if isinstance(method, dict) else {}
        result = result if isinstance(result, dict) else {}
        row |= {"name": short(method.get("description")), "value": result.get("value"), "unit": result.get("unit"),
                "status": record.get("record_status"), "result_status": result.get("status")}
    elif name == "questions":
        withdrawn = record.get("record_status") == "withdrawn"
        row |= {"name": short(record.get("question")), "status": "withdrawn" if withdrawn else record.get("status")}
    elif name == "options":
        row |= {"name": short(record.get("title")), "status": record.get("record_status")}
    else:
        row |= {"name": short(record.get("topic")), "status": record.get("status")}
    return row


def print_data(header: dict, lines: list[str]) -> None:
    emit(header)
    if lines:
        print(DATA_NOTICE)
        print("\n".join(lines))


def cmd_show(args) -> int:
    root = open_case(args.case)
    case_version(root)
    for number in args.ids:
        if not RECORD_ID.fullmatch(number):
            raise Refused("bad_id", "номер вида SRC-0001, CLM-0001, CALC-0001, Q-0001, OPT-0001 или DEC-0001")
    wanted = list(dict.fromkeys(args.ids))
    found: dict[str, dict] = {}
    for name, (prefix, _) in REGISTRIES.items():
        if any(number.startswith(prefix + "-") for number in wanted):
            found |= {record["id"]: record for record in read_registry(root, name)}
    missing = [number for number in wanted if number not in found]
    if missing:
        raise Refused("records_not_found", "записей с такими номерами нет", missing=missing)
    budget = load_limits()["max_output_chars"]
    if args.start < 0:
        raise UsageError("--from не меньше 0")
    if args.start and len(wanted) != 1:
        raise UsageError("--from применяется к одной записи")
    if len(wanted) == 1:
        line = json.dumps(found[wanted[0]], ensure_ascii=False)
        if args.start > 0 and args.start >= len(line):
            raise Refused("offset_out_of_range", "смещение за концом записи", length=len(line))
        if args.start or len(line) + 1 > budget:
            part = line[args.start:args.start + budget - 1]
            end = args.start + len(part)
            print_data({"status": "ok", "command": "show", "shown": 1, "length": len(line), "part": f"{args.start}-{end}",
                        "next": f"--from {end}" if end < len(line) else None}, [part])
            return EXIT_OK
    lines, used, not_shown, too_long = [], 0, [], []
    for number in wanted:
        line = json.dumps(found[number], ensure_ascii=False)
        if used + len(line) + 1 > budget:
            not_shown.append(number)
            if len(line) + 1 > budget:
                too_long.append(number)
            continue
        lines.append(line)
        used += len(line) + 1
    header = {"status": "ok", "command": "show", "shown": len(lines)}
    if not_shown:
        header["not_shown"] = not_shown
    if too_long:
        header["too_long"] = too_long
    print_data(header, lines)
    return EXIT_OK


def cmd_list(args) -> int:
    root = open_case(args.case)
    case_version(root)
    if args.registry not in REGISTRIES:
        raise Refused("registry_unknown", "такого реестра нет", registries=sorted(REGISTRIES))
    if args.start < 1:
        raise UsageError("--from начинается с 1")
    records = read_registry(root, args.registry)
    budget = load_limits()["max_output_chars"]
    lines, used = [], 0
    for record in records[args.start - 1:]:
        line = json.dumps(summary(args.registry, record), ensure_ascii=False)
        if lines and used + len(line) + 1 > budget:
            break
        lines.append(line)
        used += len(line) + 1
    resume = args.start + len(lines)
    print_data({"status": "ok", "command": "list", "registry": args.registry, "total": len(records),
                "shown": len(lines), "next": f"--from {resume}" if resume <= len(records) else None}, lines)
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
    show = commands.add_parser("show")
    show.add_argument("case", type=Path)
    show.add_argument("ids", nargs="+")
    show.add_argument("--from", dest="start", type=int, default=0)
    show.set_defaults(run=cmd_show)
    listing = commands.add_parser("list")
    listing.add_argument("case", type=Path)
    listing.add_argument("registry")
    listing.add_argument("--from", dest="start", type=int, default=1)
    listing.set_defaults(run=cmd_list)
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
