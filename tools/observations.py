"""Parse the observation file an agent writes after looking at an image.

The format narrows room for invention: every observation has a place, what is seen is kept apart
from the text read word for word, and what could not be read has its own section. Nothing here
opens an image or follows the text: the file is data.
"""
from dataclasses import dataclass
import re

SCREEN_LABELS = ("целевой", "заслонён", "вход", "капча", "ошибка", "пусто", "другое")
TARGET = "целевой"
NO_TEXT_VALUES = ("нет", "неразборчиво")
LINE_NAMES = ("Где", "Видно", "Текст")
HEADING = re.compile(r"##[ \t]+(.+?)[ \t]*$")
ITEM_HEADING = re.compile(r"Наблюдение[ \t]+(\d+)")
# A quote may hold one level of quotes inside, as a Russian screen writes a name: «Тариф «Салон»».
INNER = r"«(?!\s*»)[^«»]+»"
QUOTE = rf"«(?!\s*»)(?:[^«»]|{INNER})+»"
QUOTED_LIST = re.compile(rf"(?:{QUOTE}(?:[\s,;]+|$))+")
SCREEN_LINE = re.compile(r"([^\s:—–-]+)[ \t]*[:—–-]*[ \t]*(.*)")


class FormatError(Exception):
    """The file does not follow the format; `reason` names the rule, never the text."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True)
class Item:
    index: int
    area: str
    seen: str
    text: str


@dataclass(frozen=True)
class Parsed:
    screen: str
    screen_note: str
    items: list[Item]
    unresolved: str | None


def sections(text: str) -> list[tuple[str, list[str]]]:
    found: list[tuple[str, list[str]]] = []
    for line in text.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        heading = HEADING.fullmatch(line.rstrip())
        if heading:
            found.append((heading[1].strip(), []))
        elif found:
            found[-1][1].append(line)
        elif line.strip():
            raise FormatError("текст до первого раздела")
    return found


def lines_of(body: list[str]) -> list[str]:
    return [line.strip() for line in body if line.strip()]


def parse_screen(body: list[str]) -> tuple[str, str]:
    lines = lines_of(body)
    found = SCREEN_LINE.fullmatch(lines[0]) if lines else None
    if not found or len(lines) > 2:
        raise FormatError("раздел «Экран»: нужна метка и одна строка о том, что это за экран")
    label, rest = found[1].casefold(), found[2].strip()
    if label not in SCREEN_LABELS:
        raise FormatError("раздел «Экран»: метка не из списка")
    if bool(rest) == (len(lines) == 2):
        raise FormatError("раздел «Экран»: нужна ровно одна строка о том, что это за экран")
    return label, rest or lines[1]


def parse_item(index: int, body: list[str]) -> Item:
    lines = lines_of(body)
    if len(lines) != len(LINE_NAMES):
        raise FormatError(f"наблюдение {index}: нужны ровно три строки Где, Видно, Текст")
    values = []
    for name, line in zip(LINE_NAMES, lines):
        prefix, _, value = line.partition(":")
        if prefix.strip() != name or not value.strip():
            raise FormatError(f"наблюдение {index}: строка {name}: отсутствует или пуста")
        values.append(value.strip())
    area, seen, text = values
    if text.casefold().rstrip(".") not in NO_TEXT_VALUES and not QUOTED_LIST.fullmatch(text):
        raise FormatError(f"наблюдение {index}: Текст — только нет, неразборчиво или цитаты в «» через запятую")
    return Item(index, area, seen, text)


def parse(text: str) -> Parsed:
    found = sections(text)
    if not found or found[0][0] != "Экран":
        raise FormatError("первым должен идти раздел «Экран»")
    screen, note = parse_screen(found[0][1])
    items: list[Item] = []
    unresolved: str | None = None
    closed = False
    for title, body in found[1:]:
        if closed:
            raise FormatError("после «Не разобрано» разделов быть не должно")
        number = ITEM_HEADING.fullmatch(title)
        if number:
            if int(number[1]) != len(items) + 1:
                raise FormatError("номера наблюдений должны идти подряд с единицы")
            items.append(parse_item(len(items) + 1, body))
        elif title == "Не разобрано":
            value = " ".join(" ".join(lines_of(body)).split())
            if not value:
                raise FormatError("раздел «Не разобрано» пуст: напишите нет")
            unresolved = None if value.casefold().rstrip(".") == "нет" else value
            closed = True
        else:
            raise FormatError("неизвестный раздел")
    if not closed:
        raise FormatError("нет раздела «Не разобрано»")
    if screen == TARGET and not items:
        raise FormatError("метка целевой требует хотя бы одного наблюдения")
    return Parsed(screen, note, items, unresolved)
