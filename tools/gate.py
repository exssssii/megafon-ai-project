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
from material_extract import load_limits
from outputs_md import STATUS_CHOSEN_RE, STATUS_PRELIMINARY_RE, is_status_line, split_sections
from revision import REVISIONS, parse_revisions
from team import TEAM, owners_of, read_team
from validate_project import BRACKET_RE, ID_RE, Validator, is_active, is_synthetic_source, prefix_of

EXIT_OK, EXIT_PROBLEMS, EXIT_REFUSED, EXIT_INTERNAL = 0, 1, 4, 5
ACCOUNTING_REPORT = "outputs/accounting.md"
STAGES = ("research", "options", "decision", "strategies", "validation_plan", "done")
PRELIMINARY_STAGES = ("strategies", "validation_plan")
ASSUMPTION = "(допущение)"
BRIEF = "brief.md"
BRIEF_REF = "[бриф]"
USER_WORDS_SECTION = "Слова пользователя"

OPTIONS = "outputs/options.md"
PRODUCT = "outputs/product-strategy.md"
COMMUNICATION = "outputs/communication-strategy.md"
PLAN = "outputs/validation-plan.md"
COMMITMENTS = "outputs/commitments.md"
GUIDE = "outputs/interview-guide.md"
HISTORY = "outputs/.history"
OPTION_REGISTRY = "decisions/options.json"
QUESTION_REGISTRY = "research/questions.json"
DECISION_REGISTRY = "decisions/decisions.json"

