"""Dossiers of one cell of the comparison E06 for the model judges.

Takes the results of two live runs of the suite hard on the same case (results.zip or the
unpacked results folder): the agent, the plain chat started with -PlainChat or the agent
without a component. Writes a judge folder with two anonymised dossiers under the random
labels X and Y, the expectations of the case, its control file and the task of the judge
taken from the protocol. The key of the labels goes next to the judge folder, never into it.

The dossier holds the final text of every reply of the comparison segment and the text
files the executor wrote. The protocol docs/acceptance/e06-protocol.md says what counts.
"""
import argparse
import hashlib
import json
import pathlib
import re
import secrets
import shutil
import sys
import tempfile
import zipfile

ROOT = pathlib.Path(__file__).resolve().parent.parent
# The comparison segment of the suite hard; c4 and c5 test the agent itself.
SEGMENT = ["c1-1", "c1-2", "c1-3", "c1-4", "c1-5a", "c1-5", "c1-6", "c2", "c3-1", "c3-2", "c3-2a", "c3-3"]
TEXT_SUFFIXES = {".md", ".txt", ".csv"}
# Parts of the case folder of the agent that are not its result for the manager.
AGENT_SKIPPED_DIRS = {"state", "sources", ".history"}
AGENT_SKIPPED_FILES = {"accounting.md"}
JUDGE_SECTIONS = ["Сценарий реплик", "Существенные ошибки и пропуски", "Пункты оценки", "Контрольные числа",
                  "Задание судьи"]

URL = re.compile(r"https?://[^\s)>\]`'\"]+")
# The agent opens every answer with its mode: a label of the variant, not content.
MODE_LINE = re.compile(r"^[ \t]*Режим: (?:продукт|разработка)\.[ \t]*\r?\n?", re.MULTILINE)
RECORD_ID = re.compile(r"\b[A-Z]{1,5}-\d{2,}\b")
SCRIPT = re.compile(r"(?<![\w-])[\w-]+\.py\b")
SERVICE_FILE = re.compile(r"(?<![\w.-])[\w.-]+\.jsonl?\b")
DRIVE_PATH = re.compile(r"(?<![\w])[A-Za-z]:[\\/](?:[^\s\\/`'\"()]+[\\/])+")
FOLDER_PATH = re.compile(r"(?<![\w/\\])(?:\.{1,2}[\\/])?(?:(?:projects|inbox|outputs|sources|evidence|decisions|research"
                         r"|state|tools|agent|materials|extra|results|clone|held|written|after-final|docs|\.venv)[\\/])"
                         r"(?:[\w.-]+[\\/])*")


def anonymise(text: str) -> str:
    """Paths, our utilities, service files, record numbers and the mode line go; the wording stays."""
    text = MODE_LINE.sub("", text)
    parts = []
    last = 0
    for match in URL.finditer(text):
        parts.append(_anonymise_plain(text[last:match.start()]))
        parts.append(match.group(0))
        last = match.end()
    parts.append(_anonymise_plain(text[last:]))
    return "".join(parts)


def _anonymise_plain(text: str) -> str:
    text = DRIVE_PATH.sub("", text)
    text = FOLDER_PATH.sub("", text)
    text = SCRIPT.sub("[утилита]", text)
    text = SERVICE_FILE.sub("[служебный файл]", text)
    return RECORD_ID.sub("[запись]", text)


def final_text(log: pathlib.Path) -> tuple[str, bool]:
    """The last answer of a reply and whether it ended normally.

    Claude Code puts it into the result event. Codex gives it as the last agent message of a
    turn that completed. A reply broken by the run has neither: then everything it said
    before the break is given.
    """
    final = None
    last_message = None
    completed = False
    said = []
    for line in log.read_text(encoding="utf-8-sig", errors="replace").splitlines():
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if not isinstance(event, dict):
            continue
        if event.get("type") == "result" and isinstance(event.get("result"), str):
            final = event["result"]
        if event.get("type") == "assistant":
            for block in (event.get("message") or {}).get("content") or []:
                if isinstance(block, dict) and block.get("type") == "text" and block.get("text"):
                    said.append(block["text"])
        item = event.get("item")
        if event.get("type") == "item.completed" and isinstance(item, dict) and item.get("type") == "agent_message":
            if item.get("text"):
                said.append(item["text"])
                last_message = item["text"]
        if event.get("type") == "turn.completed":
            completed = True
    if final is None and completed and last_message is not None:
        final = last_message
    if final is not None:
        return final, True
    return "\n\n".join(said), False


