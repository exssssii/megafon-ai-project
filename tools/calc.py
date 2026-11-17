"""Record calculations whose numbers come from the case materials, and recheck them.

Commands: add (write), check (read only).

`add` takes a request file from sources/incoming/ of the case. Input values are never typed by
the agent: the tool sums, averages or reads the cells of the named blocks, or takes the single
number of a quote, then evaluates the formula, a plain arithmetic expression over input names.
A gap, text instead of a number, adding different units or dividing different periods without an
explanation is a refusal. A zero denominator is a result: the record is written as not computable.
The case validator then runs; a new error on the record restores the registry.

Exit codes of add: 0 written, 4 nothing written (refused, wrong call included), 5 internal failure.
Exit codes of check: 0 every checked record follows from its inputs, 1 a mismatch, 4 refused, 5 failure.
The first output line is a JSON report without material text.
"""
import argparse
import json
import re
import sys
from pathlib import Path

import calc_engine as engine
from calc_engine import CalcError
from evidence import (
    CALCULATIONS_FILE,
    Snapshot,
    baseline,
    blocks_of,
    case_findings,
    check_case,
    check_object,
    fail,
    finish,
    open_case,
    read_request,
    text_value,
    usable_source,
)
from material_extract import load_limits
import checkpoint
from read_material import (EXIT_INTERNAL, EXIT_OK, EXIT_REFUSED, Case, Parser, Refused, dumps, emit, journaled, now,
                           write_atomic)
from validate_project import validate_case

EXIT_MISMATCH = 1
CALC_ID = re.compile(r"CALC-[0-9]{4,}")
SOURCE_ID = re.compile(r"SRC-[0-9]{4,}")
CALC_FIELDS = ("id", "created_at", "record_status", "status_reason", "inputs", "method", "result", "gaps",
               "assumptions", "reproduce")
PERIOD_NOTE = "Разные периоды: "
NOT_COMPUTABLE_REPRODUCE = "Не вычисляется: причина в gaps"


# --- request ------------------------------------------------------------------

def nullable_text(value, where: str):
    return None if value is None else text_value(value, where)


def text_list(value, where: str) -> list[str]:
    if not isinstance(value, list):
        fail(where, "ожидается список строк")
    return [text_value(item, where) for item in value]


