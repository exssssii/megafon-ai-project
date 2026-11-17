"""Check the answers of a respondent talk against the form rules of the role. Reads only.

Commands (run from the repository root):
  check <case folder> <talk path relative to the case folder>

The first output line is a JSON report with codes, question numbers and a short detail
(a listed phrase, a number or a sentence count), never the text of an answer. Exit codes:
0 passed, 1 violations found, 4 refused, 5 internal failure. A refusal never means "passed".
This is a check of the form only: it does not see playing along with the question, invented
circumstances without numbers, or an answer that ignores the card.
"""
import argparse
import json
import re
import sys
from pathlib import Path

from calc_engine import CalcError, number_matches, same_number
from checkpoint import Parser, Refused, UsageError

EXIT_OK, EXIT_VIOLATIONS, EXIT_REFUSED, EXIT_INTERNAL = 0, 1, 4, 5

MAX_SENTENCES = 6  # the role allows four; counting by punctuation is rough, a false fail costs a model call

# Phrases are compared lowercase, with "ё" written as "е", on word boundaries.
# Role rule 7 (the answer is short and real): a failed load is not an answer.
NOT_LOADED_PHRASES = ("роль не загружена",)
# Role rule 5 (speaks only for the persona, not for other people).
SPEAKS_FOR_OTHERS_PHRASES = (
    "все так считают", "все так думают", "все считают", "большинству клиентов", "большинство клиентов",
    "клиентам это нужно", "клиентам нужно", "людям это нужно", "людям нужно",
)
# Role rules 6 and 7 (stays the person: no AI voice, no reference to the card, no advice).
OUT_OF_ROLE_PHRASES = (
    "как ии", "как языковая модель", "в карточке", "согласно карточке", "советую вам", "рекомендую вам",
)

HEADER_RE = re.compile(r"^Персонаж:\s*(.+?),\s*карточка\s+(\S.*?)\s*$", re.MULTILINE)
QUESTION_RE = re.compile(r"^##[ \t]+Вопрос[ \t]+(\d+)[ \t]*$", re.MULTILINE)
SECTION_RE = re.compile(r"^##[ \t]+(.+?)[ \t]*$", re.MULTILINE)
# A terminator ends a sentence only at the end of text or before whitespace and a capital, digit, quote or dash.
SENTENCE_END_RE = re.compile(r"[.!?…]+(?=\s*\Z|\s+[A-ZА-ЯЁ0-9\"'«“„(—–\-])")
YEAR_RE = re.compile(r"(?:19|20)\d{2}")
UNIT_AFTER_RE = re.compile(r"\s*(?:%|₽|\$|€|руб|тыс|млн|млрд|шт|коп|долл|евро|человек|клиент|книг|раз)", re.IGNORECASE)
BRACKET_RE = re.compile(r"\[[^\]]*\]")


def fold(text: str) -> str:
    return text.lower().replace("ё", "е")


def phrase_regex(phrase: str) -> re.Pattern:
    return re.compile(r"(?<!\w)" + r"\s+".join(re.escape(word) for word in phrase.split()) + r"(?!\w)")


def find_phrase(text: str, phrases: tuple) -> str | None:
    folded = fold(text)
    for phrase in phrases:
        if phrase_regex(phrase).search(folded):
            return phrase
    return None


def count_sentences(text: str) -> int:
    return sum(1 for part in SENTENCE_END_RE.split(text) if any(char.isalnum() for char in part))


def numbers_of(text: str) -> list:
    """Pairs (absolute value, written form) of the numbers of running text; bracketed IDs are skipped."""
    text = BRACKET_RE.sub(" ", text)
    try:
        matches = number_matches(text)
    except CalcError:  # a dash before a number: only the size matters here, not the sign
        matches = number_matches(text.replace("–", " "))
    result = []
    for value, match in matches:
        digits = match[2]
        if YEAR_RE.fullmatch(digits) and not UNIT_AFTER_RE.match(text, match.end()):
            continue  # a year, not a fact that needs a source
        result.append((abs(value), match[0].strip().lstrip("+-−").strip()))
    return result


def is_counted(value) -> bool:
    """Single-digit integers are speech ("once in 2 weeks"), not facts that need a source."""
    try:
        return not (float(value).is_integer() and value < 10)
    except OverflowError:  # an integer too large for a float is certainly not a small one
        return True


def read_text(path: Path, reason: str, message: str) -> str:
    try:
        return path.read_text(encoding="utf-8-sig")
    except (OSError, UnicodeDecodeError):
        raise Refused(reason, message) from None


