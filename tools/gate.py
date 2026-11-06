"""Check whether a case may leave a stage. Reads only.

Commands (run from the repository root):
  check <case folder> <stage> [--preliminary]

Stages are cumulative: research, options, decision, strategies, validation_plan,
done. The first output line is a JSON report with codes, files, IDs and line
numbers, never the text of a material: a number of a strategy may be named. Exit codes: 0 passed, 1 problems found,
4 refused, 5 internal failure. Passing means the mechanical signs hold, not that
the strategy is good. The rules are in agent/contracts/README.md.
"""
import argparse
import json
import re
import sys
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from pathlib import Path

from calc_engine import CalcError, number_matches, numbers_in_text, round_half_up, same_number, to_decimal
from checkpoint import Parser, Refused, UsageError
from validate_project import BRACKET_RE, ID_RE, Validator, is_active, prefix_of

EXIT_OK, EXIT_PROBLEMS, EXIT_REFUSED, EXIT_INTERNAL = 0, 1, 4, 5
ACCOUNTING_REPORT = "outputs/accounting.md"
STAGES = ("research", "options", "decision", "strategies", "validation_plan", "done")
PRELIMINARY_STAGES = ("strategies", "validation_plan")
ASSUMPTION = "(допущение)"

OPTIONS = "outputs/options.md"
PRODUCT = "outputs/product-strategy.md"
COMMUNICATION = "outputs/communication-strategy.md"
PLAN = "outputs/validation-plan.md"
OPTION_REGISTRY = "decisions/options.json"
QUESTION_REGISTRY = "research/questions.json"
DECISION_REGISTRY = "decisions/decisions.json"

CHOSEN_SECTION = "Выбранное направление"
PROMISE_SECTION = "Обещания и возможности"
PROMISE_COLUMNS = ["Обещание", "Возможность продукта", "Основание"]
COMPARISON_SECTION = "Сравнение вариантов"
SECTIONS = {
    OPTIONS: ("Что известно", "Чего не хватает", COMPARISON_SECTION, "Рекомендация", "Что изменит рекомендацию"),
    PRODUCT: ("Аудитория и задача", "Текущие альтернативы", CHOSEN_SECTION, "Отказы", "Ценность",
              "Затраты и доходы", "Допущения и ограничения"),
    COMMUNICATION: ("Аудитория и ситуация", "Барьер", "Ключевое сообщение", "Подтверждение", "Роль каналов",
                    "Границы обещаний", PROMISE_SECTION),
    PLAN: ("Ожидаемое изменение", "Показатель и единица", "Исходное состояние", "С чем сравниваем",
           "Период наблюдения", "Условие успеха и неуспеха", "Нежелательные последствия"),
}
KIND_SECTIONS = {"close": "Завершение и последствия", "keep": "Почему без изменений и условия пересмотра"}
LINKED_SECTIONS = {
    PRODUCT: ("Аудитория и задача", CHOSEN_SECTION, "Ценность", "Затраты и доходы"),
    COMMUNICATION: ("Ключевое сообщение", "Подтверждение"),
    PLAN: ("Исходное состояние",),
}
OPTION_TEXT_FIELDS = ("title", "audience_task", "action", "benefit_mechanism", "economics", "reversibility",
                      "verification")
OPTION_LIST_FIELDS = ("rejected_actions", "preconditions", "objections", "risks")

STATUS_CHOSEN_RE = re.compile(r"Статус: выбор сделан \[(DEC-[0-9]{4,})\], вариант \[(OPT-[0-9]{4,})\]")
STATUS_PRELIMINARY_RE = re.compile(r"Статус: предварительно, выбор не сделан, предложен вариант \[(OPT-[0-9]{4,})\]")
MONTHS = "января|февраля|марта|апреля|мая|июня|июля|августа|сентября|октября|ноября|декабря"
NOT_QUANTITY_RE = re.compile(rf"\b\d{{4}}-\d{{2}}(?:-\d{{2}})?\b|\b\d{{1,2}}\.\d{{1,2}}\.\d{{2,4}}\b|\b\d{{1,2}}\.\d{{4}}\b"
                             rf"|\b\d{{1,2}}\s+(?:{MONTHS})\b|\b\d+/\d+\b|[A-Za-zА-Яа-яЁё]+-\d+\b", re.IGNORECASE)
