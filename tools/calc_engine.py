"""Arithmetic over case materials: numbers, input values from extracted blocks, a restricted formula.

Shared by tools/calc.py and the case validator so that both compute the same way.
Nothing here prints. Errors carry a code, cell addresses and input names, never material text.
"""
import ast
import keyword
import math
import re
import unicodedata
from decimal import ROUND_HALF_UP, Decimal, DecimalException, InvalidOperation, localcontext

from evidence_blocks import matching_blocks, parse_range, quote_in

AGGREGATES = ("value", "sum", "mean", "min", "max", "count")
TABLE_TYPES = frozenset({"csv", "xlsx"})
TEXT_TYPES = frozenset({"md", "txt", "docx", "pdf", "web"})
MAX_FORMULA_CHARS = 500
MAX_FORMULA_NODES = 200
DEFAULT_DIGITS = 6
MAX_DIGITS = 10
INPUT_DIGITS = 10
# Arithmetic runs in decimals with this many significant digits: 0.3 - 0.1 - 0.2 is exactly zero.
PRECISION = 60
MAX_ABS = Decimal("1e300")
MAX_LISTED_CELLS = 20
COLUMN_LETTERS = re.compile(r"[A-Z]{1,3}")

_GROUPED = r"\d{1,3}(?:[   ]\d{3})+"
_DIGITS = rf"(?:{_GROUPED}|\d+)(?:[.,]\d+)?"
CELL_NUMBER = re.compile(rf"([+\-−]?)({_DIGITS})\s*%?")
# A number inside running text: not glued to a word, a digit or a decimal separator on either side.
# A sign counts only at the start or after a space or an opening bracket.
TEXT_NUMBER = re.compile(rf"(?<![\w.,])((?:(?<![^\s(])[+\-−])?)({_DIGITS})(?![.,]?\d)(?!\w)")


class CalcError(Exception):
    """A refusal with a stable code; details hold addresses and names only."""

    def __init__(self, code: str, message: str, **details):
        super().__init__(message)
        self.code = code
        self.details = details


# --- numbers ----------------------------------------------------------------

def _to_number(sign: str, digits: str):
    plain = re.sub(r"[   ]", "", digits).replace(",", ".")
    value = float(plain) if "." in plain else int(plain)
    return -value if sign in ("-", "−") else value


def parse_cell_text(text: str):
    """A number written as text in a cell; None when the text is not exactly one number."""
    found = CELL_NUMBER.fullmatch(text.strip())
    return _to_number(found[1], found[2]) if found else None


def numbers_in_text(text: str) -> list:
    """Every number of running text, in order.

    A dash (U+2013) right before a number that does not follow another number may be a minus written
    as a dash: the sign cannot be read for sure, so this is a refusal, not a positive number.
    """
    return [number for number, _ in number_matches(text)]


def number_matches(text: str) -> list:
    """Pairs (number, regex match) of running text, in order; refuses like numbers_in_text."""
    found = []
    for match in TEXT_NUMBER.finditer(text):
        start = match.start()
        if not match[1] and start > 0 and text[start - 1] == "–" and not _digit_before(text, start - 1):
            raise CalcError("ambiguous_sign", "перед числом тире: неясно, минус это или нет; возьмите цитату без него")
        found.append((_to_number(match[1], match[2]), match))
    return found


def _digit_before(text: str, index: int) -> bool:
    """A digit right before `index`, spaces skipped: a dash between two numbers is a range, not a sign."""
    index -= 1
    while index >= 0 and text[index] in "   ":
        index -= 1
    return index >= 0 and text[index].isdigit()