def inside(root: Path, path: Path) -> bool:
    return path == root or root in path.parents


def card_text(card: str, persona: str) -> tuple[str, str]:
    """The card text the numbers are checked against, and its scope: "section" or "file"."""
    headings = list(SECTION_RE.finditer(card))
    own = [index for index, found in enumerate(headings) if fold(found[1]) == fold(persona)]
    if len(own) != 1:
        return card, "file"
    index = own[0]
    end = headings[index + 1].start() if index + 1 < len(headings) else len(card)
    return card[headings[index].end():end], "section"


def split_talk(text: str) -> tuple[str, list]:
    questions = list(QUESTION_RE.finditer(text))
    if not questions:
        raise Refused("talk_malformed", "в беседе нет ни одного раздела «## Вопрос N»")
    sections = []
    for index, found in enumerate(questions):
        end = questions[index + 1].start() if index + 1 < len(questions) else len(text)
        sections.append((int(found[1]), text[found.end():end]))
    return text[:questions[0].start()], sections


def split_section(body: str) -> tuple[str, str]:
    """The first paragraph is the question, everything after it is the answer."""
    paragraphs = re.split(r"\n[ \t]*\n", body.strip(), maxsplit=1)
    question = paragraphs[0].strip()
    answer = paragraphs[1].strip() if len(paragraphs) > 1 else ""
    return question, answer


def check_answer(number: int, question: str, answer: str, allowed: list) -> list:
    if not answer.strip():
        return [{"code": "ANSWER_MISSING", "question": number, "detail": "нет ответа"}]
    found = []

    def add(code: str, detail: str):
        found.append({"code": code, "question": number, "detail": detail})

    whole = question + "\n\n" + answer
    phrase = find_phrase(whole, NOT_LOADED_PHRASES)
    if phrase:
        add("ROLE_NOT_LOADED", phrase)
    sentences = count_sentences(answer)
    if sentences > MAX_SENTENCES:
        add("TOO_LONG", f"{sentences} предложений")
    known = allowed + [value for value, _ in numbers_of(question)]
    for value, written in numbers_of(answer):
        if is_counted(value) and not any(same_number(value, other) for other in known):
            add("FOREIGN_NUMBER", written)
            break
    phrase = find_phrase(answer, SPEAKS_FOR_OTHERS_PHRASES)
    if phrase:
        add("SPEAKS_FOR_OTHERS", phrase)
    phrase = find_phrase(whole, OUT_OF_ROLE_PHRASES)
    if phrase:
        add("OUT_OF_ROLE", phrase)
    return found


def cmd_check(args) -> int:
    root = args.case.resolve()
    if not root.is_dir():
        raise Refused("talk_unreadable", "папка кейса не найдена")
    talk_path = (root / args.talk).resolve()
    if not inside(root, talk_path):
        raise Refused("outside_case", "беседа лежит вне папки кейса")
    talk = read_text(talk_path, "talk_unreadable", "беседа не читается как UTF-8")
    header, sections = split_talk(talk)
    persona = HEADER_RE.search(header)
    if not persona:
        raise Refused("talk_malformed", "в шапке нет строки «Персонаж: <имя>, карточка <путь>»")
    name, card_ref = persona[1].strip(), persona[2].strip()
    card_path = (root / card_ref).resolve()
    if not inside(root, card_path):
        raise Refused("persona_card_missing", "карточка лежит вне папки кейса")
    card = read_text(card_path, "persona_card_missing", "файл карточек не найден или не читается")
    scoped, scope = card_text(card, name)
    allowed = [value for value, _ in numbers_of(scoped)]
    violations = []
    for number, body in sections:
        question, answer = split_section(body)
        violations += check_answer(number, question, answer, allowed)
    print(json.dumps({"status": "failed" if violations else "passed", "persona": name, "card_scope": scope,
                      "checked": len(sections), "violations": violations}, ensure_ascii=False))
    return EXIT_VIOLATIONS if violations else EXIT_OK


def build_parser() -> Parser:
    parser = Parser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    commands = parser.add_subparsers(dest="command", required=True, parser_class=Parser)
    check = commands.add_parser("check")
    check.add_argument("case", type=Path)
    check.add_argument("talk", type=Path)
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
    except Exception as error:  # noqa: BLE001 - the text of an exception could quote the talk
        print(json.dumps({"status": "internal_error", "error_type": type(error).__name__}))
        return EXIT_INTERNAL


if __name__ == "__main__":
    raise SystemExit(main())
