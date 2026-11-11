"""Record claims with verified locators, withdraw claims and sources, follow links to fragments.

Commands: add, update, withdraw (write), trace, impact (read only).

A write command takes a request file from sources/incoming/ of the case: quotes come
from materials, so they never travel through the shell. The tool copies the locator
from the block you name, checks the quote against the block text, applies the change,
runs the case validator and restores the previous bytes when the claim got a new error.
The request file is removed last.

Exit codes: 0 done, 4 nothing written (refused, wrong call included), 5 internal failure.
The first output line is a JSON report without material text.
"""
import argparse
import copy
import json
import re
import sys
from pathlib import Path

from evidence_blocks import collapse, load_blocks, matching_blocks, quotable_text, quote_in
from material_extract import TEXT_STATUSES, load_limits
from read_material import (
    DATA_NOTICE,
    EXIT_INTERNAL,
    EXIT_OK,
    EXIT_REFUSED,
    SOURCES_FILE,
    Case,
    Parser,
    Refused,
    dumps,
    emit,
    inside,
    now,
    write_atomic,
)
from validate_project import (
    BRACKET_RE,
    ID_RE,
    LOOKS_LIKE_ID_RE,
    UNREADABLE_EXTRACTION,
    is_active,
    prefix_of,
    validate_case,
)

CLAIMS_FILE = "evidence/claims.json"
CALCULATIONS_FILE = "evidence/calculations.json"
QUESTIONS_FILE = "research/questions.json"
OPTIONS_FILE = "decisions/options.json"
REVISION_LOG = "outputs/revisions.md"
DECISIONS_FILE = "decisions/decisions.json"
CLAIM_ID = re.compile(r"CLM-[0-9]{4,}")
SOURCE_ID = re.compile(r"SRC-[0-9]{4,}")
DERIVED_ID = re.compile(r"(?:CLM|CALC)-[0-9]{4,}")
KINDS = ("fact", "hypothesis", "forecast", "inference")
SETTABLE_STATUSES = ("confirmed", "unconfirmed", "contested")
CLAIM_FIELDS = ("id", "created_at", "text", "kind", "status", "status_reason", "supports", "contradicts",
                "derived_from", "limits")
REFERENCE_FIELDS = ("source_id", "locator", "note", "resolution")
GROUP_ORDER = ("CLM", "CALC", "OPT", "Q", "DEC", "SRC")


# --- request ----------------------------------------------------------------

def fail(where: str, what: str):
    raise Refused(f"запрос, {where}: {what}")


def check_object(value, where: str, required: set, optional: set) -> dict:
    if not isinstance(value, dict):
        fail(where, "ожидается объект")
    if any(key not in required | optional for key in value):
        fail(where, "есть неизвестное поле")
    for key in sorted(required):
        if key not in value:
            fail(where, f"нет обязательного поля {key}")
    return value


def text_value(value, where: str) -> str:
    if not isinstance(value, str) or not value.strip():
        fail(where, "ожидается непустая строка")
    return value.strip()


