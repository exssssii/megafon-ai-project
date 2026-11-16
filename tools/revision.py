"""Revision of a case after new data: the log, the affected sections, the snapshot of outputs.

Commands (run from the repository root):
  affected <case> <ID> [<ID> ...]   what depends on the records, by record and by section of outputs/*.md
  snapshot <case> <N>               copy outputs/*.md except revisions.md into outputs/.history/<N>/

Both print one JSON line without the text of a material. Exit codes: 0 done, 4 refused (nothing
written), 5 internal failure. `affected` reads only. The log is `outputs/revisions.md`; its format
is in agent/contracts/README.md.
"""
import argparse
import json
import os
import re
import shutil
import sys
from datetime import date
from pathlib import Path

import evidence
from checkpoint import Parser, Refused, UsageError
from outputs_md import PREAMBLE, is_status_line, split_with_preamble
from read_material import Refused as MaterialRefused
from validate_project import BRACKET_RE, ID_RE

EXIT_OK, EXIT_REFUSED, EXIT_INTERNAL = 0, 4, 5

REVISIONS = "outputs/revisions.md"
TITLE = "# Пересмотры"
LABELS = ("Новые данные", "Изменилось", "Условия пересмотра", "Затронуто", "Не затронуто", "Выбор",
          "Прежние версии")
NO_CHANGE = "изменений нет"
NO_SNAPSHOT = "нет"

HEADING_RE = re.compile(r"## Пересмотр (\d+) — (\d{4}-\d{2}-\d{2})\s*")
BULLET_RE = re.compile(r"- (" + "|".join(LABELS) + r"):[ \t]*(.*?)\s*")
CHOICE_RE = re.compile(r"\[(DEC-[0-9]{4,})\]")
SNAPSHOT_RE = re.compile(r"outputs/\.history/([0-9]+)/")
FILE_RE = re.compile(r"[\w-]+\.md(?![\w])")
OUTPUTS = "outputs"
HISTORY = "outputs/.history"
ACCOUNTING = "outputs/accounting.md"
REAPPROVAL = {"outputs/validation-plan.md": "thresholds", "outputs/commitments.md": "commitments"}


def problem(code: str, line: int, note: str) -> dict:
    return {"code": code, "line": line, "note": note}


def ids_in_line(text: str) -> list[str]:
    found = []
    for match in BRACKET_RE.finditer(text):
        found += [part.strip() for part in match.group(1).split(",") if ID_RE.fullmatch(part.strip())]
    return found


def touched_in(text: str) -> list[str]:
    return list(dict.fromkeys(FILE_RE.findall(text) + ids_in_line(text)))


def valid_date(text: str) -> bool:
    try:
        date.fromisoformat(text)
    except ValueError:
        return False
    return True


def parse_revisions(text: str) -> tuple[list[dict], list[dict]]:
    """Entries of the log and the problems found; never raises on odd text."""
    lines = text.lstrip("﻿").splitlines()
    entries: list[dict] = []
    problems: list[dict] = []
    blocks: list[tuple[dict, dict]] = []  # an entry and its bullets: label -> (line number, value)
    bullets: dict | None = None
    title_seen, fenced = False, False
    for number, line in enumerate(lines, 1):
        if line.lstrip().startswith("```"):
            fenced = not fenced
        if fenced or not line.strip():
            continue
        if not title_seen:
            title_seen = True
            if line.strip() != TITLE:
                problems.append(problem("REVISION_FORMAT", number, f"the first line must be «{TITLE}»"))
                if not line.startswith("## "):
                    continue
            else:
                continue
        if line.startswith("## "):
            heading = HEADING_RE.fullmatch(line)
            if not heading or not valid_date(heading.group(2)):
                problems.append(problem("REVISION_FORMAT", number, "heading: «## Пересмотр <N> — <ГГГГ-ММ-ДД>»"))
                bullets = None
                continue
            entry = {"number": int(heading.group(1)), "date": heading.group(2), "line": number, "choice": None,
                     "touched": [], "snapshot": None}
            bullets = {}
            entries.append(entry)
            blocks.append((entry, bullets))
        elif bullets is not None:
            found = BULLET_RE.fullmatch(line.strip())
            if found:
                label = found.group(1)
                if label in bullets:
                    problems.append(problem("REVISION_FORMAT", number, f"«{label}» is repeated"))
                else:
                    bullets[label] = (number, found.group(2))
            else:
                # A continuation written under the seven lines would carry a choice no check reads.
                problems.append(problem("REVISION_FORMAT", number, "an entry holds only its seven lines «- <label>: …»"))
    if not title_seen:
        problems.append(problem("REVISION_FORMAT", 1, f"the first line must be «{TITLE}»"))
    for entry, bullets in blocks:
        fill_entry(entry, bullets, problems)
    for position, entry in enumerate(entries, 1):
        if entry["number"] != position:
            problems.append(problem("REVISION_NUMBER", entry["line"], f"expected number {position}"))
            break
    return entries, problems