LIST_NUMBER_RE = re.compile(r"\s*\d+[.)]\s")
GLUED_UNIT_RE = re.compile(r"(?<=\d)(?=[а-яё])")
DASH_AFTER_UNIT_RE = re.compile(r"(?<=%)[–—-](?=\d)|(?<=млн)[–—-](?=\d)|(?<=тыс)[–—-](?=\d)|(?<=млрд)[–—-](?=\d)")
SCALE_RE = re.compile(r"\s*(тыс|млн|млрд|миллион|миллиард)[а-яё]*\.?|\s*(%)")
SCALES = {"тыс": 1000, "млн": 10 ** 6, "млрд": 10 ** 9, "миллион": 10 ** 6, "миллиард": 10 ** 9}
RANGE_LINK_RE = re.compile(r"\s*(?:[–—-]|до)\s*")
YEAR_WORD_RE = re.compile(r"\s*(?:гг?\.|год|года|году|годы|годах|годов)(?![а-яё])")
YEAR_RANGE_RE = re.compile(r"[–—-]\d{4}" + YEAR_WORD_RE.pattern)
HEADING_RE = re.compile(r"#{1,6}\s")
MAX_ROUNDING_DIGITS = 10


@dataclass
class Result:
    problems: list[dict] = field(default_factory=list)
    warnings: list[dict] = field(default_factory=list)

    def problem(self, code, file=None, record_id=None, note="", line=None):
        self.problems.append(entry(code, file, record_id, note, line))

    def warning(self, code, file=None, record_id=None, note="", line=None):
        self.warnings.append(entry(code, file, record_id, note, line))


def entry(code, file, record_id, note, line) -> dict:
    item = {"code": code, "file": file, "id": record_id, "note": note}
    if line is not None:
        item["line"] = line
    return item


# --- text of outputs -------------------------------------------------------

def read_output(root: Path, file: str) -> list[str] | None:
    try:
        return (root / file).read_bytes().decode("utf-8-sig").splitlines()
    except OSError:
        return None


def split_sections(lines: list[str]) -> dict:
    """Heading of the second level -> list of (line number, line)."""
    sections, current, fenced = {}, None, False
    for number, line in enumerate(lines, 1):
        if line.lstrip().startswith("```"):
            fenced = not fenced
        if not fenced and line.startswith("## "):
            current = sections.setdefault(line[3:].strip(), [])
        elif not fenced and line.startswith("# "):
            current = None
        elif current is not None:
            current.append((number, line))
    return sections


def has_text(body: list) -> bool:
    return any(line.strip() and not line.lstrip().startswith("#") for _, line in body)


def references(line: str) -> list[str]:
    found = []
    for match in BRACKET_RE.finditer(line):
        for part in match.group(1).split(","):
            if ID_RE.fullmatch(part.strip()):
                found.append(part.strip())
    return found


def status_line(lines: list[str]) -> tuple[int, str] | None:
    """The first line with text after the file title."""
    seen_title = False
    for number, line in enumerate(lines, 1):
        if not line.strip():
            continue
        if not seen_title and line.startswith("# "):
            seen_title = True
            continue
        return number, line.strip()
    return None


# --- numbers ---------------------------------------------------------------

def is_status_line(line: str) -> bool:
    return bool(STATUS_CHOSEN_RE.fullmatch(line.strip()) or STATUS_PRELIMINARY_RE.fullmatch(line.strip()))


def blank(match: re.Match) -> str:
    return "\0" * len(match.group(0))  # not a space: a link must not read as a gap in a range


def strip_non_numbers(line: str) -> str:
    """Hide what holds digits but is no quantity; positions stay as they were."""
    line = BRACKET_RE.sub(lambda m: blank(m) if references(m.group(0)) else m.group(0), line)
    line = ID_RE.sub(blank, line)
    line = NOT_QUANTITY_RE.sub(blank, line)
    found = LIST_NUMBER_RE.match(line)
    line = blank(found) + line[found.end():] if found else line
    return DASH_AFTER_UNIT_RE.sub(" ", GLUED_UNIT_RE.sub(" ", line))