def summary_of(results: pathlib.Path) -> dict:
    path = results / "summary.txt"
    if not path.exists():
        raise SystemExit(f"No summary.txt in {results}: is this the results of a live run?")
    lines = path.read_text(encoding="utf-8-sig").splitlines()
    case = next((x.split(" ", 1)[1].strip() for x in lines if x.startswith("case ")), "")
    if not case:
        raise SystemExit(f"summary.txt of {results} names no case")
    return {"case": case, "plain": "plain_chat True" in lines,
            "commit": next((x.split(" ", 1)[1].strip() for x in lines if x.startswith("commit ")), ""),
            "model": next((x.split(" ", 1)[1].strip() for x in lines if x.startswith("model ")), ""),
            "counts": "run_counts True" in lines}


def written_files(results: pathlib.Path, case: str, plain: bool) -> tuple[pathlib.Path | None, list[pathlib.Path]]:
    """Text files the executor wrote by the end of c3-3."""
    if plain:
        base = results / "written"
        skip_dirs, skip_files = set(), set()
    else:
        # The snapshot right after c3-3: the case folder at the end has the failure steps in it.
        base = results / "after-final"
        if (base / case).is_dir():
            base = base / case
        skip_dirs, skip_files = AGENT_SKIPPED_DIRS, AGENT_SKIPPED_FILES
    if not base.is_dir():
        return None, []
    files = []
    for path in sorted(base.rglob("*")):
        relative = path.relative_to(base)
        if not path.is_file() or path.suffix.lower() not in TEXT_SUFFIXES:
            continue
        if set(relative.parts[:-1]) & skip_dirs or path.name in skip_files:
            continue
        files.append(path)
    return base, files


def dossier(results: pathlib.Path, label: str, info: dict) -> str:
    out = [f"# Досье {label}", "", "## Ответы на реплики руководителя", ""]
    replies = 0
    for name in SEGMENT:
        log = results / f"{name}.jsonl"
        if not log.exists():
            continue
        replies += 1
        text, ended = final_text(log)
        out.append(f"### {name}")
        out.append("")
        if not ended:
            out.append("_Реплика оборвана, ниже всё, что исполнитель успел сказать до обрыва._")
            out.append("")
        out.append(anonymise(text).strip() or "_Ответа нет._")
        out.append("")
    if not replies:
        raise SystemExit(f"No reply of the comparison segment in {results}")
    base, files = written_files(results, info["case"], info["plain"])
    out += ["## Файлы, которые написал исполнитель", ""]
    if not files:
        out += ["_Текстовых файлов нет._", ""]
    for path in files:
        out.append(f"### {path.name}")
        out.append("")
        body = anonymise(path.read_text(encoding="utf-8-sig", errors="replace")).strip()
        fence = "````" if "```" in body else "```"
        out += [fence, body, fence, ""]
    return "\n".join(out)


def judge_task(protocol: pathlib.Path) -> str:
    """The sections of the protocol that the judge gets, as they are written there."""
    if not protocol.exists():
        raise SystemExit(f"The protocol is missing: {protocol}")
    lines = protocol.read_text(encoding="utf-8-sig").splitlines()
    found = {}
    for index, line in enumerate(lines):
        heading = re.match(r"^(#{2,3}) (.+?)\s*$", line)
        if not heading or heading.group(2) not in JUDGE_SECTIONS:
            continue
        level = len(heading.group(1))
        end = next((j for j in range(index + 1, len(lines))
                    if re.match(rf"^#{{1,{level}}} ", lines[j])), len(lines))
        body = lines[index + 1:end]
        if heading.group(2) == "Сценарий реплик":
            # Only the tables: the notes around them say which variant uses which words.
            tables = []
            for line in body:
                if line.startswith("|"):
                    tables.append(line)
                elif tables and tables[-1]:
                    tables.append("")
            body = tables
        found[heading.group(2)] = "\n".join(body).strip()
    missing = [name for name in JUDGE_SECTIONS if name not in found]
    if missing:
        raise SystemExit(f"The protocol has no section {', '.join(missing)}")
    parts = ["# Задание судьи", found["Задание судьи"]]
    parts += [f"## {name}\n\n{found[name]}" for name in JUDGE_SECTIONS if name != "Задание судьи"]
    return "\n\n".join(parts) + "\n"