CHOSEN_SECTION = "Выбранное направление"
PROMISE_SECTION = "Обещания и возможности"
PROMISE_COLUMNS = ["Обещание", "Возможность продукта", "Основание"]
COMPARISON_SECTION = "Сравнение вариантов"
ACTION_SECTION = "Действия"
ACTION_COLUMNS = ["Действие", "Ответственный", "Срок", "Показатель", "Основание"]
REVIEW_SECTION = "Условия пересмотра"
APPROVAL_SECTION = "Согласование"
SECTIONS = {
    OPTIONS: ("Что известно", "Чего не хватает", COMPARISON_SECTION, "Рекомендация", "Что изменит рекомендацию"),
    PRODUCT: ("Аудитория и задача", "Текущие альтернативы", CHOSEN_SECTION, "Отказы", "Ценность",
              "Затраты и доходы", "Допущения и ограничения"),
    COMMUNICATION: ("Аудитория и ситуация", "Барьер", "Ключевое сообщение", "Подтверждение", "Роль каналов",
                    "Границы обещаний", PROMISE_SECTION),
    PLAN: ("Ожидаемое изменение", "Показатель и единица", "Исходное состояние", "С чем сравниваем",
           "Период наблюдения", "Условие успеха и неуспеха", "Нежелательные последствия"),
    COMMITMENTS: ("Решение", ACTION_SECTION, "Обязательства", REVIEW_SECTION, APPROVAL_SECTION),
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
GAPS_SECTION = "Чего не хватает"
SUCCESS_SECTION = "Условие успеха и неуспеха"

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
GUIDE_SECTIONS = ("Кого спрашивать", "Согласие и данные")
GUIDE_STATUS_RE = re.compile(r"Статус: способ выбран \[(DEC-[0-9]{4,})\]")
GUIDE_QUESTION_RE = re.compile(r"Q-[0-9]{4,}\b")
GUIDE_QUESTION_HEAD_RE = re.compile(r"(Q-[0-9]{4,})\.\s*\S")
SYNTHETIC_MARK = "(синтетика)"
SYNTHETIC_MARK_RE = re.compile(r"\s*" + re.escape(SYNTHETIC_MARK))
MAX_SYNTHETIC_SESSIONS = 3
MAX_INTERVIEW_QUESTIONS = 10
QUESTION_SECTION_PREFIX = "Вопрос "
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


def user_numbers(root: Path) -> list:
    """Numbers the user named: a limit or a budget from the brief has no CLM behind it and needs none."""
    try:
        lines = read_output(root, BRIEF) or []
    except UnicodeDecodeError:
        return []
    body = split_sections(lines).get(USER_WORDS_SECTION, [])
    numbers = []
    for _, line in body:
        try:
            numbers += [(abs(item["value"]) * item["scale"], item["percent"]) for item in quantities(line)]
        except CalcError:
            continue
    return numbers


def said_by_user(quantity: dict, user_said) -> bool:
    """Exact, with the unit: unlike a record, a quotation holds numbers about anything."""
    return (abs(quantity["value"]) * quantity["scale"], quantity["percent"]) in user_said


def check_numbers(result, file, lines, index, record_id=None, shared_refs=(), user_said=()):
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
        from_user = user_said if BRIEF_REF in line else ()
        if not linked and BRIEF_REF not in line:
            result.problem("NUMBER_UNLINKED", file, record_id, "a number without a CLM or CALC link", number)
            continue
        known = [value for ref in linked for value in record_numbers(ref, index)]
        for quantity in found:
            if not any(matches_record(quantity, value) for value in known) and not said_by_user(quantity, from_user):
                result.problem("NUMBER_MISMATCH", file, record_id,
                               f"{quantity['value']} is not in {', '.join(dict.fromkeys(linked)) or 'the brief'}", number)


# --- stage checks ----------------------------------------------------------

class Gate:
    def __init__(self, root: Path, validator: Validator, stage: str, preliminary: bool, result: Result):
        self.root, self.stage, self.preliminary, self.result = root, stage, preliminary, result
        self.index = validator.index
        self.user_said = user_numbers(root)
        self.questions = [r for r in validator.registries["Q"] if is_active(r["id"], r)]
        self.options = [r for r in validator.registries["OPT"] if is_active(r["id"], r)]
        self.all_decisions = list(validator.registries["DEC"])
        self.decisions = [r for r in self.all_decisions if is_active(r["id"], r)]
        self.all_sources = list(validator.registries["SRC"])
        self.all_claims = list(validator.registries["CLM"])
        self.all_calculations = list(validator.registries["CALC"])
        self.sources = [r for r in self.all_sources if is_active(r["id"], r)]
        self.claims = [r for r in self.all_claims if is_active(r["id"], r)]
        self.calculations = [r for r in self.all_calculations if is_active(r["id"], r)]
        self.direction = self.active_decision("direction")
        self.cost_rules = validator.cost_rules   # options_format: 2 in case.json
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
        self.check_interviews()

    def synthetic_sources(self) -> list[dict]:
        """Active synthetic conversations: the ones the session limits and the method date apply to."""
        return [source for source in self.sources if is_synthetic_source(source)]

    def synthetic_based(self) -> set[str]:
        """Claims and calculations resting on a synthetic source, through derived_from and calculation inputs.

        Synthetic is where the text came from, so withdrawn sources, claims and calculations count too:
        a conversation replaced by a repeat leaves its hypotheses behind, and they stay marked.
        """
        synthetic = {source["id"] for source in self.all_sources if is_synthetic_source(source)}
        based = {claim["id"] for claim in self.all_claims
                 if any(item["source_id"] in synthetic for item in claim["supports"])}
        based |= {calc["id"] for calc in self.all_calculations
                  if any(item.get("source_id") in synthetic for item in calc["inputs"])}
        grown = True
        while grown:
            grown = False
            for claim in self.all_claims:
                if claim["id"] not in based and based & set(claim["derived_from"]):
                    based.add(claim["id"])
                    grown = True
            for calc in self.all_calculations:
                if calc["id"] not in based and any(item.get("calculation_id") in based for item in calc["inputs"]):
                    based.add(calc["id"])
                    grown = True
        return based

    def check_interviews(self):
        result = self.result
        synthetic, guide = self.synthetic_sources(), self.lines_of(GUIDE)
        if not synthetic and guide is None:
            return
        method = self.active_decision("interview_method")
        if method is not None and method["status"] != "chosen":
            method = None
        if method is None:
            result.problem("INTERVIEW_METHOD_MISSING", DECISION_REGISTRY, None,
                           "synthetic sources or a guide without a chosen interview_method decision")
        else:
            chosen_at = self.first_method_choice(method)
            for source in synthetic:
                if datetime.fromisoformat(source["created_at"]) < chosen_at:
                    result.problem("INTERVIEW_METHOD_MISSING", "evidence/sources.json", source["id"],
                                   "the source is older than the first choice of the interview method")
        self.check_session_limits(synthetic, method)
        if guide is not None:
            self.check_guide(guide, method)

    def first_method_choice(self, method) -> datetime:
        """When the person first chose an interview method: the earliest choice along the supersedes chain.

        A re-chosen method replaces the earlier decision, but the conversations and the budget decision made
        under it stay valid: what matters is that no conversation precedes the person's choice.
        """
        by_id = {d["id"]: d for d in self.all_decisions}
        times, node, seen = [], method, set()
        while node is not None and node["id"] not in seen:
            seen.add(node["id"])
            if node.get("choice"):
                times.append(datetime.fromisoformat(node["choice"]["chosen_at"]))
            node = by_id.get(node.get("supersedes"))
        return min(times)

    def check_session_limits(self, synthetic, method):
        result = self.result
        if len(synthetic) > MAX_SYNTHETIC_SESSIONS:
            chosen_at = self.first_method_choice(method) if method else None
            budget = any(d["topic"] == "budget" and d["status"] == "chosen" and chosen_at is not None
                         and datetime.fromisoformat(d["choice"]["chosen_at"]) > chosen_at for d in self.decisions)
            if not budget:
                result.problem("INTERVIEW_LIMIT", "evidence/sources.json", None,
                               f"{len(synthetic)} synthetic sources and no budget decision after the method")
        for source in synthetic:
            lines = read_output(self.root, source["location"].get("path", ""))
            if lines is None:
                continue
            asked = sum(1 for name in split_sections(lines) if name.startswith(QUESTION_SECTION_PREFIX))
            if asked > MAX_INTERVIEW_QUESTIONS:
                result.problem("INTERVIEW_LIMIT", source["location"]["path"], source["id"],
                               f"{asked} question sections, at most {MAX_INTERVIEW_QUESTIONS}")

    def check_guide(self, guide, method):
        result = self.result
        found = status_line(guide)
        number, text = found if found else (None, "")
        status = GUIDE_STATUS_RE.fullmatch(text)
        if not status:
            result.problem("INTERVIEW_GUIDE", GUIDE, None, "no status line «Статус: способ выбран [DEC-…]»", number)
        elif method is None or status.group(1) != method["id"]:
            result.problem("INTERVIEW_GUIDE", GUIDE, status.group(1), "not the active interview_method decision", number)
        self.check_structure(GUIDE, GUIDE_SECTIONS)
        by_id = {question["id"]: question for question in self.questions}
        if not any(GUIDE_QUESTION_RE.match(name) for name in split_sections(guide)):
            result.problem("INTERVIEW_GUIDE", GUIDE, None, "no question section «Q-NNNN. title»")
        for name in split_sections(guide):
            if not GUIDE_QUESTION_RE.match(name):
                continue
            head = GUIDE_QUESTION_HEAD_RE.match(name)
            question = by_id.get(head.group(1)) if head else None
            if head is None:
                result.problem("INTERVIEW_GUIDE", GUIDE, None, f"section «{name}» is not «Q-NNNN. title»")
            elif question is None:
                result.problem("INTERVIEW_GUIDE", GUIDE, head.group(1), "no such active question")
            elif question["status"] not in ("partial", "blocked"):
                result.problem("INTERVIEW_GUIDE", GUIDE, head.group(1),
                               f"the question is {question['status']}, a guide covers partial or blocked ones")
        check_numbers(result, GUIDE, list(enumerate(guide, 1)), self.index, user_said=self.user_said)

    def check_synthetic_marks(self, file, lines):
        based = self.synthetic_based()
        if not based:
            return
        for number, line in enumerate(lines, 1):
            for match in BRACKET_RE.finditer(line):
                hit = [ref for ref in references(match.group(0)) if ref in based]
                if hit and not SYNTHETIC_MARK_RE.match(line, match.end()):
                    self.result.problem("SYNTHETIC_UNMARKED", file, hit[0],
                                        f"no {SYNTHETIC_MARK} right after the reference", number)
                    break

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
            if self.cost_rules:
                self.check_option_cost(option)
                self.check_option_limit(option)
        if self.cost_rules:
            self.warn_about_cost()
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
        check_numbers(result, OPTIONS, list(enumerate(lines, 1)), self.index, user_said=self.user_said)
        self.check_synthetic_marks(OPTIONS, lines)
        if self.cost_rules:
            self.check_gaps_shown(sections)

    def check_option_fields(self, option):
        empty = [name for name in OPTION_TEXT_FIELDS if not option[name].strip()]
        empty += [name for name in OPTION_LIST_FIELDS
                  if not option[name] or not all(item.strip() for item in option[name])]
        if empty:
            self.result.problem("OPTION_FIELD_EMPTY", OPTION_REGISTRY, option["id"], ", ".join(empty))
        check_numbers(self.result, OPTION_REGISTRY, list(enumerate(option["economics"].splitlines(), 1)),
                      self.index, option["id"], option["basis"], self.user_said)

    def is_computed(self, calc_id) -> bool:
        record = self.index.get(calc_id)
        return (isinstance(record, dict) and prefix_of(calc_id) == "CALC" and is_active(calc_id, record)
                and record["result"]["status"] == "computed")

    def check_option_cost(self, option):
        result, cost = self.result, option.get("cost")
        if not isinstance(cost, dict):
            result.problem("OPTION_COST_MISSING", OPTION_REGISTRY, option["id"],
                           "no cost: write cost as {status: estimated, calc_ids: [computed CALC], note}, "
                           "{status: none, note} or {status: not_estimated, reason, question_id of a Q that "
                           "is partial or blocked and has a gap}")
            return
        if cost["status"] == "estimated":
            for calc_id in cost["calc_ids"]:
                if not self.is_computed(calc_id):
                    result.problem("OPTION_COST_UNFOUNDED", OPTION_REGISTRY, option["id"],
                                   f"cost.calc_ids: {calc_id} is not a computed active calculation; list only "
                                   "calculations with the status computed, or use not_estimated with a question")
                elif calc_id not in option["basis"]:
                    result.problem("OPTION_COST_UNFOUNDED", OPTION_REGISTRY, option["id"],
                                   f"cost.calc_ids: {calc_id} is not in basis; add it to the basis of the option")
        elif cost["status"] == "not_estimated":
            question = self.index.get(cost["question_id"])
            if not (isinstance(question, dict) and prefix_of(cost["question_id"]) == "Q"
                    and is_active(cost["question_id"], question) and question["status"] in ("partial", "blocked")
                    and isinstance(question["gap"], str) and question["gap"].strip()):
                result.problem("OPTION_COST_GAP", OPTION_REGISTRY, option["id"],
                               f"cost.question_id: {cost['question_id']} must be an active question with the "
                               "status partial or blocked and a non-empty gap that names what is missing")

    def check_option_limit(self, option):
        result, limit, cost = self.result, option.get("limit_check"), option.get("cost")
        if not isinstance(limit, dict):
            result.problem("OPTION_LIMIT_MISSING", OPTION_REGISTRY, option["id"],
                           "no limit_check: write limit_check as {brief_decision: the current brief decision "
                           "or null if there is none, result: fits, exceeds, unknown or no_limit, calc_id for "
                           "exceeds and for fits unless cost.status is none, note for unknown and no_limit}")
            return
        current = self.brief_decision_id()
        if limit["brief_decision"] != current:
            waiting = self.active_decision("brief")
            if waiting is not None and waiting["status"] != "chosen":
                note = (f"limit_check.brief_decision is {limit['brief_decision']}, and the brief decision "
                        f"{waiting['id']} is not chosen yet (status {waiting['status']}): the change of the brief "
                        "waits for the person's confirmation; leave the checks as they are for now and redo them "
                        "against the new decision after the person confirms")
            else:
                note = (f"limit_check.brief_decision is {limit['brief_decision']}, the current chosen brief "
                        f"decision is {current}: redo the check against it and write its ID in brief_decision")
            result.problem("OPTION_LIMIT_STALE", OPTION_REGISTRY, option["id"], note)
        if limit["result"] in ("fits", "exceeds"):
            if isinstance(cost, dict) and cost["status"] == "not_estimated":
                result.problem("OPTION_LIMIT_UNFOUNDED", OPTION_REGISTRY, option["id"],
                               f"limit_check.result {limit['result']} needs a cost: cost.status is not_estimated, "
                               "so the result must be unknown or no_limit")
            elif limit["result"] == "exceeds" and isinstance(cost, dict) and cost["status"] == "none":
                result.problem("OPTION_LIMIT_UNFOUNDED", OPTION_REGISTRY, option["id"],
                               "limit_check.result exceeds needs a cost: cost.status is none, nothing can exceed "
                               "the limit; use fits without calc_id")
            elif "calc_id" not in limit:
                if not (isinstance(cost, dict) and cost["status"] == "none"):
                    result.problem("OPTION_LIMIT_UNFOUNDED", OPTION_REGISTRY, option["id"],
                                   "limit_check.calc_id: fits without a calculation is only allowed when "
                                   "cost.status is none; otherwise add the calc_id of the comparison")
            elif not self.is_computed(limit["calc_id"]):
                result.problem("OPTION_LIMIT_UNFOUNDED", OPTION_REGISTRY, option["id"],
                               f"limit_check.calc_id: {limit['calc_id']} is not a computed active calculation; "
                               f"{limit['result']} needs a computed comparison of the cost with the limit")
            elif limit["calc_id"] not in option["basis"]:
                result.problem("OPTION_LIMIT_UNFOUNDED", OPTION_REGISTRY, option["id"],
                               f"limit_check.calc_id: {limit['calc_id']} is not in basis; add it to the basis "
                               "of the option")
        if isinstance(cost, dict):
            self.check_option_compared(option)

    def check_option_compared(self, option):
        """What the comparison with the limit covered: stated by the agent, and a claimed coverage of lost revenue needs a link."""
        limit, cost = option["limit_check"], option["cost"]
        if limit["result"] not in ("fits", "exceeds") or cost["status"] == "not_estimated":
            return  # nothing was compared (the second case is OPTION_LIMIT_UNFOUNDED)
        if cost["status"] == "none" and "calc_id" not in limit:
            return
        compared = limit.get("compared")
        if compared is None:
            self.result.problem("OPTION_LIMIT_COMPARED_MISSING", OPTION_REGISTRY, option["id"],
                                "limit_check.compared is missing: recompute the comparison per the section "
                                "«Варианты», item 2, and write what was compared: direct_costs (direct spending, "
                                "including discounts and compensations paid from the budget) or "
                                "direct_costs_and_lost_revenue (only when the limit is known to cover lost revenue)")
        elif compared == "direct_costs_and_lost_revenue":
            note = limit.get("note", "")
            linked = BRIEF_REF in note or any(prefix_of(ref) in ("CLM", "SRC") for ref in references(note))
            if not linked:
                self.result.problem("OPTION_LIMIT_COVERAGE_UNFOUNDED", OPTION_REGISTRY, option["id"],
                                    "limit_check.note does not show that the limit covers lost revenue: link "
                                    f"where it comes from ({BRIEF_REF}, [CLM-…] or [SRC-…]), or recompute the "
                                    "comparison per the section «Варианты», item 2, with compared: direct_costs")

    def brief_decision_id(self):
        """ID of the active decision on the brief that the person has chosen; None when there is none."""
        decision = self.active_decision("brief")
        return decision["id"] if decision is not None and decision["status"] == "chosen" else None

    def check_gaps_shown(self, sections):
        shown = {ref for _, line in sections.get(GAPS_SECTION, []) for ref in references(line)}
        for option in self.options:
            cost = option.get("cost")
            if isinstance(cost, dict) and cost["status"] == "not_estimated" and cost["question_id"] not in shown:
                self.result.problem("OPTION_GAP_NOT_SHOWN", OPTIONS, option["id"],
                                    f"cost.question_id {cost['question_id']} is not named in the section "
                                    f"«{GAPS_SECTION}»: add [{cost['question_id']}] there")

    def warn_about_cost(self):
        develop = [o for o in self.options if o["strategy_kind"] == "develop"]
        if develop and not any(isinstance(o.get("cost"), dict) and o["cost"]["status"] == "estimated"
                               for o in develop):
            self.result.warning("OPTIONS_COST_UNKNOWN", OPTION_REGISTRY, None,
                                "no develop option has an estimated cost")
        chosen = self.expected_option()
        for option in self.options:
            limit = option.get("limit_check")
            if option["id"] == chosen and isinstance(limit, dict) and limit["result"] == "exceeds":
                self.result.warning("CHOICE_EXCEEDS_LIMIT", OPTION_REGISTRY, option["id"],
                                    "the proposed or chosen option exceeds the limit of the brief; say so to the person")

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
        if self.cost_rules:
            self.check_threshold_basis()
        self.check_team()

    def check_threshold_basis(self):
        body = split_sections(self.lines_of(PLAN) or []).get(SUCCESS_SECTION, [])
        if has_text(body) and not any(prefix_of(ref) in ("CALC", "Q") for _, line in body for ref in references(line)):
            self.result.problem("THRESHOLD_BASIS_MISSING", PLAN, None,
                                f"the section «{SUCCESS_SECTION}» has no link to a calculation of the spread of "
                                "the indicator [CALC-…] or to the question about the missing periods [Q-…]")

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
        elif not self.cited(PLAN, thresholds["id"]):
            result.problem("THRESHOLDS_NOT_CITED", PLAN, thresholds["id"],
                           "the validation plan does not cite the current thresholds decision")
        self.check_commitments()
        self.check_revisions()
        for record_id, record in self.index.items():
            if prefix_of(record_id) == "ACT" and isinstance(record, dict) and record.get("end") is None:
                result.problem("ACTIONS_OPEN", "state/actions.jsonl", record_id, "started and not finished")

    def check_commitments(self):
        result = self.result
        self.check_output(COMMITMENTS, SECTIONS[COMMITMENTS])
        lines = self.lines_of(COMMITMENTS)
        if lines is not None:
            sections = split_sections(lines)
            if ACTION_SECTION in sections:
                self.check_actions(sections[ACTION_SECTION])
            for number, line in sections.get(REVIEW_SECTION, []):
                if line.strip() and not HEADING_RE.match(line) and not references(line):
                    result.problem("REVIEW_UNLINKED", COMMITMENTS, None, "a condition without a link", number)
        decision = self.active_decision("commitments")
        if decision is None or decision["status"] != "chosen":
            result.problem("COMMITMENTS_PENDING", DECISION_REGISTRY, None,
                           "the person has not agreed the commitments")
        elif self.is_before_direction(decision):
            result.problem("COMMITMENTS_PENDING", DECISION_REGISTRY, decision["id"],
                           "the commitments were agreed before the current choice of direction")
        elif self.is_before_team(decision):
            result.problem("COMMITMENTS_PENDING", DECISION_REGISTRY, decision["id"],
                           "the commitments were agreed before the current team in team.md")
        elif lines is not None and not any(decision["id"] in references(line) for _, line
                                           in split_sections(lines).get(APPROVAL_SECTION, [])):
            result.problem("COMMITMENTS_NOT_CITED", COMMITMENTS, decision["id"],
                           "the section «Согласование» does not cite the current commitments decision")

    def check_team(self):
        """The roster file and the owners of the actions; runs at validation_plan and done."""
        result = self.result
        lines = self.lines_of(COMMITMENTS)
        roster = read_team(self.root)
        if roster is None:
            if lines is not None:
                result.warning("TEAM_NOT_ASKED", TEAM, None, "commitments exist and the team was not asked")
            return
        problems = list(roster.problems)
        if roster.action_id is not None:
            action = self.index.get(roster.action_id)
            end = action.get("end") if isinstance(action, dict) else None
            kind = action["start"]["kind"] if isinstance(action, dict) and action.get("start") else None
            if kind != "human_input" or not isinstance(end, dict) or end.get("outcome") != "completed":
                problems.append(f"{roster.action_id} is not a completed human_input action")
        for note in problems:
            result.problem("TEAM_FORMAT", TEAM, None, note)
        if problems or roster.members is None or lines is None:
            return
        names = {name for name, _ in roster.members}
        body = split_sections(lines).get(ACTION_SECTION, [])
        rows = [(number, [cell.strip() for cell in line.strip().strip("|").split("|")])
                for number, line in body if line.lstrip().startswith("|")]
        if not rows or rows[0][1] != ACTION_COLUMNS:
            return
        for number, cells in rows[1:]:
            if len(cells) != len(ACTION_COLUMNS) or all(set(cell) <= set(":- ") for cell in cells):
                continue
            unknown = [name for name in owners_of(cells[1]) if name not in names]
            if unknown:
                result.problem("TEAM_UNKNOWN_OWNER", COMMITMENTS, None,
                               "not in team.md: " + ", ".join(unknown), number)

    def check_actions(self, body):
        rows = [(number, [cell.strip() for cell in line.strip().strip("|").split("|")])
                for number, line in body if line.lstrip().startswith("|")]
        if not rows or rows[0][1] != ACTION_COLUMNS:
            self.result.problem("COMMITMENTS_TABLE", COMMITMENTS, None, "columns: " + ", ".join(ACTION_COLUMNS))
            return
        rows = [row for row in rows[1:] if not all(set(cell) <= set(":- ") for cell in row[1])]
        if not rows:
            self.result.problem("COMMITMENTS_TABLE", COMMITMENTS, None, "the table has no rows")
        for number, cells in rows:
            if len(cells) != len(ACTION_COLUMNS) or not all(cells) or not references(cells[-1]):
                self.result.problem("COMMITMENTS_TABLE", COMMITMENTS, None,
                                    "a row needs all five cells and a link in the basis", number)

    def check_revisions(self):
        result = self.result
        revisions = [d for d in self.all_decisions if d["topic"] == "revision" and d["status"] in ("chosen", "superseded")]
        text = read_output(self.root, REVISIONS)
        if not revisions and text is None:
            return
        if text is None:
            for decision in revisions:
                result.problem("REVISION_UNRECORDED", REVISIONS, decision["id"], "no revision log")
            return
        entries, problems = parse_revisions("\n".join(text))
        for item in problems:
            result.problem(item["code"], REVISIONS, None, item["note"], item["line"])
        for decision in revisions:
            if sum(1 for e in entries if e["choice"] == decision["id"]) != 1:
                result.problem("REVISION_UNRECORDED", REVISIONS, decision["id"],
                               "the decision must be in the choice of exactly one entry")
        for entry in entries:
            folder = self.root / HISTORY / str(entry["number"])
            if entry["choice"] and not (folder.is_dir() and any(path.is_file() for path in folder.iterdir())):
                result.problem("REVISION_NO_SNAPSHOT", f"{HISTORY}/{entry['number']}", entry["choice"],
                               "no copy of the previous outputs", entry["line"])
        by_id = {d["id"]: d for d in revisions}
        for file, topic in (("validation-plan.md", "thresholds"), ("commitments.md", "commitments")):
            touching = [by_id[e["choice"]] for e in entries if e["choice"] in by_id and file in e["touched"]]
            current = self.active_decision(topic)
            if not touching or current is None or current["status"] != "chosen":
                continue
            latest = max(datetime.fromisoformat(d["choice"]["chosen_at"]) for d in touching)
            if datetime.fromisoformat(current["choice"]["chosen_at"]) < latest:
                result.problem("REAPPROVAL_PENDING", DECISION_REGISTRY, current["id"],
                               f"the {topic} were agreed before the last revision that touched {file}")

    def cited(self, file, record_id) -> bool:
        lines = self.lines_of(file) or []
        return any(record_id in references(line) for line in lines)

    def is_before_direction(self, thresholds) -> bool:
        choice = self.direction.get("choice") if self.direction else None
        if not choice:
            return False
        return (datetime.fromisoformat(thresholds["choice"]["chosen_at"])
                < datetime.fromisoformat(choice["chosen_at"]))

    def is_before_team(self, decision) -> bool:
        """A roster written after the consent: the agent had to ask for the commitments again."""
        roster = read_team(self.root)
        if roster is None or roster.action_id is None:
            return False
        choice = decision.get("choice") or {}
        if roster.action_id == choice.get("action_id") or not choice.get("chosen_at"):
            return False
        action = self.index.get(roster.action_id)
        end = action.get("end") if isinstance(action, dict) else None
        if not isinstance(end, dict) or not end.get("at"):
            return False
        return datetime.fromisoformat(choice["chosen_at"]) < datetime.fromisoformat(end["at"])

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
        check_numbers(self.result, file, list(enumerate(lines, 1)), self.index, user_said=self.user_said)
        self.check_synthetic_marks(file, lines)
        sections = split_sections(lines)
        for name in LINKED_SECTIONS.get(file, ()):
            body = sections.get(name, [])
            if body and not any(references(line) or ASSUMPTION in line or BRIEF_REF in line for _, line in body):
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
        budget = load_limits()["max_output_chars"]
        used = 0
        for number, error in enumerate(report.errors):
            item = entry(error.code, error.file, error.record_id, error.message, None)
            used += len(json.dumps(item, ensure_ascii=False)) + 2
            if number and used > budget:
                hidden = len(report.errors) - number
                result.problem("VALIDATOR_ERRORS_NOT_SHOWN", None, None,
                               f"{hidden} validator errors not shown; tools/validate_project.py lists them all")
                break
            result.problems.append(item)
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