def block_number(value, where: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        fail(where, "ожидается номер блока, целое число от 1")
    return value


def parse_input(item, where: str) -> dict:
    if isinstance(item, dict) and "calculation_id" in item:
        item = check_object(item, where, {"name", "calculation_id"}, set())
        if not isinstance(item["calculation_id"], str) or not CALC_ID.fullmatch(item["calculation_id"]):
            fail(where, "calculation_id должен быть вида CALC-NNNN")
        parsed = {"calculation_id": item["calculation_id"]}
    else:
        item = check_object(item, where, {"name", "source_id", "block", "unit", "period"},
                            {"block_to", "column", "aggregate", "quote"})
        if not isinstance(item["source_id"], str) or not SOURCE_ID.fullmatch(item["source_id"]):
            fail(where, "source_id должен быть вида SRC-NNNN")
        parsed = {"source_id": item["source_id"], "block": block_number(item["block"], where + " block"),
                  "unit": nullable_text(item["unit"], where + " unit"),
                  "period": nullable_text(item["period"], where + " period")}
        parsed["block_to"] = block_number(item.get("block_to", parsed["block"]), where + " block_to")
        if "quote" in item:
            if {"column", "aggregate", "block_to"} & set(item):
                fail(where, "у числа из цитаты нет column, aggregate и block_to")
            parsed["quote"] = text_value(item["quote"], where + " quote")
        else:
            if "column" not in item:
                fail(where, "у входа из таблицы нужен column, у входа из текста — quote")
            parsed["column"] = item["column"]
            if "aggregate" in item and item["aggregate"] not in engine.AGGREGATES:
                fail(where, "aggregate: value, sum, mean, min, max или count")
            parsed["aggregate"] = item.get("aggregate")
    if not engine.valid_name(item["name"]):
        fail(where, "имя входа — идентификатор из букв, цифр и _, не с цифры")
    parsed["name"] = item["name"]
    return parsed


def parse_add(data) -> dict:
    data = check_object(data, "запрос", {"description", "inputs", "result"},
                        {"formula", "not_computable", "assumptions", "gaps", "period_mix"})
    result = check_object(data["result"], "result", {"unit", "period"}, {"digits"})
    digits = result.get("digits", engine.DEFAULT_DIGITS)
    if not isinstance(digits, int) or isinstance(digits, bool) or not 0 <= digits <= engine.MAX_DIGITS:
        fail("result digits", "ожидается целое число от 0 до 10")
    if not isinstance(data["inputs"], list):
        fail("inputs", "ожидается список")
    inputs = [parse_input(item, f"inputs[{number}]") for number, item in enumerate(data["inputs"])]
    names = [item["name"] for item in inputs]
    if len(set(names)) != len(names):
        fail("inputs", "имена входов повторяются")
    not_computable = data.get("not_computable", False)
    if not isinstance(not_computable, bool):
        fail("not_computable", "ожидается true или false")
    spec = {
        "description": text_value(data["description"], "description"),
        "inputs": inputs,
        "unit": nullable_text(result["unit"], "result unit"),
        "period": nullable_text(result["period"], "result period"),
        "digits": digits,
        "not_computable": not_computable,
        "assumptions": text_list(data.get("assumptions", []), "assumptions"),
        "gaps": text_list(data.get("gaps", []), "gaps"),
        "period_mix": nullable_text(data.get("period_mix"), "period_mix"),
    }
    if not_computable:
        if "formula" in data or spec["period_mix"] is not None or "digits" in result:
            fail("запрос", "у невычисляемого расчёта нет formula, digits и period_mix")
        if not spec["gaps"]:
            fail("gaps", "у невычисляемого расчёта назовите, каких данных нет")
    else:
        if "formula" not in data:
            fail("запрос", "нужна formula либо not_computable: true")
        if not inputs:
            fail("inputs", "формуле нужен хотя бы один вход")
        spec["formula"] = data["formula"]
    return spec


# --- inputs ------------------------------------------------------------------------

def refuse(error: CalcError, **where):
    raise Refused(str(error), code=error.code, **where, **error.details)


def table_locator(source: dict, blocks: list[dict], item: dict) -> tuple[dict, object]:
    """Locator of a column range between two blocks, and the column as stored."""
    by_number = {block["n"]: block for block in blocks}
    first, last = by_number.get(item["block"]), by_number.get(item["block_to"])
    if first is None or last is None:
        raise Refused("блока с таким номером нет", id=source["id"], name=item["name"])
    if last["n"] < first["n"]:
        raise Refused("block_to указывает на блок раньше первого", id=source["id"], name=item["name"])
    if source["type"] == "csv":
        column = engine.csv_column(blocks, item["column"])
        return {"kind": "rows", "from": first["locator"]["from"], "to": last["locator"]["to"]}, column
    sheet = first["locator"].get("sheet")
    if last["locator"].get("sheet") != sheet:
        raise Refused("block и block_to лежат на разных листах", id=source["id"], name=item["name"])
    column = item["column"]
    if not isinstance(column, str) or not engine.COLUMN_LETTERS.fullmatch(column):
        raise Refused("столбец XLSX задаётся буквами, например E", id=source["id"], name=item["name"])
    first_row = engine.parse_range(first["locator"]["range"])[1]
    last_row = engine.parse_range(last["locator"]["range"])[3]
    cells = f"{column}{first_row}" if first_row == last_row else f"{column}{first_row}:{column}{last_row}"
    return {"kind": "cells", "sheet": sheet, "range": cells}, None


def text_locator(source: dict, blocks: list[dict], item: dict) -> dict:
    block = next((b for b in blocks if b["n"] == item["block"]), None)
    if block is None:
        raise Refused("блока с таким номером нет", id=source["id"], name=item["name"])
    return dict(block["locator"]) | {"quote": item["quote"]}


def formula_cached(case: Case, source_id: str, locator: dict) -> list[str]:
    """Cells of the range whose value a spreadsheet program stored for a formula: not rechecked."""
    try:
        report = json.loads(case.extracted_path(source_id).with_suffix(".report.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    listed = report.get("formula_cached") if isinstance(report, dict) else None
    bounds = engine.parse_range(locator["range"])
    found = []
    for item in listed if isinstance(listed, list) else []:
        sheet, _, address = str(item).rpartition("!")
        cell = engine.parse_range(address)
        if sheet == locator["sheet"] and cell and bounds[0] <= cell[0] <= bounds[2] and bounds[1] <= cell[1] <= bounds[3]:
            found.append(address)
    return found


def resolve_source_input(case: Case, cache: dict, item: dict, warnings: list) -> tuple[dict, int]:
    source = usable_source(case, item["source_id"], supporting=True)
    blocks = blocks_of(case, source["id"], cache)
    stored = {"name": item["name"], "source_id": source["id"]}
    try:
        if "quote" in item:
            if source["type"] not in engine.TEXT_TYPES:
                raise Refused("из таблицы число берётся по столбцу, а не цитатой", id=source["id"], name=item["name"])
            locator = text_locator(source, blocks, item)
            got = engine.text_input(blocks, locator)
            stored["locator"] = locator
        else:
            if source["type"] not in engine.TABLE_TYPES:
                raise Refused("column и aggregate допустимы только для CSV и XLSX: из текста число берётся цитатой",
                              id=source["id"], name=item["name"])
            locator, column = table_locator(source, blocks, item)
            rows = locator["to"] - locator["from"] + 1 if locator["kind"] == "rows" else \
                engine.parse_range(locator["range"])[3] - engine.parse_range(locator["range"])[1] + 1
            how = item["aggregate"] or ("value" if rows == 1 else None)
            if how is None:
                raise Refused("для нескольких строк укажите aggregate", id=source["id"], name=item["name"])
            got = engine.table_input(source["type"], blocks, locator, column, how)
            stored["locator"] = locator
            if column is not None:
                stored["column"] = column
            stored["aggregate"] = how
            if source["type"] == "xlsx":
                cached = formula_cached(case, source["id"], locator)
                if cached:
                    warnings.append({"code": "formula_cached", "id": source["id"], "cells": cached})
    except CalcError as error:
        refuse(error, id=source["id"], name=item["name"])
    partial = {"code": "SOURCE_PARTIAL", "id": source["id"]}
    if source["extraction"]["status"] == "partial" and partial not in warnings:
        warnings.append(partial)
    return stored | {"value": got["value"], "unit": item["unit"], "period": item["period"]}, got["cells"]


def resolve_calc_input(registry: dict, item: dict) -> dict:
    upstream = next((r for r in registry["records"] if r.get("id") == item["calculation_id"]), None)
    if upstream is None:
        raise Refused("расчёт не найден", id=item["calculation_id"], name=item["name"])
    if upstream.get("record_status") != "active":
        raise Refused("расчёт отозван", id=item["calculation_id"], name=item["name"])
    result = upstream["result"]
    if result["status"] != "computed":
        raise Refused("у расчёта нет числа: зависящий от него показатель тоже не вычисляется",
                      id=item["calculation_id"], name=item["name"])
    return {"name": item["name"], "calculation_id": upstream["id"], "value": result["value"],
            "unit": result["unit"], "period": result["period"]}


# --- registry --------------------------------------------------------------------------

def load_registry(case: Case) -> dict:
    registry = case.load(CALCULATIONS_FILE)
    for record in registry["records"]:
        valid = (isinstance(record, dict) and isinstance(record.get("id"), str) and CALC_ID.fullmatch(record["id"])
                 and isinstance(record.get("inputs"), list) and isinstance(record.get("result"), dict)
                 and isinstance(record.get("method"), dict))
        if not valid:
            raise Refused("реестр расчётов не соответствует контракту: запустите валидатор")
    return registry


def next_id(registry: dict) -> str:
    numbers = [int(record["id"].split("-")[1]) for record in registry["records"]]
    return f"CALC-{max(numbers, default=0) + 1:04d}"


def canonical(record: dict) -> dict:
    ordered = {key: record[key] for key in CALC_FIELDS if key in record}
    return ordered | {key: value for key, value in record.items() if key not in ordered}


def same_calculation(existing: dict, record: dict) -> bool:
    """A repeat of the request: the same inputs, formula and result; without a formula, the same gaps too."""
    keys = ("inputs", "result") + (() if "formula" in record["method"] else ("gaps",))
    method = ("formula", "digits")
    return (existing.get("record_status") == "active" and all(existing.get(key) == record[key] for key in keys)
            and all(existing["method"].get(key) == record["method"].get(key) for key in method))


# --- commands ----------------------------------------------------------------------------

def evaluate(spec: dict, inputs: list[dict]) -> tuple[dict, list[str], str | None]:
    """Result, extra gaps and dimension of the formula over resolved inputs."""
    try:
        formula = engine.Formula(spec["formula"])
    except CalcError as error:
        refuse(error)
    names = {item["name"] for item in inputs}
    unknown = sorted(formula.used() - names)
    if unknown:
        raise Refused("в формуле имя, которого нет среди входов", code="unknown_name", names=unknown)
    unused = sorted(names - formula.used())
    if unused:
        raise Refused("вход не используется в формуле: уберите его", code="unused_input", names=unused)
    units = {item["name"]: item["unit"] for item in inputs}
    periods = {item["name"]: item["period"] for item in inputs}
    try:
        mixes = formula.mixes(units, periods)
        dimension = formula.dimension(units, periods)
        raw, zero = formula.evaluate({item["name"]: item["value"] for item in inputs})
        value = None if zero is not None else engine.finish_value(raw, spec["digits"])
    except CalcError as error:
        refuse(error)
    if mixes and spec["period_mix"] is None:
        raise Refused("действие над разными периодами: объясните в period_mix, почему это верно, либо выровняйте периоды",
                      code="period_mix", operations=mixes)
    if not mixes and spec["period_mix"] is not None:
        raise Refused("period_mix указан, но периоды не смешиваются: уберите его", code="period_mix_unused")
    if zero is not None:
        result = {"status": "not_computable", "value": None, "unit": spec["unit"], "period": spec["period"]}
        return result, [f"Деление на ноль: знаменатель {zero} равен нулю"], dimension
    return {"status": "computed", "value": value, "unit": spec["unit"], "period": spec["period"]}, [], dimension


def recorded(args, case: Case, run) -> int:
    """Journal the command as one automatic compute action when --environment was given, else just run it."""
    try:
        return checkpoint.recorded_action(case.root, args, "compute", "calc.py add", run, (Refused,))
    except checkpoint.Refused as error:
        raise Refused(f"журнал действий: {error.message or error.reason}", journal=error.reason) from None


def cmd_add(args, limits: dict) -> int:
    with journaled(args):
        case = open_case(args.case)
        return recorded(args, case, lambda refs: add_calculation(case, args, limits, refs))


def add_calculation(case: Case, args, limits: dict, refs: list) -> int:
    spec = parse_add(read_request(case, args.request, limits))
    registry = load_registry(case)
    before = baseline(case)
    blind = sorted({f.code for f in before.errors if f.file == CALCULATIONS_FILE and f.record_id is None})
    if blind:
        raise Refused("валидатор не видит реестр расчётов: сначала исправьте его", errors=blind)
    cache, warnings, inputs, cells = {}, [], [], {}
    for item in spec["inputs"]:
        if "calculation_id" in item:
            inputs.append(resolve_calc_input(registry, item))
            cells[item["name"]] = 1
        else:
            stored, count = resolve_source_input(case, cache, item, warnings)
            inputs.append(stored)
            cells[item["name"]] = count
    gaps, assumptions = list(spec["gaps"]), list(spec["assumptions"])
    method = {"kind": "tool", "description": spec["description"]}
    if spec["not_computable"]:
        result = {"status": "not_computable", "value": None, "unit": spec["unit"], "period": spec["period"]}
        dimension = None
    else:
        result, extra_gaps, dimension = evaluate(spec, inputs)
        gaps += [gap for gap in extra_gaps if gap not in gaps]
        if spec["period_mix"] is not None:
            assumptions.append(PERIOD_NOTE + spec["period_mix"])
        method |= {"formula": engine.Formula(spec["formula"]).text, "digits": spec["digits"]}
    record = canonical({
        "id": next_id(registry), "created_at": now(), "record_status": "active", "inputs": inputs,
        "method": method, "result": result, "gaps": gaps, "assumptions": assumptions, "reproduce": "",
    })
    record["reproduce"] = (f"tools/calc.py check projects/{case_id(case)} {record['id']}"
                           if "formula" in method else NOT_COMPUTABLE_REPRODUCE)
    report = {"command": "add", "result": result, "dimension": dimension,
              "inputs": [{"name": item["name"], "value": item["value"], "cells": cells[item["name"]]} for item in inputs],
              "warnings": warnings}
    existing = next((r for r in registry["records"] if same_calculation(r, record)), None)
    if existing is not None:
        refs.append(existing["id"])
        refs.repeat = True
        emit(finish({**report, "id": existing["id"], "existing": True} | case_findings(before, {existing["id"]}),
                    args.request))
        return EXIT_OK
    snapshot = Snapshot(case, (CALCULATIONS_FILE,))
    registry["records"].append(record)
    write_atomic(case.root / CALCULATIONS_FILE, dumps(registry))
    refs.append(record["id"])
    after = check_case(case, before, {record["id"]}, snapshot)
    findings = case_findings(after, {record["id"]})
    emit(finish({**report, "id": record["id"], "existing": False,
                 "warnings": warnings + findings["warnings"], "case_errors": findings["case_errors"]}, args.request))
    return EXIT_OK


def case_id(case: Case) -> str:
    try:
        value = json.loads((case.root / "case.json").read_text(encoding="utf-8")).get("case_id")
    except (OSError, ValueError, AttributeError):
        value = None
    return value if isinstance(value, str) and value else case.root.name


def cmd_check(args, limits: dict) -> int:
    case = open_case(args.case)
    if args.id is not None and not CALC_ID.fullmatch(args.id):
        raise Refused("ожидается ID расчёта вида CALC-NNNN")
    registry = load_registry(case)
    records = [r for r in registry["records"] if r.get("record_status") == "active"]
    if args.id is not None:
        records = [r for r in registry["records"] if r["id"] == args.id]
        if not records:
            raise Refused("расчёт не найден", id=args.id)
        if records[0].get("record_status") != "active":
            raise Refused("расчёт отозван: проверяются только активные", id=args.id)
    report = validate_case(case.root)
    unverified = {f.record_id for f in report.warnings if f.code == "CALC_UNVERIFIED"}
    verdicts = {"ok": [], "mismatch": [], "blocked": [], "unverified": [], "not_computable": []}
    for record in records:
        rid = record["id"]
        own = [f for f in report.errors if f.record_id == rid and f.file == CALCULATIONS_FILE]
        if any(f.code == "CALC_MISMATCH" for f in own):
            verdicts["mismatch"].append({"id": rid, "reasons": [f.message for f in own if f.code == "CALC_MISMATCH"]})
        elif own:
            verdicts["blocked"].append({"id": rid, "errors": sorted({f.code for f in own})})
        elif rid in unverified:
            verdicts["unverified"].append(rid)
        elif "formula" not in record["method"]:
            verdicts["not_computable"].append(rid)
        else:
            verdicts["ok"].append(rid)
    emit({"command": "check", "checked": len(records)} | verdicts)
    return EXIT_MISMATCH if verdicts["mismatch"] or verdicts["blocked"] else EXIT_OK


def build_parser() -> Parser:
    parser = Parser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    commands = parser.add_subparsers(dest="command", required=True)

    add = commands.add_parser("add")
    add.add_argument("case", type=Path)
    add.add_argument("request", type=Path)
    checkpoint.add_action_keys(add)
    add.set_defaults(run=cmd_add)

    check = commands.add_parser("check")
    check.add_argument("case", type=Path)
    check.add_argument("id", nargs="?")
    check.set_defaults(run=cmd_check)
    return parser


def main(argv: list[str] | None = None) -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    try:
        args = build_parser().parse_args(argv)
        return args.run(args, load_limits())
    except Refused as error:
        emit({"status": "refused", "reason": str(error)} | error.details)
        return EXIT_REFUSED
    except Exception as error:
        emit({"status": "internal_error", "error_type": type(error).__name__})
        return EXIT_INTERNAL


if __name__ == "__main__":
    raise SystemExit(main())