def number_value(value, where: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        fail(where, "ожидается целое число от 1")
    return value


def id_list(value, where: str, pattern: re.Pattern) -> list[str]:
    if not isinstance(value, list):
        fail(where, "ожидается список")
    for item in value:
        if not isinstance(item, str) or not pattern.fullmatch(item):
            fail(where, "ожидается ID нужного вида")
    return list(dict.fromkeys(value))


def parse_reference(item, where: str, contradiction: bool) -> dict:
    optional = {"block_to", "section"} | ({"resolution"} if contradiction else set())
    item = check_object(item, where, {"source_id", "block", "quote", "note"}, optional)
    if not isinstance(item["source_id"], str) or not SOURCE_ID.fullmatch(item["source_id"]):
        fail(where, "source_id должен быть вида SRC-NNNN")
    parsed = {"source_id": item["source_id"], "block": number_value(item["block"], where + " block"),
              "quote": text_value(item["quote"], where + " quote"), "note": text_value(item["note"], where + " note")}
    if "block_to" in item:
        parsed["block_to"] = number_value(item["block_to"], where + " block_to")
    if "section" in item:
        if not isinstance(item["section"], bool):
            fail(where, "section должно быть true или false")
        parsed["section"] = item["section"]
    if "resolution" in item:
        parsed["resolution"] = text_value(item["resolution"], where + " resolution")
    return parsed


def reference_list(value, where: str, contradiction: bool) -> list[dict]:
    if not isinstance(value, list):
        fail(where, "ожидается список")
    return [parse_reference(item, f"{where}[{number}]", contradiction) for number, item in enumerate(value)]


def limits_value(value):
    return None if value is None else text_value(value, "limits")


def parse_add(data) -> dict:
    data = check_object(data, "запрос", {"text", "kind"},
                        {"status", "limits", "derived_from", "supports", "contradicts"})
    if data["kind"] not in KINDS:
        fail("kind", "неизвестный вид утверждения")
    status = data.get("status", "unconfirmed")
    if status not in SETTABLE_STATUSES:
        fail("status", "допустимы confirmed, unconfirmed, contested; отзыв делает команда withdraw")
    return {
        "text": text_value(data["text"], "text"),
        "kind": data["kind"],
        "status": status,
        "limits": limits_value(data.get("limits")),
        "derived_from": id_list(data.get("derived_from", []), "derived_from", DERIVED_ID),
        "supports": reference_list(data.get("supports", []), "supports", False),
        "contradicts": reference_list(data.get("contradicts", []), "contradicts", True),
    }


def parse_update(data) -> dict:
    data = check_object(data, "запрос", set(),
                        {"supports", "contradicts", "resolutions", "status", "status_reason", "limits"})
    if not data:
        fail("запрос", "в нём нет изменений")
    parsed = {}
    if "status" in data:
        if data["status"] not in SETTABLE_STATUSES:
            fail("status", "допустимы confirmed, unconfirmed, contested; отзыв делает команда withdraw")
        parsed["status"] = data["status"]
    if "status_reason" in data:
        parsed["status_reason"] = text_value(data["status_reason"], "status_reason")
    if "limits" in data:
        parsed["limits"] = limits_value(data["limits"])
    parsed["supports"] = reference_list(data.get("supports", []), "supports", False)
    parsed["contradicts"] = reference_list(data.get("contradicts", []), "contradicts", True)
    resolutions = data.get("resolutions", {})
    if not isinstance(resolutions, dict):
        fail("resolutions", "ожидается объект")
    parsed["resolutions"] = {}
    for key, value in resolutions.items():
        if not re.fullmatch(r"0|[1-9][0-9]*", key):
            fail("resolutions", "ключ должен быть номером противоречия с нуля")
        parsed["resolutions"][int(key)] = text_value(value, "resolutions")
    return parsed


def read_request(case: Case, path: Path, limits: dict):
    if not path.is_file() or not inside(path, case.root / "sources" / "incoming"):
        raise Refused("запрос должен лежать в sources/incoming/ кейса")

    def reject(constant):
        raise ValueError(constant)

    def no_duplicates(pairs):
        if len({key for key, _ in pairs}) != len(pairs):
            raise ValueError("duplicate key")
        return dict(pairs)

    try:
        text = path.read_bytes().decode("utf-8-sig")
        if len(text) > limits["max_text_chars"]:
            raise Refused("запрос превышает предел размера текста")
        data = json.loads(text, parse_constant=reject, object_pairs_hook=no_duplicates)
        json.dumps(data, ensure_ascii=False).encode("utf-8")  # a lone surrogate cannot be written
        return data
    except (UnicodeDecodeError, ValueError, RecursionError, OSError) as error:
        raise Refused("запрос должен быть JSON в UTF-8") from error


def finish(report: dict, request: Path | None) -> dict:
    if request is not None:
        try:
            request.unlink()
        except OSError:
            report["incoming_left"] = True
    return report


# --- registries -------------------------------------------------------------

def load_claims(case: Case) -> dict:
    registry = case.load(CLAIMS_FILE)
    for record in registry["records"]:
        valid = (isinstance(record, dict) and isinstance(record.get("id"), str) and CLAIM_ID.fullmatch(record["id"])
                 and isinstance(record.get("text"), str) and record.get("kind") in KINDS
                 and isinstance(record.get("status"), str)
                 and "limits" in record and isinstance(record["limits"], (str, type(None)))
                 and all(isinstance(record.get(key), list) for key in ("supports", "contradicts", "derived_from")))
        if valid:
            valid = all(isinstance(item, dict) and isinstance(item.get("source_id"), str)
                        and isinstance(item.get("locator"), dict)
                        and isinstance(item.get("resolution", ""), str) and isinstance(item.get("note", ""), str)
                        for item in record["supports"] + record["contradicts"])
        if not valid:
            raise Refused("реестр утверждений не соответствует контракту: запустите валидатор")
    return registry


def open_case(path: Path) -> Case:
    """The case with a source registry of the right shape: a damaged one is a refusal, not a crash."""
    case = Case(path)
    for record in case.records:
        extraction = record.get("extraction") if isinstance(record, dict) else None
        valid = (isinstance(extraction, dict) and isinstance(record.get("id"), str)
                 and SOURCE_ID.fullmatch(record["id"]) and isinstance(record.get("location"), dict)
                 and isinstance(extraction.get("status"), str) and "complete" in extraction
                 and isinstance(record.get("record_status"), str) and isinstance(record.get("allowed"), bool)
                 and isinstance(record.get("type"), str) and isinstance(record.get("title"), str)
                 and "sha256" in record)
        if not valid:
            raise Refused("реестр источников не соответствует контракту: запустите валидатор")
    return case


def load_calculations(case: Case) -> dict:
    registry = case.load(CALCULATIONS_FILE)
    for record in registry["records"]:
        inputs = record.get("inputs", []) if isinstance(record, dict) else None
        if not isinstance(inputs, list) or not all(isinstance(item, dict) for item in inputs):
            raise Refused("реестр расчётов не соответствует контракту: запустите валидатор")
    return registry


def check_derivation(case: Case, registry: dict, derived: list[str]) -> None:
    claims = {record["id"]: record for record in registry["records"]}
    calculations = {record.get("id"): record for record in load_calculations(case)["records"]}
    for item in derived:
        if item.startswith("CLM"):
            gone = claims.get(item, {}).get("status") == "withdrawn"
        else:
            gone = calculations.get(item, {}).get("record_status") == "withdrawn"
        if gone:
            raise Refused("в derived_from стоит отозванная запись", id=item)


NOTE_START = re.compile(r"Основание отозвано: (?:SRC|CLM)-[0-9]{4,},")


def replace_limits(old: str | None, new: str | None) -> str | None:
    """New limits text; the notes that withdrawals left in the old one stay."""
    text = old or ""
    starts = [match.start() for match in NOTE_START.finditer(text)]
    notes = [text[a:b].strip() for a, b in zip(starts, starts[1:] + [len(text)])]
    kept = [note for note in notes if note[:note.index(",") + 1] not in (new or "")]
    return " ".join(([new] if new else []) + kept) or None


def canonical(claim: dict) -> dict:
    ordered = {key: claim[key] for key in CLAIM_FIELDS if key in claim}
    return ordered | {key: value for key, value in claim.items() if key not in ordered}


def next_claim_id(registry: dict) -> str:
    numbers = [int(record["id"].split("-")[1]) for record in registry["records"]]
    return f"CLM-{max(numbers, default=0) + 1:04d}"


def find_claim(registry: dict, claim_id: str) -> dict:
    for record in registry["records"]:
        if record["id"] == claim_id:
            return record
    raise Refused("утверждение не найдено", id=claim_id)


def write_claims(case: Case, registry: dict) -> None:
    write_atomic(case.root / CLAIMS_FILE, dumps(registry))


def write_sources(case: Case) -> None:
    write_atomic(case.root / SOURCES_FILE, dumps(case.registry))


class Snapshot:
    """Bytes of the files a command may rewrite; restored when the case validator rejects the change."""

    def __init__(self, case: Case, names: tuple[str, ...]):
        self.case = case
        self.saved = {}
        for name in names:
            path = case.root / name
            self.saved[name] = path.read_bytes() if path.exists() else None

    def restore(self) -> None:
        for name, data in self.saved.items():
            path = self.case.root / name
            if data is None:
                path.unlink(missing_ok=True)
            else:
                write_atomic(path, data)


# --- sources and references ---------------------------------------------------

def blocks_of(case: Case, source_id: str, cache: dict) -> list[dict]:
    if source_id not in cache:
        path = case.extracted_path(source_id)
        if not path.is_file():
            raise Refused("у источника нет извлечённого текста", id=source_id)
        if not inside(path, case.root):
            raise Refused("извлечённый текст источника повреждён", id=source_id)
        try:
            cache[source_id] = load_blocks(path)
        except (ValueError, OSError) as error:
            raise Refused("извлечённый текст источника повреждён", id=source_id) from error
    return cache[source_id]


def usable_source(case: Case, source_id: str, supporting: bool) -> dict:
    record = case.get(source_id)
    status = record["extraction"]["status"]
    if status in UNREADABLE_EXTRACTION or status not in TEXT_STATUSES:
        raise Refused("у источника нет прочитанного текста", id=source_id)
    if supporting and (record["record_status"] != "active" or not record["allowed"]):
        raise Refused("источник отозван или запрещён как подтверждение: он может только противоречить", id=source_id)
    return record


def locator_for(item: dict, source: dict, blocks: list[dict]) -> dict:
    by_number = {block["n"]: block for block in blocks}
    first = by_number.get(item["block"])
    if first is None:
        raise Refused("блока с таким номером нет", id=source["id"])
    if item.get("section"):
        if "block_to" in item:
            raise Refused("section и block_to несовместимы", id=source["id"])
        heading = first.get("heading")
        if not isinstance(heading, str) or not heading.strip():
            raise Refused("у блока нет заголовка: укажите его без section, локатором paragraph", id=source["id"])
        return {"kind": "section", "heading": heading}
    if "block_to" in item:
        last = by_number.get(item["block_to"])
        if source["type"] != "csv" or last is None or last["n"] < first["n"]:
            raise Refused("block_to допустим только для CSV и должен указывать на блок не раньше первого",
                          id=source["id"])
        return {"kind": "rows", "from": first["locator"]["from"], "to": last["locator"]["to"]}
    return dict(first["locator"])


def build_reference(case: Case, cache: dict, item: dict, contradiction: bool) -> dict:
    source = usable_source(case, item["source_id"], supporting=not contradiction)
    blocks = blocks_of(case, source["id"], cache)
    locator = locator_for(item, source, blocks)
    found = matching_blocks(blocks, locator)
    if not found:
        raise Refused("локатор не указывает ни на один блок", id=source["id"])
    if not any(quote_in(item["quote"], quotable_text(block)) for block in found):
        raise Refused("цитаты нет в тексте блока", id=source["id"], block=item["block"])
    reference = {"source_id": source["id"], "locator": locator | {"quote": item["quote"]}, "note": item["note"]}
    if "resolution" in item:
        reference["resolution"] = item["resolution"]
    return reference


def place(reference: dict) -> tuple:
    locator = {key: value for key, value in reference["locator"].items() if key != "quote"}
    return reference["source_id"], json.dumps(locator, sort_keys=True)


def merge_references(existing: list[dict], new: list[dict]) -> tuple[list[dict], list[int]]:
    """Append references; an identical one is skipped, the same place with other words is refused."""
    result, added = list(existing), []
    for reference in new:
        clash = [item for item in result if place(item) == place(reference)]
        if any(all(item.get(key) == reference.get(key) for key in REFERENCE_FIELDS) for item in clash):
            continue
        if clash:
            raise Refused("то же место источника уже указано с другой цитатой или заметкой", id=reference["source_id"])
        result.append(reference)
        added.append(len(result) - 1)
    return result, added


def address_keys(source: dict) -> list[tuple]:
    """Keys under which two sources are one origin: the same address or the same file digest."""
    keys = []
    if "url" in source["location"]:
        keys.append(("url", source["location"]["url"]))
    if source.get("sha256"):
        keys.append(("sha256", source["sha256"]))
    return keys


def same_origin(case: Case, claim: dict) -> list[list[str]]:
    """Groups of supporting sources that share an address or a file digest: one basis, not several."""
    known = {record["id"]: record for record in case.records}
    groups: dict[tuple, set] = {}
    for item in claim["supports"]:
        record = known.get(item["source_id"])
        if record is None:
            continue
        for key in address_keys(record):
            groups.setdefault(key, set()).add(item["source_id"])
    found = {tuple(sorted(ids)) for ids in groups.values() if len(ids) > 1}
    return sorted(list(ids) for ids in found)


def claim_key(text: str) -> str:
    return collapse(text).casefold()


# --- validation after a write ---------------------------------------------------

def own(finding, ids: set[str]) -> bool:
    """A finding on the touched records. WITHDRAWN_USED is never one: it names the users of a withdrawn record."""
    if finding.code == "WITHDRAWN_USED":
        return False
    return finding.record_id in ids or (finding.file == CLAIMS_FILE and finding.record_id is None)


def pairs(findings, ids: set[str]) -> set[tuple]:
    return {(f.code, f.record_id) for f in findings if own(f, ids)}


def check_case(case: Case, before, ids: set[str], snapshot: Snapshot):
    """Validate after the write; a new error on the touched records restores the files and refuses."""
    try:
        after = validate_case(case.root)
    except BaseException:
        snapshot.restore()
        raise
    new = pairs(after.errors, ids) - pairs(before.errors, ids)
    if new:
        snapshot.restore()
        raise Refused("изменение не прошло проверку кейса, ничего не записано",
                      errors=sorted({code for code, _ in new}), id=sorted(ids)[0])
    return after


def baseline(case: Case):
    """Validate before a change; refuse when the validator cannot see the claim and source registries."""
    report = validate_case(case.root)
    blind = sorted({f.code for f in report.errors if f.code.startswith("FORMAT_")
                    or (f.code == "SCHEMA" and f.record_id is None and f.file in (CLAIMS_FILE, SOURCES_FILE))})
    if blind:
        raise Refused("валидатор не видит кейс: сначала исправьте ошибки формата", errors=blind)
    return report


def reject_known_errors(report, ids: set[str]) -> None:
    codes = sorted({f.code for f in report.errors if own(f, ids)})
    if codes:
        raise Refused("запись не проходит проверку кейса", errors=codes, id=sorted(ids)[0])


def case_findings(report, ids: set[str]) -> dict:
    return {
        "warnings": [{"code": f.code, "record_id": f.record_id} for f in report.warnings if f.record_id in ids],
        "case_errors": [{"code": f.code, "record_id": f.record_id, "file": f.file}
                        for f in report.errors if not own(f, ids)],
    }


# --- add and update -----------------------------------------------------------------

def settle_status(status: str, previous: str, contradictions: list[dict], added: list[int]) -> tuple[str, dict | None]:
    """A confirmed claim with a new contradiction nobody explained becomes contested."""
    if status == "confirmed" and any(not (contradictions[index].get("resolution") or "").strip() for index in added):
        return "contested", ({"from": previous, "to": "contested"} if previous != "contested" else None)
    return status, None


def claim_report(command: str, case: Case, claim: dict, change: dict | None, after) -> dict:
    return {
        "command": command,
        "id": claim["id"],
        "claim_status": claim["status"],
        "status_change": change,
        "supports": len(claim["supports"]),
        "contradicts": len(claim["contradicts"]),
        "same_origin": same_origin(case, claim),
    } | case_findings(after, {claim["id"]})


def cmd_add(args, limits: dict) -> int:
    case = open_case(args.case)
    spec = parse_add(read_request(case, args.request, limits))
    registry = load_claims(case)
    before = baseline(case)
    check_derivation(case, registry, spec["derived_from"])
    cache: dict = {}
    supports, _ = merge_references([], [build_reference(case, cache, item, False) for item in spec["supports"]])
    contradicts, added = merge_references([], [build_reference(case, cache, item, True) for item in spec["contradicts"]])
    status, change = settle_status(spec["status"], spec["status"], contradicts, added)
    claim = canonical({
        "id": next_claim_id(registry), "created_at": now(), "text": spec["text"], "kind": spec["kind"],
        "status": status, "supports": supports, "contradicts": contradicts,
        "derived_from": spec["derived_from"], "limits": spec["limits"],
    })
    for existing in registry["records"]:
        if (existing["status"] != "withdrawn" and existing["kind"] == claim["kind"]
                and claim_key(existing["text"]) == claim_key(claim["text"])):
            same = all(existing.get(key) == claim[key]
                       for key in ("status", "supports", "contradicts", "derived_from", "limits"))
            if not same:
                raise Refused("такое утверждение уже есть: добавьте основания командой update", existing_id=existing["id"])
            reject_known_errors(before, {existing["id"]})
            report = {"command": "add", "id": existing["id"], "existing": True, "claim_status": existing["status"]}
            emit(finish(report | case_findings(before, {existing["id"]}), args.request))
            return EXIT_OK
    snapshot = Snapshot(case, (CLAIMS_FILE,))
    registry["records"].append(claim)
    write_claims(case, registry)
    after = check_case(case, before, {claim["id"]}, snapshot)
    emit(finish(claim_report("add", case, claim, change, after) | {"existing": False}, args.request))
    return EXIT_OK


def cmd_update(args, limits: dict) -> int:
    case = open_case(args.case)
    if not CLAIM_ID.fullmatch(args.claim):
        raise Refused("ожидается ID утверждения вида CLM-NNNN")
    spec = parse_update(read_request(case, args.request, limits))
    registry = load_claims(case)
    before = baseline(case)
    current = find_claim(registry, args.claim)
    if current["status"] == "withdrawn":
        raise Refused("отозванное утверждение не обновляется", id=args.claim)
    check_derivation(case, registry, current["derived_from"])
    cache: dict = {}
    claim = copy.deepcopy(current)
    claim["supports"], _ = merge_references(
        claim["supports"], [build_reference(case, cache, item, False) for item in spec["supports"]])
    claim["contradicts"], added = merge_references(
        claim["contradicts"], [build_reference(case, cache, item, True) for item in spec["contradicts"]])
    for index, resolution in spec["resolutions"].items():
        if index >= len(claim["contradicts"]):
            raise Refused("противоречия с таким номером нет", id=args.claim)
        old = (claim["contradicts"][index].get("resolution") or "").strip()
        if old and old != resolution:
            raise Refused("у противоречия уже есть resolution: перезаписать его нельзя", id=args.claim)
        claim["contradicts"][index] = claim["contradicts"][index] | {"resolution": resolution}
    if "limits" in spec:
        claim["limits"] = replace_limits(claim["limits"], spec["limits"])
    if "status_reason" in spec:
        claim["status_reason"] = spec["status_reason"]
    previous = current["status"]
    claim["status"], change = settle_status(spec.get("status", previous), previous, claim["contradicts"], added)
    claim = canonical(claim)
    if claim == current:
        reject_known_errors(before, {args.claim})
        report = {"command": "update", "id": args.claim, "changed": False, "claim_status": current["status"]}
        emit(finish(report | case_findings(before, {args.claim}), args.request))
        return EXIT_OK
    snapshot = Snapshot(case, (CLAIMS_FILE,))
    registry["records"][registry["records"].index(current)] = claim
    write_claims(case, registry)
    after = check_case(case, before, {args.claim}, snapshot)
    previous_errors = sorted({f.code for f in before.errors if own(f, {args.claim})})
    emit(finish(claim_report("update", case, claim, change, after) | {"changed": True, "previous_errors": previous_errors},
                args.request))
    return EXIT_OK


# --- links between records ------------------------------------------------------------

def output_links(text: str) -> list[str]:
    """IDs in [ID] and [ID, ID] brackets, parsed like the validator does."""
    links = []
    for match in BRACKET_RE.finditer(text):
        parts = [part.strip() for part in match.group(1).split(",")]
        if all(LOOKS_LIKE_ID_RE.fullmatch(part) for part in parts):
            links += [part for part in parts if ID_RE.fullmatch(part)]
    return list(dict.fromkeys(links))


def ids_in(value) -> list[str]:
    return [item for item in value if isinstance(item, str) and ID_RE.fullmatch(item)] if isinstance(value, list) else []


class Graph:
    """What each record and output text refers to, and the reverse."""

    def __init__(self, case: Case, claims: dict):
        self.records: dict[str, dict] = {}
        self.edges: dict[str, list[str]] = {}
        self.outputs: dict[str, list[str]] = {}
        for record in case.records:
            self.add_record(record, [])
        for record in claims["records"]:
            links = [item["source_id"] for item in record["supports"] + record["contradicts"]]
            self.add_record(record, links + ids_in(record["derived_from"]))
        for record in load_calculations(case)["records"]:
            inputs = [item for item in record.get("inputs", []) if isinstance(item, dict)] if isinstance(record, dict) else []
            self.add_record(record, [item.get("source_id") or item.get("calculation_id") for item in inputs])
        for record in case.load(QUESTIONS_FILE)["records"]:
            self.add_record(record, ids_in(record.get("findings") if isinstance(record, dict) else None))
        for record in case.load(OPTIONS_FILE)["records"]:
            self.add_record(record, ids_in(record.get("basis") if isinstance(record, dict) else None))
        for record in case.load(DECISIONS_FILE)["records"]:
            parts = [record.get("proposal"), record.get("choice")] if isinstance(record, dict) else []
            self.add_record(record, [part.get("option_id") for part in parts if isinstance(part, dict)])
        folder = case.root / "outputs"
        if folder.is_dir():
            for path in sorted(folder.rglob("*.md")):
                relative = path.relative_to(case.root).as_posix()
                if any(part.startswith(".") for part in path.relative_to(folder).parts) or relative == REVISION_LOG:
                    continue  # old copies and the revision log are history, not texts to review
                try:
                    text = path.read_bytes().decode("utf-8-sig")
                except (UnicodeDecodeError, OSError):
                    continue
                self.outputs[relative] = output_links(text)
        self.reverse: dict[str, list[str]] = {}
        for node, targets in list(self.edges.items()) + list(self.outputs.items()):
            for target in targets:
                self.reverse.setdefault(target, []).append(node)

    def add_record(self, record, links) -> None:
        record_id = record.get("id") if isinstance(record, dict) else None
        if isinstance(record_id, str) and ID_RE.fullmatch(record_id):
            self.records[record_id] = record
            self.edges[record_id] = list(dict.fromkeys(ids_in(links)))

    def active(self, node: str) -> bool:
        if node not in self.records:
            return True
        try:
            return is_active(node, self.records[node])
        except (KeyError, TypeError):
            return True

    def dependents(self, start: str) -> dict:
        """Everything that depends on `start`, grouped by record type; withdrawn ones are listed apart."""
        seen, queue = {start}, [start]
        while queue:
            for node in self.reverse.get(queue.pop(), []):
                if node not in seen:
                    seen.add(node)
                    queue.append(node)
        seen.discard(start)
        groups: dict[str, list[str]] = {}
        inactive = []
        for node in sorted(seen):
            if node in self.outputs:
                groups.setdefault("outputs", []).append(node)
            elif self.active(node):
                groups.setdefault(prefix_of(node), []).append(node)
            else:
                inactive.append(node)
        ordered = {key: groups[key] for key in (*GROUP_ORDER, "outputs") if key in groups}
        return {"dependents": ordered} | ({"inactive": inactive} if inactive else {})


# --- withdraw ---------------------------------------------------------------------------

def add_note(claim: dict, target: str, reason: str) -> bool:
    marker = f"Основание отозвано: {target},"
    if marker in (claim["limits"] or ""):
        return False
    note = f"{marker} причина: {reason}."
    claim["limits"] = f"{claim['limits']} {note}" if claim["limits"] else note
    return True


def has_note(claim: dict, target: str) -> bool:
    return f"Основание отозвано: {target}," in (claim["limits"] or "")


def lost_calculations(graph: "Graph", source_id: str) -> set[str]:
    """Calculations that rest on the source directly or through other calculations, withdrawn ones included."""
    found, queue = set(), [source_id]
    while queue:
        for node in graph.reverse.get(queue.pop(), []):
            if prefix_of(node) == "CALC" and node not in found:
                found.add(node)
                queue.append(node)
    return found


def cascade(registry: dict, graph: "Graph", target: str, reason: str) -> tuple[list[str], list[str]]:
    """Lower the claims that lost their basis; return (downgraded, only annotated)."""
    claims = [r for r in registry["records"] if r["status"] != "withdrawn" and r["id"] != target]
    if target.startswith("SRC"):
        lost = lost_calculations(graph, target)
        affected = [r for r in claims if any(s["source_id"] == target for s in r["supports"])
                    or lost & set(r["derived_from"])]
    elif target.startswith("CALC"):
        lost = lost_calculations(graph, target) | {target}
        affected = [r for r in claims if lost & set(r["derived_from"])]
    else:
        affected = [r for r in claims if target in r["derived_from"]]
    done = {r["id"] for r in affected}
    queue = list(affected)
    while queue:
        current = queue.pop(0)
        for record in claims:
            if record["id"] not in done and current["id"] in record["derived_from"]:
                done.add(record["id"])
                affected.append(record)
                queue.append(record)
    downgraded, annotated = [], []
    for record in affected:
        had = has_note(record, target)
        lowered = record["status"] == "confirmed"
        if lowered:
            record["status"] = "unconfirmed"
        added = add_note(record, target, reason)
        if lowered or (had and record["status"] == "unconfirmed"):
            downgraded.append(record["id"])
        elif added or had:
            annotated.append(record["id"])
    return sorted(downgraded), sorted(annotated)


def find_calculation(calculations: dict, calc_id: str) -> dict:
    for record in calculations["records"]:
        if isinstance(record, dict) and record.get("id") == calc_id:
            if not isinstance(record.get("record_status"), str):
                raise Refused("реестр расчётов не соответствует контракту: запустите валидатор")
            return record
    raise Refused("расчёт не найден", id=calc_id)


def withdrawn_copy(record: dict, reason: str) -> dict:
    """The record marked withdrawn, with the reason right after the status."""
    result = {}
    for key, value in record.items():
        if key == "status_reason":
            continue
        result[key] = "withdrawn" if key == "record_status" else value
        if key == "record_status":
            result["status_reason"] = reason
    return result


def cmd_withdraw(args, limits: dict) -> int:
    case = open_case(args.case)
    target, reason = args.id, args.reason.strip()
    if not reason:
        raise Refused("--reason не должен быть пустым")
    if not re.fullmatch(r"(?:SRC|CLM|CALC)-[0-9]{4,}", target):
        raise Refused("отзыв поддерживается для утверждений CLM-NNNN, расчётов CALC-NNNN и источников SRC-NNNN")
    registry = load_claims(case)
    graph = Graph(case, registry)
    calculations = load_calculations(case) if target.startswith("CALC") else None
    if target.startswith("SRC"):
        source = case.get(target)
    elif target.startswith("CALC"):
        source = find_calculation(calculations, target)
    else:
        source = find_claim(registry, target)
    status_key = "status" if target.startswith("CLM") else "record_status"
    already = source[status_key] == "withdrawn"
    reread = sorted(r["id"] for r in registry["records"]
                    if target.startswith("SRC") and r["status"] != "withdrawn"
                    and any(c["source_id"] == target for c in r["contradicts"]))
    before = baseline(case)
    snapshot = Snapshot(case, (CLAIMS_FILE, SOURCES_FILE, CALCULATIONS_FILE))
    original = copy.deepcopy(registry)
    downgraded, annotated = cascade(registry, graph, target, reason)
    if not already and target.startswith("CLM"):
        for key, value in (("status", "withdrawn"), ("status_reason", reason)):
            source[key] = value
        registry["records"][registry["records"].index(source)] = canonical(source)
    if registry != original:
        write_claims(case, registry)
    if not already and target.startswith("SRC"):
        case.records[case.records.index(source)] = withdrawn_copy(source, reason)
        write_sources(case)
    if not already and target.startswith("CALC"):
        records = calculations["records"]
        records[records.index(source)] = withdrawn_copy(source, reason)
        write_atomic(case.root / CALCULATIONS_FILE, dumps(calculations))
    touched = {target, *downgraded, *annotated}
    after = check_case(case, before, touched, snapshot)
    report = {"command": "withdraw", "id": target, "already": already, "downgraded": downgraded,
              "annotated": annotated, "reread_resolution": reread} | graph.dependents(target)
    report |= {"case_errors": case_findings(after, touched)["case_errors"]}
    emit(report)
    return EXIT_OK


# --- trace and impact -----------------------------------------------------------------------

TRACE_START = re.compile(r"(?:CLM|CALC|OPT|Q|DEC)-[0-9]{4,}")
IMPACT_START = re.compile(r"(?:SRC|CLM|CALC|OPT|Q|DEC)-[0-9]{4,}")
BLOCK_PAD = {"i": 10**7, "part": 10**5, "parts": 10**5, "char_offset": 10**9}


class Tracer:
    """Walk from a record or an output text down to blocks of sources, listing flat items."""

    def __init__(self, case: Case, graph: Graph, budget: int):
        self.case, self.graph, self.budget = case, graph, budget
        self.items: list[dict] = []
        self.warnings: dict[str, int] = {}
        self.seen: set[str] = set()
        self.cache: dict[str, tuple] = {}
        self.sources = {record["id"]: record for record in case.records}
        self.addresses: dict[tuple, list[str]] = {}
        for record in case.records:
            for key in address_keys(record):
                self.addresses.setdefault(key, []).append(record["id"])

    def add(self, item: dict, warnings: list[str] = ()) -> None:
        warnings = list(dict.fromkeys(warnings))
        for name in warnings:
            self.warnings[name] = self.warnings.get(name, 0) + 1
        self.items.append(item | ({"warnings": warnings} if warnings else {}))

    def run(self, starts: list[str], depth: int = 0, via: str | None = None) -> None:
        stack = [(rid, depth, via) for rid in reversed(starts)]
        while stack:
            rid, level, parent = stack.pop()
            for child in reversed(self.visit(rid, level, parent)):
                stack.append((child, level + 1, rid))

    def visit(self, rid: str, depth: int, via: str | None) -> list[str]:
        base = {"depth": depth} | ({"from": via} if via else {})
        if rid in self.seen:
            self.add({"type": "seen", "id": rid} | base)
            return []
        record = self.graph.records.get(rid)
        if record is None:
            self.add({"type": "unresolved", "id": rid} | base, ["link_unresolved"])
            return []
        self.seen.add(rid)
        return getattr(self, "visit_" + prefix_of(rid).lower())(rid, record, base)

    def visit_clm(self, rid: str, record: dict, base: dict) -> list[str]:
        status = record["status"]
        warnings = ["withdrawn"] if status == "withdrawn" else [status] if status in ("unconfirmed", "contested") else []
        if record["kind"] in ("hypothesis", "forecast"):
            warnings.append(record["kind"])
        if status != "withdrawn" and any(not (item.get("resolution") or "").strip() for item in record["contradicts"]):
            warnings.append("contradiction_unresolved")
        item = {"type": "record", "id": rid, "record": "claim", "kind": record["kind"], "status": status,
                "text": record["text"], "limits": record.get("limits")} | base
        if "status_reason" in record:
            item["status_reason"] = record["status_reason"]
        self.add(item, warnings)
        for reference in record["supports"]:
            self.reference("support", rid, reference)
        for reference in record["contradicts"]:
            self.reference("contradiction", rid, reference)
        return ids_in(record["derived_from"])

    def visit_calc(self, rid: str, record: dict, base: dict) -> list[str]:
        result = record.get("result") if isinstance(record.get("result"), dict) else {}
        warnings = ["withdrawn"] if record.get("record_status") == "withdrawn" else []
        if result.get("status") != "computed":
            warnings.append("not_computed")
        self.add({"type": "record", "id": rid, "record": "calculation", "status": record.get("record_status"),
                  "result": result, "gaps": record.get("gaps"), "assumptions": record.get("assumptions"),
                  "method": record.get("method")} | base, warnings)
        children = []
        for entry in record.get("inputs", []):
            if not isinstance(entry, dict):
                continue
            if "source_id" in entry and isinstance(entry.get("locator"), dict):
                extra = {key: entry.get(key) for key in ("name", "value", "unit", "period")}
                extra |= {key: entry[key] for key in ("aggregate", "column") if key in entry}
                self.reference("input", rid, {"source_id": entry["source_id"], "locator": entry["locator"]}, extra)
            elif isinstance(entry.get("calculation_id"), str):
                children.append(entry["calculation_id"])
        return ids_in(children)

    def visit_opt(self, rid: str, record: dict, base: dict) -> list[str]:
        warnings = ["withdrawn"] if record.get("record_status") == "withdrawn" else []
        self.add({"type": "record", "id": rid, "record": "option", "status": record.get("record_status"),
                  "title": record.get("title")} | base, warnings)
        return ids_in(record.get("basis"))

    def visit_q(self, rid: str, record: dict, base: dict) -> list[str]:
        warnings = ["withdrawn"] if record.get("record_status") == "withdrawn" else []
        if record.get("status") != "answered":
            warnings.append("question_not_answered")
        self.add({"type": "record", "id": rid, "record": "question", "status": record.get("record_status"),
                  "question_status": record.get("status"), "question": record.get("question"),
                  "gap": record.get("gap")} | base, warnings)
        return ids_in(record.get("findings"))

    def visit_dec(self, rid: str, record: dict, base: dict) -> list[str]:
        status = record.get("status")
        warnings = ["superseded"] if status == "superseded" else ["decision_not_chosen"] if status == "proposed" else []
        self.add({"type": "record", "id": rid, "record": "decision", "status": status,
                  "topic": record.get("topic")} | base, warnings)
        parts = [record.get("proposal"), record.get("choice")]
        return list(dict.fromkeys(ids_in([part.get("option_id") for part in parts if isinstance(part, dict)])))

    def visit_src(self, rid: str, record: dict, base: dict) -> list[str]:
        item, warnings = self.source_view(record)
        self.add({"type": "source", "id": rid} | item | base, warnings)
        return []

    def source_view(self, source: dict) -> tuple[dict, list[str]]:
        extraction = source["extraction"]
        warnings = []
        if source["record_status"] == "withdrawn":
            warnings.append("source_withdrawn")
        if not source["allowed"]:
            warnings.append("source_not_allowed")
        if extraction["status"] in UNREADABLE_EXTRACTION:
            warnings.append("source_no_text")
        elif extraction["status"] == "partial" or not extraction["complete"]:
            warnings.append("source_partial")
        if source["type"] == "web" and source["sha256"] is None and extraction["status"] not in UNREADABLE_EXTRACTION:
            warnings.append("source_via_model")
        item = {"title": source["title"], "location": source["location"], "source_status": source["record_status"],
                "extraction": extraction}
        others = sorted({other for key in address_keys(source) for other in self.addresses[key]} - {source["id"]})
        if others:
            item["same_address_as"] = others
            warnings.append("same_address")
        return item, warnings

    def blocks(self, source_id: str) -> tuple[list[dict] | None, str | None]:
        if source_id not in self.cache:
            path = self.case.extracted_path(source_id)
            try:
                if path.is_file() and not inside(path, self.case.root):
                    raise ValueError("outside the case")
                self.cache[source_id] = (load_blocks(path), None) if path.is_file() else (None, "extraction_missing")
            except (ValueError, OSError):
                self.cache[source_id] = (None, "extraction_corrupt")
        return self.cache[source_id]

    def reference(self, kind: str, owner: str, reference: dict, extra: dict | None = None) -> None:
        source_id, locator = reference["source_id"], reference["locator"]
        item = {"type": kind, "owner": owner, "source_id": source_id, "locator": locator}
        item |= {key: reference[key] for key in ("note", "resolution") if key in reference}
        item |= extra or {}
        found, warnings = [], []
        source = self.sources.get(source_id)
        if source is None:
            warnings.append("source_missing")
        else:
            view, warnings = self.source_view(source)
            item |= view
            if source["extraction"]["status"] in TEXT_STATUSES:
                blocks, problem = self.blocks(source_id)
                if problem:
                    warnings.append(problem)
                else:
                    try:
                        found = matching_blocks(blocks, locator)
                        quote = locator.get("quote")
                        if not found:
                            warnings.append("locator_not_found")
                        elif quote is not None and not any(quote_in(quote, quotable_text(block)) for block in found):
                            warnings.append("quote_not_found")
                    except (KeyError, TypeError):
                        warnings.append("locator_not_found")
        if kind == "contradiction" and not (reference.get("resolution") or "").strip():
            warnings.append("contradiction_unresolved")
        self.add(item, warnings)
        for block in found:
            self.block_items(source_id, block)

    def block_items(self, source_id: str, block: dict) -> None:
        """The text of a block, in parts that each fit the output limit: nothing is cut off."""
        base = {"type": "block", "source_id": source_id, "n": block["n"], "locator": block["locator"]}
        overhead = len(json.dumps(base | BLOCK_PAD | {"text": ""}, ensure_ascii=False))
        text, offset, parts = block["text"], 0, []
        while offset < len(text) or not parts:
            size = max(1, self.budget - overhead)
            while size > 1 and len(json.dumps(text[offset:offset + size], ensure_ascii=False)) + overhead > self.budget:
                size = max(1, size // 2)
            parts.append((offset, text[offset:offset + size]))
            offset += size
        for number, (start, part) in enumerate(parts, 1):
            self.add(base | {"part": number, "parts": len(parts), "char_offset": start, "text": part})


def output_target(case: Case, target: str) -> tuple[str, list[str]]:
    message = "trace принимает CLM, CALC, OPT, Q, DEC или путь к файлу .md в outputs/ кейса"
    try:
        path = Path(target)
        candidates = [path] if path.is_absolute() else [case.root / path, path]
        found = next((p for p in candidates if inside(p, case.root / "outputs") and p.is_file()), None)
        if found is None or found.suffix.lower() != ".md":
            raise Refused(message)
        text = found.read_bytes().decode("utf-8-sig")
        return found.resolve().relative_to(case.root.resolve()).as_posix(), output_links(text)
    except (ValueError, OSError) as error:
        raise Refused(message) from error


def cmd_trace(args, limits: dict) -> int:
    if args.start < 1:
        raise Refused("--from начинается с 1")
    case = open_case(args.case)
    graph = Graph(case, load_claims(case))
    tracer = Tracer(case, graph, limits["max_output_chars"])
    target = args.target
    if TRACE_START.fullmatch(target):
        if target not in graph.records:
            raise Refused("запись не найдена", id=target)
        tracer.run([target])
    elif ID_RE.fullmatch(target):
        raise Refused("trace принимает CLM, CALC, OPT, Q, DEC или путь к файлу .md в outputs/ кейса")
    else:
        target, links = output_target(case, target)
        tracer.add({"type": "output", "path": target, "depth": 0})
        tracer.run(links, depth=1, via=target)
    shown, used = [], 0
    for number in range(args.start, len(tracer.items) + 1):
        line = json.dumps({"i": number} | tracer.items[number - 1], ensure_ascii=False)
        if shown and used + len(line) + 1 > limits["max_output_chars"]:
            break
        shown.append(line)
        used += len(line) + 1
    resume = args.start + len(shown)
    emit({"command": "trace", "target": target, "blocks_total": len(tracer.items), "blocks_shown": len(shown),
          "next": f"--from {resume}" if resume <= len(tracer.items) else None,
          "warnings": dict(sorted(tracer.warnings.items()))})
    if shown:
        print(DATA_NOTICE)
        print("\n".join(shown))
    return EXIT_OK


def cmd_impact(args, limits: dict) -> int:
    case = open_case(args.case)
    if not IMPACT_START.fullmatch(args.id):
        raise Refused("impact принимает ID записи SRC, CLM, CALC, OPT, Q или DEC")
    graph = Graph(case, load_claims(case))
    if args.id not in graph.records:
        raise Refused("запись не найдена", id=args.id)
    emit({"command": "impact", "id": args.id} | graph.dependents(args.id))
    return EXIT_OK


# --- command line ---------------------------------------------------------------------------

def build_parser() -> Parser:
    parser = Parser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    commands = parser.add_subparsers(dest="command", required=True)

    add = commands.add_parser("add")
    add.add_argument("case", type=Path)
    add.add_argument("request", type=Path)
    add.set_defaults(run=cmd_add)

    update = commands.add_parser("update")
    update.add_argument("case", type=Path)
    update.add_argument("claim")
    update.add_argument("request", type=Path)
    update.set_defaults(run=cmd_update)

    withdraw = commands.add_parser("withdraw")
    withdraw.add_argument("case", type=Path)
    withdraw.add_argument("id")
    withdraw.add_argument("--reason", required=True)
    withdraw.set_defaults(run=cmd_withdraw)

    trace = commands.add_parser("trace")
    trace.add_argument("case", type=Path)
    trace.add_argument("target")
    trace.add_argument("--from", dest="start", type=int, default=1)
    trace.set_defaults(run=cmd_trace)

    impact = commands.add_parser("impact")
    impact.add_argument("case", type=Path)
    impact.add_argument("id")
    impact.set_defaults(run=cmd_impact)
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