def is_number(value) -> bool:
    """A finite int or float; an int too large for a float is not one."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    try:
        return math.isfinite(value)
    except OverflowError:
        return False


def to_decimal(value) -> Decimal:
    """Exact decimal form of a stored number: a float goes through its shortest representation."""
    if isinstance(value, Decimal):
        return value
    return Decimal(value) if isinstance(value, int) else Decimal(repr(float(value)))


def round_half_up(value, digits: int):
    """Round half away from zero; an integral result becomes int. Refuses a value beyond MAX_ABS."""
    try:
        exact = to_decimal(value)
        if not exact.is_finite() or abs(exact) > MAX_ABS:
            raise CalcError("overflow", "результат не является конечным числом допустимой величины")
        with localcontext() as context:
            context.prec = max(PRECISION, exact.adjusted() + digits + 3)
            rounded = exact.quantize(Decimal(1).scaleb(-digits), rounding=ROUND_HALF_UP)
    except (InvalidOperation, OverflowError, ValueError) as error:
        raise CalcError("overflow", "результат не является конечным числом допустимой величины") from error
    if rounded == rounded.to_integral_value():
        return int(rounded)
    return float(rounded)


def same_number(left, right) -> bool:
    if not (is_number(left) and is_number(right)):
        return False
    return math.isclose(left, right, rel_tol=1e-9, abs_tol=1e-12)


# --- inputs from blocks ----------------------------------------------------------

def aggregate(values: list, how: str):
    if how == "value":
        if len(values) != 1:
            raise CalcError("aggregate_value_many", "свёртка value берёт ровно одну ячейку", cells=len(values))
        return round_half_up(values[0], INPUT_DIGITS)
    exact = [to_decimal(value) for value in values]
    with localcontext() as context:
        context.prec = PRECISION
        if how == "sum":
            result = sum(exact, Decimal(0))
        elif how == "mean":
            result = sum(exact, Decimal(0)) / len(exact)
        elif how == "min":
            result = min(exact)
        elif how == "max":
            result = max(exact)
        else:
            result = Decimal(len(exact))
    return round_half_up(result, INPUT_DIGITS)


def _cell_number(raw, address: str, problems: dict):
    if raw is None:
        problems.setdefault("empty", []).append(address)
        return None
    if isinstance(raw, bool):
        problems.setdefault("not_number", []).append(address)
        return None
    if isinstance(raw, (int, float)):
        if not math.isfinite(raw):
            problems.setdefault("not_number", []).append(address)
            return None
        return raw
    if isinstance(raw, str):
        if not raw.strip():
            problems.setdefault("empty", []).append(address)
            return None
        value = parse_cell_text(raw)
        if value is None:
            problems.setdefault("not_number", []).append(address)
        return value
    problems.setdefault("not_number", []).append(address)
    return None


def _listed(cells: list[str]) -> dict:
    return {"cells": cells[:MAX_LISTED_CELLS], "count": len(cells)}


def _refuse_problems(problems: dict) -> None:
    if problems.get("formula_no_value"):
        raise CalcError("formula_no_value", "в диапазоне формула без сохранённого значения",
                        **_listed(problems["formula_no_value"]))
    if problems.get("empty"):
        raise CalcError("empty_cell", "в диапазоне пустые ячейки: разбейте диапазон вокруг них и назовите пробел",
                        **_listed(problems["empty"]))
    if problems.get("not_number"):
        raise CalcError("not_number", "в диапазоне есть значения, которые не являются числом",
                        **_listed(problems["not_number"]))


def _refuse_missing_rows(rows: dict, first_row: int, last_row: int, address) -> None:
    """A range longer than the rows the material has: refuse naming the first gaps, without walking all of it."""
    present = sum(1 for row in rows if first_row <= row <= last_row)
    if present == last_row - first_row + 1:
        return
    missing, row = [], first_row
    while len(missing) < MAX_LISTED_CELLS and row <= last_row:
        if row not in rows:
            missing.append(address(row))
        row += 1
    raise CalcError("empty_cell", "в диапазоне пустые ячейки: разбейте диапазон вокруг них и назовите пробел",
                    cells=missing, count=last_row - first_row + 1 - present)


def xlsx_values(blocks: list[dict], sheet: str, column: str, first_row: int, last_row: int) -> list:
    """Numbers of one column, rows first_row..last_row of a sheet; refuses on any gap or non-number."""
    if not COLUMN_LETTERS.fullmatch(column or ""):
        raise CalcError("bad_column", "столбец XLSX задаётся буквами, например E")
    rows = {}
    for block in blocks:
        locator = block["locator"]
        if locator.get("kind") != "cells" or locator.get("sheet") != sheet:
            continue
        bounds = parse_range(locator.get("range"))
        if bounds and isinstance(block.get("cells"), dict):
            rows[bounds[1]] = block
    _refuse_missing_rows(rows, first_row, last_row, lambda row: f"{column}{row}")
    values, problems = [], {}
    for row in range(first_row, last_row + 1):
        address = f"{column}{row}"
        block = rows.get(row)
        cells = block["cells"] if block else {}
        if address in cells and cells[address] is None:
            problems.setdefault("formula_no_value", []).append(address)
            continue
        value = _cell_number(cells.get(address), address, problems)
        if value is not None:
            values.append(value)
    _refuse_problems(problems)
    return values


def csv_values(blocks: list[dict], column: int, first_row: int, last_row: int) -> list:
    rows = {}
    for block in blocks:
        locator = block["locator"]
        if locator.get("kind") == "rows" and isinstance(block.get("cells"), list):
            rows[locator.get("from")] = block
    _refuse_missing_rows(rows, first_row, last_row, lambda row: f"строка {row}, столбец {column}")
    values, problems = [], {}
    for row in range(first_row, last_row + 1):
        address = f"строка {row}, столбец {column}"
        block = rows.get(row)
        cells = block["cells"] if block else []
        raw = cells[column - 1] if column - 1 < len(cells) else None
        value = _cell_number(raw, address, problems)
        if value is not None:
            values.append(value)
    _refuse_problems(problems)
    return values


def csv_column(blocks: list[dict], column) -> int:
    """Column number from a 1-based number or the exact header of the first row."""
    if isinstance(column, int) and not isinstance(column, bool):
        if column < 1:
            raise CalcError("bad_column", "номер столбца CSV начинается с 1")
        return column
    if not isinstance(column, str) or not column.strip():
        raise CalcError("bad_column", "столбец CSV задаётся номером с 1 либо заголовком первой строки")
    header = next((b for b in blocks if b["locator"].get("kind") == "rows" and b["locator"].get("from") == 1), None)
    cells = header.get("cells") if header else None
    if not isinstance(cells, list):
        raise CalcError("bad_column", "у таблицы нет первой строки с заголовками")
    found = [index for index, cell in enumerate(cells, 1) if isinstance(cell, str) and cell.strip() == column.strip()]
    if len(found) != 1:
        raise CalcError("bad_column", "заголовок должен встретиться в первой строке ровно один раз", matches=len(found))
    return found[0]


def table_input(source_type: str, blocks: list[dict], locator: dict, column, how: str) -> dict:
    """Value of a table input whose locator is already known (the validator path)."""
    if how not in AGGREGATES:
        raise CalcError("bad_aggregate", "неизвестная свёртка")
    if source_type == "xlsx":
        if locator.get("kind") != "cells":
            raise CalcError("bad_locator", "вход XLSX указывает диапазон ячеек")
        bounds = parse_range(locator.get("range"))
        if bounds is None or bounds[0] != bounds[2]:
            raise CalcError("bad_locator", "диапазон входа должен лежать в одном столбце")
        letters = re.match(r"[A-Z]+", locator["range"])[0]
        values = xlsx_values(blocks, locator.get("sheet"), letters, bounds[1], bounds[3])
    elif source_type == "csv":
        if locator.get("kind") != "rows" or not isinstance(column, int) or isinstance(column, bool) or column < 1:
            raise CalcError("bad_locator", "вход CSV указывает строки и номер столбца")
        values = csv_values(blocks, column, locator["from"], locator["to"])
    else:
        raise CalcError("bad_locator", "свёртка допустима только для таблиц CSV и XLSX")
    if not values:
        raise CalcError("empty_cell", "в диапазоне нет ни одной ячейки")
    return {"value": aggregate(values, how), "cells": len(values)}


def text_input(blocks: list[dict], locator: dict) -> dict:
    """Value of a quoted number; the locator holds the block place and the quote."""
    quote = locator.get("quote")
    if not isinstance(quote, str) or not quote.strip():
        raise CalcError("no_quote", "числу из текста нужна цитата")
    place = {key: value for key, value in locator.items() if key != "quote"}
    found = matching_blocks(blocks, place)
    if not found:
        raise CalcError("block_missing", "локатор не указывает ни на один блок")
    if not any(quote_in(quote, block["text"]) for block in found):
        raise CalcError("quote_not_found", "цитаты нет в тексте блока")
    numbers = numbers_in_text(quote)
    if len(numbers) != 1:
        raise CalcError("quote_numbers", "в цитате должно быть ровно одно число: сократите её", numbers=len(numbers))
    return {"value": round_half_up(numbers[0], INPUT_DIGITS), "cells": 1}


# --- formula ----------------------------------------------------------------------

def valid_name(name) -> bool:
    return (isinstance(name, str) and name.isidentifier() and not name.startswith("__")
            and unicodedata.normalize("NFKC", name) == name
            and not keyword.iskeyword(name) and not keyword.issoftkeyword(name))


def unit_key(unit) -> str:
    return " ".join(unit.split()).casefold() if isinstance(unit, str) else "?"


def period_key(period) -> str | None:
    """Comparable form of a period; None for an unknown one, which neither mixes nor matches."""
    return " ".join(period.split()).casefold() if isinstance(period, str) and period.strip() else None


class Formula:
    """A parsed arithmetic expression over input names. Parsing refuses every construct outside the list."""

    OPERATORS = {ast.Add: "+", ast.Sub: "-", ast.Mult: "*", ast.Div: "/"}

    def __init__(self, text):
        if not isinstance(text, str) or not text.strip():
            raise CalcError("formula_syntax", "формула пуста")
        if len(text) > MAX_FORMULA_CHARS:
            raise CalcError("formula_too_long", "формула длиннее 500 символов")
        try:
            tree = ast.parse(text.strip(), mode="eval")
        except (SyntaxError, ValueError, RecursionError, MemoryError) as error:
            raise CalcError("formula_syntax", "формулу нельзя разобрать") from error
        nodes = list(ast.walk(tree))
        if len(nodes) > MAX_FORMULA_NODES:
            raise CalcError("formula_too_long", "в формуле слишком много действий")
        self.names = []
        for node in nodes:
            if isinstance(node, (ast.Expression, ast.Load)) or type(node) in self.OPERATORS:
                continue
            if isinstance(node, (ast.UAdd, ast.USub)):
                continue
            if isinstance(node, ast.BinOp) and type(node.op) in self.OPERATORS:
                continue
            if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.UAdd, ast.USub)):
                continue
            if isinstance(node, ast.Constant) and is_number(node.value):
                continue
            if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load):
                self.names.append(node.id)
                continue
            raise CalcError("formula_forbidden",
                            "в формуле допустимы только числа, имена входов, + - * / и скобки")
        self.tree = tree.body
        self.text = text.strip()

    def used(self) -> set[str]:
        return set(self.names)

    def mixes(self, units: dict, periods: dict) -> list[dict]:
        """Check dimensions; return period mixes that need an explanation. Refuses on unit mismatch."""
        found = []
        self._shape(self.tree, units, periods, found)
        return found

    def _shape(self, node, units, periods, found):
        """(dimension, periods, has names) of a subexpression."""
        if isinstance(node, ast.Constant):
            return {}, frozenset(), False
        if isinstance(node, ast.Name):
            key = period_key(periods[node.id])
            return {unit_key(units[node.id]): 1}, frozenset({key} if key else ()), True
        if isinstance(node, ast.UnaryOp):
            return self._shape(node.operand, units, periods, found)
        left, right = self._shape(node.left, units, periods, found), self._shape(node.right, units, periods, found)
        op = self.OPERATORS[type(node.op)]
        if op in "+-":
            if left[2] and right[2] and left[0] != right[0]:
                raise CalcError("unit_mismatch", "складывать и вычитать можно только одинаковые единицы",
                                left=ast.unparse(node.left), right=ast.unparse(node.right))
            dimension = left[0] if left[2] else right[0]
        else:
            sign = 1 if op == "*" else -1
            dimension = dict(left[0])
            for atom, power in right[0].items():
                dimension[atom] = dimension.get(atom, 0) + sign * power
            dimension = {atom: power for atom, power in dimension.items() if power}
        if op != "*" and left[1] and right[1] and left[1] != right[1]:
            found.append({"operation": op, "left": ast.unparse(node.left), "right": ast.unparse(node.right)})
        return dimension, left[1] | right[1], left[2] or right[2]

    def dimension(self, units: dict, periods: dict) -> str:
        shape = self._shape(self.tree, units, periods, [])[0]
        top = [f"{atom}^{p}" if p != 1 else atom for atom, p in sorted(shape.items()) if p > 0]
        bottom = [f"{atom}^{-p}" if p != -1 else atom for atom, p in sorted(shape.items()) if p < 0]
        if not top and not bottom:
            return "безразмерно"
        return " · ".join(top or ["1"]) + (" / " + " · ".join(bottom) if bottom else "")

    def evaluate(self, values: dict):
        """(Decimal value, None) or (None, text of the zero denominator).

        Decimals keep sums of shares exact, so a denominator such as 0.3 - 0.1 - 0.2 is zero, not 5e-17.
        """
        try:
            with localcontext() as context:
                context.prec = PRECISION
                return self._value(self.tree, values), None
        except _ZeroDenominator as zero:
            return None, zero.text
        except DecimalException as error:
            raise CalcError("overflow", "результат не является конечным числом допустимой величины") from error

    def _value(self, node, values):
        if isinstance(node, ast.Constant):
            return to_decimal(node.value)
        if isinstance(node, ast.Name):
            return to_decimal(values[node.id])
        if isinstance(node, ast.UnaryOp):
            value = self._value(node.operand, values)
            return -value if isinstance(node.op, ast.USub) else value
        left, right = self._value(node.left, values), self._value(node.right, values)
        op = self.OPERATORS[type(node.op)]
        if op == "+":
            result = left + right
        elif op == "-":
            result = left - right
        elif op == "*":
            result = left * right
        else:
            if right == 0:
                raise _ZeroDenominator(ast.unparse(node.right))
            result = left / right
        if not result.is_finite():
            raise CalcError("overflow", "результат не является конечным числом допустимой величины")
        return result


class _ZeroDenominator(Exception):
    def __init__(self, text: str):
        super().__init__(text)
        self.text = text


def finish_value(raw, digits: int):
    """The stored number: rounded, int when whole; a value beyond MAX_ABS is a refusal."""
    return round_half_up(raw, digits)


# --- recompute a stored record (validator path) ---------------------------------------

def _range_column(locator: dict) -> str | None:
    found = re.match(r"[A-Z]+", locator.get("range") or "")
    return found[0] if found else None


def recompute(record: dict, source_of, blocks_of, calculation_of) -> tuple[list[str], bool]:
    """(Reasons why a stored record with a formula does not follow from its inputs, whether every input was
    rechecked against the material). No reasons and True: the record follows from the material.

    source_of(id) -> source record or None, blocks_of(source) -> blocks or None (no text to check against),
    calculation_of(id) -> calculation record or None. Callers skip records with link errors.
    An input without text keeps its stored value, so the formula and the result are still checked.
    """
    method = record["method"]
    reasons, values, checked = [], {}, True
    for number, item in enumerate(record["inputs"], 1):
        name = item["name"]
        if "calculation_id" in item:
            upstream = calculation_of(item["calculation_id"])
            if upstream is None or upstream["result"]["status"] != "computed":
                reasons.append(f"input {number}: calculation has no computed result")
                continue
            result = upstream["result"]
            if not same_number(result["value"], item["value"]) or result["unit"] != item["unit"] \
                    or result["period"] != item["period"]:
                reasons.append(f"input {number}: differs from the calculation result")
            values[name] = result["value"]
            continue
        source = source_of(item["source_id"])
        blocks = blocks_of(source) if source is not None else None
        if blocks is None:
            checked = False
            if not is_number(item["value"]):
                reasons.append(f"input {number}: no value")
            values[name] = item["value"]
            continue
        if source["type"] == "xlsx" and "column" in item and item["column"] != _range_column(item["locator"]):
            reasons.append(f"input {number}: column differs from the range")
            continue
        try:
            if "aggregate" in item:
                got = table_input(source["type"], blocks, item["locator"], item.get("column"), item["aggregate"])
            elif "quote" in item["locator"] and source["type"] in TEXT_TYPES:
                got = text_input(blocks, item["locator"])
            else:
                reasons.append(f"input {number}: no aggregate or quote to recompute it from")
                continue
        except CalcError as error:
            reasons.append(f"input {number}: {error.code}")
            continue
        if not same_number(got["value"], item["value"]):
            reasons.append(f"input {number}: value differs from the material")
        values[name] = got["value"]
    if reasons:
        return reasons, checked
    try:
        formula = Formula(method["formula"])
    except CalcError as error:
        return [f"formula: {error.code}"], checked
    if not formula.used() <= set(values):
        return ["formula: uses a name that is not an input"], checked
    digits = method.get("digits", DEFAULT_DIGITS)
    try:
        raw, zero = formula.evaluate(values)
        value = None if zero is not None else finish_value(raw, digits)
    except CalcError as error:
        return [f"formula: {error.code}"], checked
    result = record["result"]
    if zero is not None:
        if result["status"] != "not_computable":
            return ["result: denominator is zero, the record says computed"], checked
        return [], checked
    if result["status"] != "computed":
        return ["result: computable, the record says not_computable"], checked
    if not same_number(value, result["value"]):
        return ["result: value differs from the formula"], checked
    return [], checked