def unpacked(source: pathlib.Path, scratch: pathlib.Path) -> pathlib.Path:
    if source.is_dir():
        return source
    if not zipfile.is_zipfile(source):
        raise SystemExit(f"Neither a results folder nor a zip archive: {source}")
    target = scratch / hashlib.sha256(str(source.resolve()).encode()).hexdigest()[:12]
    with zipfile.ZipFile(source) as archive:
        for member in archive.infolist():
            name = member.filename.replace("\\", "/")
            # An archive of a run is ours, but a path must still not leave the folder.
            if name.startswith("/") or ".." in name.split("/"):
                raise SystemExit(f"Unsafe path in {source}: {member.filename}")
            path = target / name
            if member.is_dir() or name.endswith("/"):
                path.mkdir(parents=True, exist_ok=True)
                continue
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(archive.read(member))
    return target


def build(first: pathlib.Path, second: pathlib.Path, out: pathlib.Path, protocol: pathlib.Path,
          swap: bool | None = None) -> dict:
    judge = out / "judge"
    if judge.exists() and any(judge.iterdir()):
        raise SystemExit(f"The judge folder is not empty: {judge}")
    with tempfile.TemporaryDirectory() as tmp:
        runs = []
        for source in (first, second):
            results = unpacked(source, pathlib.Path(tmp))
            runs.append((source, results, summary_of(results)))
        if runs[0][2]["case"] != runs[1][2]["case"]:
            raise SystemExit(f"The runs are of different cases: {runs[0][2]['case']} and {runs[1][2]['case']}")
        case = runs[0][2]["case"]
        expectations = ROOT / "docs" / "acceptance" / f"{case}.md"
        control = ROOT / "docs" / "acceptance" / "control" / f"{case}.json"
        for path in (expectations, control):
            if not path.exists():
                raise SystemExit(f"Missing for the case {case}: {path}")
        task = judge_task(protocol)
        if swap is None:
            swap = secrets.randbelow(2) == 1
        labelled = dict(zip(("X", "Y"), reversed(runs) if swap else runs))
        judge.mkdir(parents=True, exist_ok=True)
        key = {"case": case, "labels": {}}
        for label, (source, results, info) in labelled.items():
            text = dossier(results, label, info)
            (judge / f"dossier-{label}.md").write_text(text, encoding="utf-8")
            key["labels"][label] = {"source": str(source), "plain_chat": info["plain"], "commit": info["commit"],
                                    "model": info["model"], "run_counts": info["counts"],
                                    "sha256": hashlib.sha256(text.encode("utf-8")).hexdigest()}
        (judge / "task.md").write_text(task, encoding="utf-8")
        shutil.copyfile(expectations, judge / "expectations.md")
        shutil.copyfile(control, judge / "control.json")
    (out / "key.json").write_text(json.dumps(key, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return key


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("first", type=pathlib.Path, help="results.zip or results folder of one run")
    parser.add_argument("second", type=pathlib.Path, help="the same of the other run of the cell")
    parser.add_argument("--out", type=pathlib.Path, required=True, help="folder for judge/ and key.json")
    parser.add_argument("--protocol", type=pathlib.Path, default=ROOT / "docs" / "acceptance" / "e06-protocol.md")
    args = parser.parse_args(argv)
    key = build(args.first, args.second, args.out, args.protocol)
    print(f"Case {key['case']}: give the judges {args.out / 'judge'}; keep {args.out / 'key.json'} closed until both finish.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