def fill_entry(entry: dict, bullets: dict, problems: list[dict]) -> None:
    for label in LABELS:
        if label not in bullets:
            problems.append(problem("REVISION_FORMAT", entry["line"], f"no line «- {label}:»"))
    if "Затронуто" in bullets:
        entry["touched"] = touched_in(bullets["Затронуто"][1])
    if "Выбор" in bullets:
        number, value = bullets["Выбор"]
        choice = CHOICE_RE.fullmatch(value)
        if choice:
            entry["choice"] = choice.group(1)
        elif value != NO_CHANGE:
            problems.append(problem("REVISION_FORMAT", number, "«Выбор»: [DEC-…] or «изменений нет»"))
    if "Прежние версии" in bullets:
        number, value = bullets["Прежние версии"]
        folder = SNAPSHOT_RE.fullmatch(value)
        if folder and int(folder.group(1)) == entry["number"]:
            entry["snapshot"] = value
        elif value != NO_SNAPSHOT:
            problems.append(problem("REVISION_FORMAT", number,
                                    "«Прежние версии»: outputs/.history/<N>/ with the number of the entry, or «нет»"))


# --- affected --------------------------------------------------------------

def output_files(case: Path, skip: tuple = ()) -> list[Path]:
    """The files of outputs/ that count: the log and the history are not outputs. `skip`: more files by path."""
    folder = case / OUTPUTS
    if not folder.is_dir():
        return []
    return sorted(path for path in folder.glob("*.md")
                  if path.is_file() and not path.is_symlink() and f"{OUTPUTS}/{path.name}" not in (REVISIONS, *skip))


def affected_sections(case: Path, ids: set[str]) -> tuple[dict, dict]:
    """(affected, unaffected): file -> sections. A section is affected when a line cites a record in `ids`."""
    affected, unaffected = {}, {}
    # the accounting report is rewritten by the accounting step, not by a revision
    for path in output_files(case, (ACCOUNTING,)):
        try:
            lines = path.read_bytes().decode("utf-8-sig").splitlines()
        except (OSError, UnicodeDecodeError):
            continue
        hit, miss = [], []
        for name, body in split_with_preamble(lines).items():
            # the status line names the direction by design; it is not a citation of its own
            cited = {ref for _, line in body if not is_status_line(line) for ref in ids_in_line(line)}
            (hit if cited & ids else miss).append(name)
        file = f"{OUTPUTS}/{path.name}"
        if hit:
            affected[file] = hit
        if miss or not hit:
            unaffected[file] = miss
    return affected, unaffected