def is_year(value, token: str, after: str) -> bool:
    return (isinstance(value, int) and 1900 <= value <= 2099 and token.isdigit()
            and bool(YEAR_WORD_RE.match(after) or YEAR_RANGE_RE.match(after)))


def quantities(line: str) -> list[dict]:
    """Numbers of a line that must be backed: without years and without those a marker frees.

    Going from the end lets the first bound of a range take the unit and the marker of the second.
    """
    text = strip_non_numbers(line)
    matches = number_matches(text)
    found, following = [], None
    for index in range(len(matches) - 1, -1, -1):
        value, match = matches[index]
        next_start = matches[index + 1][1].start() if index + 1 < len(matches) else len(text)
        between = text[match.end():next_start]
        in_range = following is not None and RANGE_LINK_RE.fullmatch(between) is not None
        digits = re.search(r"[.,](\d+)$", match.group(2))
        scale = SCALE_RE.match(between)
        item = {"value": value, "digits": len(digits.group(1)) if digits else 0, "signed": bool(match.group(1)),
                "scale": SCALES[scale.group(1)] if scale and scale.group(1) else 1,
                "percent": bool(scale and scale.group(2)),
                "free": ASSUMPTION in between or (in_range and following["free"])}
        if in_range and not scale:
            item["scale"], item["percent"] = following["scale"], following["percent"]
        following = item
        if not item["free"] and not is_year(value, match.group(2), text[match.end():]):
            found.append(item)
    return found[::-1]


def record_numbers(record_id: str, index: dict) -> list:
    record = index.get(record_id)
    if not isinstance(record, dict):
        return []
    if prefix_of(record_id) == "CALC":
        values = [record["result"]["value"]] + [item["value"] for item in record["inputs"]]
        return [value for value in values if value is not None]
    texts = [record["text"]]
    for side in record["supports"] + record["contradicts"]:
        if side["locator"].get("quote"):
            texts.append(side["locator"]["quote"])
    numbers = []
    for text in texts:
        try:
            numbers += numbers_in_text(text)
        except CalcError:  # a sign that cannot be read must not hide the other numbers of the record
            numbers += numbers_in_text(text.replace("–", " "))
    return numbers


def matches_record(quantity: dict, number) -> bool:
    """Equal up to the rounding of the text. A record may hold the number in the same scale as the text.

    The sign is compared only when the text writes it: otherwise words like «падение на» carry it.
    """
    scales = [Decimal(1), Decimal(quantity["scale"])] + ([Decimal("0.01")] if quantity["percent"] else [])
    digits = min(quantity["digits"], MAX_ROUNDING_DIGITS)
    for scale in scales:
        try:
            rounded = round_half_up(to_decimal(number) / scale, digits)
        except CalcError:
            continue
        if quantity["signed"]:
            equal = same_number(rounded, quantity["value"])
        else:
            equal = same_number(abs(rounded), abs(quantity["value"]))
        if equal:
            return True
    return False


def check_numbers(result, file, lines, index, record_id=None, shared_refs=()):
    for number, line in lines:
        if HEADING_RE.match(line) or is_status_line(line):
            continue
        try:
            found = quantities(line)
        except CalcError:
            result.problem("NUMBER_AMBIGUOUS", file, record_id, "the sign of a number cannot be read", number)
            continue
        if not found:
            continue
        linked = [ref for ref in [*references(line), *shared_refs] if prefix_of(ref) in ("CLM", "CALC")]
        if not linked:
            result.problem("NUMBER_UNLINKED", file, record_id, "a number without a CLM or CALC link", number)
            continue
        known = [value for ref in linked for value in record_numbers(ref, index)]
        for quantity in found:
            if not any(matches_record(quantity, value) for value in known):
                result.problem("NUMBER_MISMATCH", file, record_id,
                               f"{quantity['value']} is not in {', '.join(dict.fromkeys(linked))}", number)


# --- stage checks ----------------------------------------------------------