def find_affected(case: Path, ids: list[str]) -> dict:
    opened = evidence.open_case(case)
    graph = evidence.Graph(opened, evidence.load_claims(opened))
    for record_id in ids:
        if not evidence.IMPACT_START.fullmatch(record_id):
            raise Refused("bad_id", "affected принимает ID записи SRC, CLM, CALC, OPT, Q или DEC")
        if record_id not in graph.records:
            raise Refused("unknown_id", "запись не найдена")
    groups: dict[str, set] = {}
    inactive: set[str] = set()
    for record_id in ids:
        found = graph.dependents(record_id)
        for group, members in found["dependents"].items():
            if group != "outputs":
                groups.setdefault(group, set()).update(members)
        inactive.update(found.get("inactive", []))
    records = {group: sorted(members) for group, members in groups.items()}
    cited = set(ids) | inactive | {member for members in records.values() for member in members}
    affected, unaffected = affected_sections(case, cited)
    reapproval = [topic for file, topic in REAPPROVAL.items()
                  if any(name != PREAMBLE for name in affected.get(file, []))]
    return {"command": "affected", "ids": ids, "records": records, "affected": affected,
            "unaffected": unaffected, "reapproval": reapproval}


def cmd_affected(args) -> int:
    root = args.case.resolve()
    if not root.is_dir():
        raise Refused("case_missing", "папка кейса не найдена")
    evidence.emit(find_affected(root, list(dict.fromkeys(args.ids))))
    return EXIT_OK


# --- snapshot --------------------------------------------------------------

def snapshot(case: Path, number: int) -> dict:
    if number < 1:
        raise Refused("bad_number", "номер пересмотра начинается с 1")
    sources = output_files(case)
    if not sources:
        raise Refused("nothing_to_copy", "в outputs/ нет файлов для копирования")
    folder = case / HISTORY / str(number)
    names = [path.name for path in sources]
    report = {"command": "snapshot", "number": number, "folder": f"{HISTORY}/{number}/", "files": names}
    if folder.exists() or folder.is_symlink():
        same = (folder.is_dir() and not folder.is_symlink()
                and sorted(path.name for path in folder.iterdir()) == names
                and all((folder / path.name).is_file() and (folder / path.name).read_bytes() == path.read_bytes()
                        for path in sources))
        if not same:
            raise Refused("history_differs", "папка истории уже есть и отличается от текущих файлов")
        return report | {"already": True}
    staging = folder.with_name(f".tmp-{os.getpid()}-{number}")
    try:
        staging.mkdir(parents=True)
        for path in sources:
            shutil.copyfile(path, staging / path.name)
        os.rename(staging, folder)
    finally:
        shutil.rmtree(staging, ignore_errors=True)
    return report | {"already": False}


def cmd_snapshot(args) -> int:
    root = args.case.resolve()
    if not root.is_dir():
        raise Refused("case_missing", "папка кейса не найдена")
    evidence.emit(snapshot(root, args.number))
    return EXIT_OK


# --- command line ----------------------------------------------------------

def build_parser() -> Parser:
    parser = Parser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    commands = parser.add_subparsers(dest="command", required=True, parser_class=Parser)
    affected = commands.add_parser("affected")
    affected.add_argument("case", type=Path)
    affected.add_argument("ids", nargs="+")
    affected.set_defaults(run=cmd_affected)
    copy = commands.add_parser("snapshot")
    copy.add_argument("case", type=Path)
    copy.add_argument("number", type=int)
    copy.set_defaults(run=cmd_snapshot)
    return parser


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    try:
        args = build_parser().parse_args(argv)
        return args.run(args)
    except UsageError as error:
        print(json.dumps({"status": "refused", "reason": "usage", "message": str(error)}, ensure_ascii=False))
        return EXIT_REFUSED
    except Refused as error:
        print(json.dumps({"status": "refused", "reason": error.reason, "message": error.message}, ensure_ascii=False))
        return EXIT_REFUSED
    except MaterialRefused as error:
        print(json.dumps({"status": "refused", "reason": str(error)} | error.details, ensure_ascii=False))
        return EXIT_REFUSED
    except Exception as error:  # noqa: BLE001 - the text of an exception could quote case data
        print(json.dumps({"status": "internal_error", "error_type": type(error).__name__}))
        return EXIT_INTERNAL


if __name__ == "__main__":
    raise SystemExit(main())