class Gate:
    def __init__(self, root: Path, validator: Validator, stage: str, preliminary: bool, result: Result):
        self.root, self.stage, self.preliminary, self.result = root, stage, preliminary, result
        self.index = validator.index
        self.questions = [r for r in validator.registries["Q"] if is_active(r["id"], r)]
        self.options = [r for r in validator.registries["OPT"] if is_active(r["id"], r)]
        self.decisions = [r for r in validator.registries["DEC"] if is_active(r["id"], r)]
        self.direction = self.active_decision("direction")
        self.texts: dict[str, list[str] | None] = {}

    def active_decision(self, topic):
        return next((d for d in self.decisions if d["topic"] == topic), None)

    def lines_of(self, file):
        if file not in self.texts:
            self.texts[file] = read_output(self.root, file)
        return self.texts[file]

    def expected_option(self):
        """The option the strategies must follow, or None when there is no such option yet."""
        if self.direction is None:
            return None
        part = self.direction.get("choice") if self.direction["status"] == "chosen" else self.direction["proposal"]
        return part["option_id"] if part else None

    def run(self):
        reached = STAGES.index(self.stage)
        for number, check in enumerate((self.check_research, self.check_options, self.check_decision,
                                        self.check_strategies, self.check_plan, self.check_done)):
            if number <= reached:
                check()

    def check_research(self):
        result = self.result
        if not self.questions:
            result.problem("RESEARCH_EMPTY", QUESTION_REGISTRY, None, "no active questions")
        for question in self.questions:
            qid, gap = question["id"], (question["gap"] or "").strip()
            if question["status"] == "open":
                result.problem("RESEARCH_OPEN", QUESTION_REGISTRY, qid, "the question is still open")
            if question["status"] == "answered" and not question["findings"]:
                result.problem("RESEARCH_NO_FINDING", QUESTION_REGISTRY, qid, "answered without findings")
            if question["status"] in ("partial", "blocked") and not gap:
                result.problem("RESEARCH_NO_GAP", QUESTION_REGISTRY, qid, "the gap is not named")
            contested = [ref for ref in question["findings"] if prefix_of(ref) == "CLM"
                         and isinstance(self.index.get(ref), dict) and self.index[ref]["status"] == "contested"]
            if contested and not gap:
                result.problem("RESEARCH_HIDDEN_CONFLICT", QUESTION_REGISTRY, qid,
                               f"contested finding without a gap: {', '.join(contested)}")

    def check_options(self):
        result = self.result
        if len(self.options) < 3:
            result.problem("OPTIONS_TOO_FEW", OPTION_REGISTRY, None, f"{len(self.options)} active options")
        kinds = {option["strategy_kind"] for option in self.options}
        for kind in ("keep", "close", "develop"):
            if kind not in kinds:
                result.problem(f"OPTIONS_NO_{kind.upper()}", OPTION_REGISTRY, None, f"no {kind} option")
        actions = {}
        for option in self.options:
            action = " ".join(option["action"].casefold().split()).rstrip(".!")
            if action in actions:
                result.problem("OPTIONS_DUPLICATE", OPTION_REGISTRY, option["id"],
                               f"the same action as {actions[action]}")
            actions.setdefault(action, option["id"])
            self.check_option_fields(option)
        if self.direction is None:
            result.problem("PROPOSAL_MISSING", DECISION_REGISTRY, None, "no direction decision")
        lines = self.check_structure(OPTIONS, SECTIONS[OPTIONS])
        if lines is None:
            return
        sections = split_sections(lines)
        table = {ref for _, line in sections.get(COMPARISON_SECTION, [])
                 if line.lstrip().startswith("|") for ref in references(line)}
        for option in self.options:
            if option["id"] not in table:
                result.problem("OPTIONS_TABLE", OPTIONS, option["id"], "the option is not in the comparison table")
        check_numbers(result, OPTIONS, list(enumerate(lines, 1)), self.index)

    def check_option_fields(self, option):
        empty = [name for name in OPTION_TEXT_FIELDS if not option[name].strip()]
        empty += [name for name in OPTION_LIST_FIELDS
                  if not option[name] or not all(item.strip() for item in option[name])]
        if empty:
            self.result.problem("OPTION_FIELD_EMPTY", OPTION_REGISTRY, option["id"], ", ".join(empty))
        check_numbers(self.result, OPTION_REGISTRY, list(enumerate(option["economics"].splitlines(), 1)),
                      self.index, option["id"], option["basis"])

    def check_decision(self):
        if self.preliminary:
            return
        if self.direction is not None and self.direction["status"] != "chosen":
            self.result.problem("DECISION_PENDING", DECISION_REGISTRY, self.direction["id"],
                                "no choice of the person yet")

    def check_strategies(self):
        option = self.index.get(self.expected_option())
        kind = option["strategy_kind"] if isinstance(option, dict) else None
        required = SECTIONS[COMMUNICATION] + ((KIND_SECTIONS[kind],) if kind in KIND_SECTIONS else ())
        self.check_output(PRODUCT, SECTIONS[PRODUCT])
        self.check_output(COMMUNICATION, required)
        self.check_chosen_section()
        self.check_promises()

    def check_plan(self):
        self.check_output(PLAN, SECTIONS[PLAN])

    def check_done(self):
        result = self.result
        if not (self.root / ACCOUNTING_REPORT).is_file():
            result.problem("ACCOUNTING_MISSING", ACCOUNTING_REPORT, None, "no accounting report")
        thresholds = self.active_decision("thresholds")
        if thresholds is None or thresholds["status"] != "chosen":
            result.problem("THRESHOLDS_PENDING", DECISION_REGISTRY, None, "the person has not agreed the thresholds")
        elif self.is_before_direction(thresholds):
            result.problem("THRESHOLDS_PENDING", DECISION_REGISTRY, thresholds["id"],
                           "the thresholds were agreed before the current choice of direction")
        for record_id, record in self.index.items():
            if prefix_of(record_id) == "ACT" and isinstance(record, dict) and record.get("end") is None:
                result.problem("ACTIONS_OPEN", "state/actions.jsonl", record_id, "started and not finished")

    def is_before_direction(self, thresholds) -> bool:
        choice = self.direction.get("choice") if self.direction else None
        if not choice:
            return False
        return (datetime.fromisoformat(thresholds["choice"]["chosen_at"])
                < datetime.fromisoformat(choice["chosen_at"]))

    def check_structure(self, file, required):
        """Existence and sections; returns the lines, or None when the file is missing."""
        lines = self.lines_of(file)
        if lines is None:
            self.result.problem("OUTPUT_MISSING", file, None, "no such file")
            return None
        sections = split_sections(lines)
        for name in required:
            if name not in sections:
                self.result.problem("OUTPUT_SECTION", file, None, f"no section «{name}»")
            elif not has_text(sections[name]):
                self.result.problem("OUTPUT_SECTION", file, None, f"section «{name}» is empty")
        return lines

    def check_output(self, file, required):
        lines = self.check_structure(file, required)
        if lines is None:
            return
        self.check_status(file, lines)
        check_numbers(self.result, file, list(enumerate(lines, 1)), self.index)
        sections = split_sections(lines)
        for name in LINKED_SECTIONS.get(file, ()):
            body = sections.get(name, [])
            if body and not any(references(line) or ASSUMPTION in line for _, line in body):
                self.result.problem("SECTION_UNLINKED", file, None, f"section «{name}» has no link")

    def check_status(self, file, lines):
        result = self.result
        found = status_line(lines)
        number, text = found if found else (None, "")
        chosen, draft = STATUS_CHOSEN_RE.fullmatch(text), STATUS_PRELIMINARY_RE.fullmatch(text)
        expected = self.expected_option()
        if draft and not self.preliminary:
            code = "PRELIMINARY_LEFT" if self.stage == "done" else "STATUS_LINE"
            result.problem(code, file, None, "the file is preliminary", number)
        elif self.preliminary:
            if not draft or (self.direction is not None and self.direction["status"] == "chosen"):
                result.problem("STATUS_LINE", file, None, "a preliminary status is expected", number)
            elif draft.group(1) != expected:
                result.problem("WRONG_OPTION", file, draft.group(1), f"proposed option is {expected}", number)
        elif not chosen:
            result.problem("STATUS_LINE", file, None, "no status line", number)
        else:
            if self.direction is None or chosen.group(1) != self.direction["id"]:
                result.problem("STATUS_LINE", file, chosen.group(1), "not the active decision", number)
            if chosen.group(2) != expected:
                result.problem("WRONG_OPTION", file, chosen.group(2), f"chosen option is {expected}", number)

    def check_chosen_section(self):
        lines = self.lines_of(PRODUCT)
        expected = self.expected_option()
        body = split_sections(lines).get(CHOSEN_SECTION, []) if lines else []
        if body and expected and not any(expected in references(line) for _, line in body):
            self.result.problem("WRONG_OPTION", PRODUCT, expected, "the chosen option is not in the section")

    def check_promises(self):
        lines = self.lines_of(COMMUNICATION)
        if lines is None:
            return
        body = split_sections(lines).get(PROMISE_SECTION)
        if body is None:
            return
        rows = [(number, [cell.strip() for cell in line.strip().strip("|").split("|")])
                for number, line in body if line.lstrip().startswith("|")]
        if not rows or rows[0][1] != PROMISE_COLUMNS:
            self.result.problem("PROMISE_TABLE", COMMUNICATION, None, "columns: " + ", ".join(PROMISE_COLUMNS))
            return
        rows = [row for row in rows[1:] if not all(set(cell) <= set(":- ") for cell in row[1])]
        if not rows:
            self.result.problem("PROMISE_TABLE", COMMUNICATION, None, "the table has no rows")
        for number, cells in rows:
            self.check_promise(number, cells)

    def check_promise(self, number, cells):
        refs = references(cells[-1]) if len(cells) == len(PROMISE_COLUMNS) else []
        if not refs or not all(cells[:-1]):
            self.result.problem("PROMISE_TABLE", COMMUNICATION, None, "a row needs a promise, a capability and a basis",
                                number)
        elif not any(self.is_firm(ref) for ref in refs):
            if set(refs) == {self.expected_option()}:
                self.result.warning("PROMISE_PLANNED", COMMUNICATION, self.expected_option(),
                                    "the promise rests on the plan only", number)
            else:
                self.result.problem("PROMISE_WEAK", COMMUNICATION, None, "no confirmed basis", number)

    def is_firm(self, ref) -> bool:
        record = self.index.get(ref)
        if not isinstance(record, dict):
            return False
        if prefix_of(ref) == "CLM":
            return record["status"] == "confirmed"
        return prefix_of(ref) == "CALC" and record["result"]["status"] == "computed"


def run_check(root: Path, stage: str, preliminary: bool) -> Result:
    validator = Validator(root)
    report = validator.run()
    result = Result()
    for warning in report.warnings:
        result.warning(warning.code, warning.file, warning.record_id, "")
    if report.errors:
        found = sorted({f"{error.code} {error.file}" for error in report.errors})
        result.problem("CASE_INVALID", None, None, ", ".join(found))
        return result
    Gate(root, validator, stage, preliminary, result).run()
    return result


def cmd_check(args) -> int:
    root = args.case.resolve()
    if not root.is_dir():
        raise Refused("case_missing", "папка кейса не найдена")
    if args.preliminary and args.stage not in PRELIMINARY_STAGES:
        raise Refused("preliminary_stage", "предварительный режим есть только у strategies и validation_plan")
    result = run_check(root, args.stage, args.preliminary)
    print(json.dumps({"command": "check", "stage": args.stage, "preliminary": args.preliminary,
                      "passed": not result.problems, "problems": result.problems, "warnings": result.warnings},
                     ensure_ascii=False))
    return EXIT_PROBLEMS if result.problems else EXIT_OK


def build_parser() -> Parser:
    parser = Parser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    commands = parser.add_subparsers(dest="command", required=True, parser_class=Parser)
    check = commands.add_parser("check")
    check.add_argument("case", type=Path)
    check.add_argument("stage", choices=STAGES)
    check.add_argument("--preliminary", action="store_true")
    check.set_defaults(run=cmd_check)
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
    except Exception as error:  # noqa: BLE001 - the text of an exception could quote case data
        print(json.dumps({"status": "internal_error", "error_type": type(error).__name__}))
        return EXIT_INTERNAL


if __name__ == "__main__":
    raise SystemExit(main())
