"""Validate a case folder: file schemas and links between records. Read only.

Usage: validate_project.py <case folder> [--json]
Exit codes: 0 no errors, 1 errors found, 2 wrong invocation, 3 internal failure.

Output names the code, file, record ID and a short note. Field values and
material text never reach the output: they may carry injected instructions
or personal data. The contract is described in agent/contracts/README.md.
"""
import argparse
import hashlib
import json
import math
import re
import sys
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlsplit

from jsonschema import Draft202012Validator, FormatChecker
from jsonschema.exceptions import ValidationError, best_match
from jsonschema.validators import extend
from referencing import Registry, Resource

import calc_engine
from evidence_blocks import load_blocks, matching_blocks, quote_in

CONTRACTS = Path(__file__).resolve().parent.parent / "agent" / "contracts"
SCHEMAS = CONTRACTS / "schemas"

ERROR_CODES = frozenset({
    "ID_DUPLICATE", "ID_MISSING", "ID_WRONG_TYPE", "CLAIM_NO_BASIS", "SOURCE_MISSING",
    "SOURCE_CHANGED", "CLAIM_WEAK_SOURCE", "CLAIM_WEAK_DERIVATION", "CLAIM_CONFLICT_HIDDEN",
    "CLAIM_NOT_CONTESTED", "FACT_NOT_CONFIRMABLE", "STATUS_NO_REASON", "LOCATOR_MISMATCH",
    "WITHDRAWN_USED", "DECISION_CHOICE_STATE", "DECISION_NOT_HUMAN", "DECISION_SUPERSEDE",
    "DECISION_MULTIPLE_ACTIVE", "ACTION_RETRY", "ACTION_ORDER", "ACTION_COST", "ACTION_HUMAN", "OUTPUT_LINK",
    "PATH_UNSAFE", "SCHEMA", "FORMAT_UNSUPPORTED", "FORMAT_MIXED", "CASE_INVALID",
    "FILE_CORRUPT", "CHECKPOINT_NONE_VALID", "CHECKPOINT_CLAIM", "LOCATOR_NOT_FOUND", "QUOTE_NOT_FOUND",
    "CALC_MISMATCH", "TIME_IN_FUTURE",
})
WARNING_CODES = frozenset({
    "BASIS_UNCONFIRMED", "SOURCE_PARTIAL", "ACTION_UNKNOWN", "JOURNAL_TORN",
    "CHECKPOINT_STALE", "CHECKPOINT_FALLBACK", "EXTRACT_MISSING", "CALC_UNVERIFIED",
})
ALL_CODES = ERROR_CODES | WARNING_CODES

SUPPORTED_VERSIONS = frozenset(
    json.loads((CONTRACTS / "versions.json").read_text(encoding="utf-8"))["supported"]
)

# Registry file -> (schema name, ID prefix of its records).
REGISTRIES = {
    "evidence/sources.json": ("registry-sources", "SRC"),
    "evidence/claims.json": ("registry-claims", "CLM"),
    "evidence/calculations.json": ("registry-calculations", "CALC"),
    "research/questions.json": ("registry-questions", "Q"),
    "decisions/options.json": ("registry-options", "OPT"),
    "decisions/decisions.json": ("registry-decisions", "DEC"),
}
REGISTRY_FILE = {prefix: rel for rel, (_, prefix) in REGISTRIES.items()}
JOURNAL = "state/actions.jsonl"
# Clocks of two machines differ a little; a case moved between them must stay valid.
CLOCK_TOLERANCE = timedelta(minutes=5)
CHECKPOINT_DIR = "state/checkpoints"

ALL_PREFIXES = {"SRC", "CLM", "CALC", "Q", "OPT", "DEC", "ACT", "CKP"}
LOCATOR_SOURCE_TYPES = {
    "section": {"md", "txt", "docx", "web"},
    "paragraph": {"docx", "md", "txt", "web"},
    "page": {"pdf"},
    "cells": {"xlsx"},
    "rows": {"csv"},
    "doc_table": {"docx", "web"},
}
UNREADABLE_EXTRACTION = {"empty", "unreadable", "unsupported", "over_limit", "unavailable"}
PREFIX_ALTERNATIVES = "SRC|CLM|CALC|Q|OPT|DEC|ACT|CKP"
ID_RE = re.compile(rf"(?:{PREFIX_ALTERNATIVES})-[0-9]{{4,}}")
BRACKET_RE = re.compile(r"\[([^\[\]\n]{1,300})\]")
LOOKS_LIKE_ID_RE = re.compile(rf"(?i:{PREFIX_ALTERNATIVES})-\w+")
DRIVE_RE = re.compile(r"^[A-Za-z]:")
CHECKPOINT_FILE_RE = re.compile(r"CKP-([0-9]{4,})\.json")
# Set-aside checkpoint files: their ID stays taken, the record is unavailable.
HIDDEN_CHECKPOINT_RE = re.compile(r"\.(?:lost|rejected)-CKP-([0-9]{4,})\.json(?:\.[0-9]+)?")
# A command or formula token that is an absolute path: "/dir", "C:\x", "\\host", "~/x".
ABSOLUTE_TOKEN_RE = re.compile(r"""^["']?(?:/[A-Za-z._~-]|[A-Za-z]:[\\/]|\\\\|~[\\/])""")


@dataclass(frozen=True)
class Finding:
    code: str
    file: str
    record_id: str | None
    message: str


@dataclass
class CheckpointStatus:
    """Verdict on one file of the checkpoint folder; `reason` is None when it is fit."""
    name: str | None
    number: int | None
    seq: int | None
    reason: str | None
    missing: list[str] = field(default_factory=list)
    previous: str | None = None


@dataclass
class Report:
    errors: list[Finding] = field(default_factory=list)
    warnings: list[Finding] = field(default_factory=list)
    current_checkpoint: str | None = None
    checkpoints: list[CheckpointStatus] = field(default_factory=list)


def prefix_of(record_id: str) -> str:
    return record_id.split("-")[0]


# --- schema machinery -----------------------------------------------------

def _pattern(validator, pattern, instance, schema):
    # fullmatch: "$" must not accept a trailing newline.
    if validator.is_type(instance, "string") and not re.fullmatch(pattern, instance):
        yield ValidationError("pattern")


def _is_integer(checker, instance):
    return isinstance(instance, int) and not isinstance(instance, bool)


StrictValidator = extend(
    Draft202012Validator,
    validators={"pattern": _pattern},
    type_checker=Draft202012Validator.TYPE_CHECKER.redefine("integer", _is_integer),
)

FORMATS = FormatChecker(formats=())


@FORMATS.checks("date-time", raises=())
def _is_rfc3339(value):
    if not isinstance(value, str):
        return True
    try:
        return datetime.fromisoformat(value).tzinfo is not None
    except ValueError:
        return False


def load_schemas() -> dict:
    documents = {p.stem.removesuffix(".schema"): json.loads(p.read_text(encoding="utf-8"))
                 for p in SCHEMAS.glob("*.schema.json")}
    registry = Registry().with_resources(
        (doc["$id"], Resource.from_contents(doc)) for doc in documents.values())
    return {name: StrictValidator(doc, registry=registry, format_checker=FORMATS)
            for name, doc in documents.items()}


def parse_json(data: bytes):
    """Parse UTF-8 JSON (BOM allowed); raises ValueError when it is not valid."""
    def reject(constant):
        raise ValueError(constant)

    try:
        return json.loads(data.decode("utf-8-sig"), parse_constant=reject)
    except (UnicodeDecodeError, RecursionError) as error:
        raise ValueError("unreadable") from error


def version_of(document):
    value = document.get("format_version") if isinstance(document, dict) else None
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def schema_notes(validator, document) -> list[tuple[tuple, str]]:
    """Return (path, note) pairs that mention schema keywords only, never data."""
    notes = []
    for error in validator.iter_errors(document):
        leaf = error
        while leaf.context:
            leaf = best_match(leaf.context)
        where = "/".join(str(p) for p in leaf.absolute_path) or "root"
        note = f"{leaf.validator} violated at {where}"
        if leaf.validator == "required" and isinstance(leaf.instance, dict):
            missing = [k for k in leaf.validator_value if k not in leaf.instance]
            note += f" (missing {', '.join(missing)})"
        notes.append((tuple(error.absolute_path), note))
    return notes


# --- small helpers ----------------------------------------------------------

def is_unsafe_path(path: str) -> bool:
    if not path or path.startswith("/") or "\\" in path or ":" in path or DRIVE_RE.match(path):
        return True
    return any(part in ("", ".", "..") for part in path.split("/"))


def has_absolute_token(text: str) -> bool:
    return any(ABSOLUTE_TOKEN_RE.match(token) for token in text.split())


def is_http_url(url: str) -> bool:
    try:
        parts = urlsplit(url)
    except ValueError:
        return False
    return parts.scheme in ("http", "https") and bool(parts.netloc)


def sha256_of(path: Path) -> str | None:
    """Digest of the raw bytes; None when the file cannot be read."""
    digest = hashlib.sha256()
    try:
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1 << 16), b""):
                digest.update(chunk)
    except OSError:
        return None
    return digest.hexdigest()


def safe_id(record):
    if isinstance(record, dict) and isinstance(record.get("id"), str) and ID_RE.fullmatch(record["id"]):
        return record["id"]
    return None


def is_active(record_id: str, record: dict) -> bool:
    kind = prefix_of(record_id)
    if kind == "CLM":
        return record["status"] != "withdrawn"
    if kind == "DEC":
        return record["status"] != "superseded"
    if kind in ("SRC", "CALC", "Q", "OPT"):
        return record["record_status"] == "active"
    return True


def is_weak_source(source: dict) -> bool:
    return (source["record_status"] == "withdrawn" or not source["allowed"]
            or source["extraction"]["status"] in UNREADABLE_EXTRACTION)


def is_partial_source(source: dict) -> bool:
    return source["extraction"]["status"] == "partial" or not source["extraction"]["complete"]


def blank(value) -> bool:
    return not isinstance(value, str) or not value.strip()


TARIFF_DATE_RE = re.compile(r"(?<![0-9])[0-9]{4}-[0-9]{2}-[0-9]{2}(?![0-9])")


def names_tariff_date(text: str) -> bool:
    for found in TARIFF_DATE_RE.findall(text):
        try:
            date.fromisoformat(found)
            return True
        except ValueError:
            continue
    return False


def current_time() -> datetime:
    return datetime.now(timezone.utc)


def finite(value) -> bool:
    return not isinstance(value, float) or math.isfinite(value)


def cost_problem(cost) -> str | None:
    amounts = cost.get("amounts")
    for value in (amounts or {}).values():
        if not all(finite(part) for part in (value.values() if isinstance(value, dict) else [value])):
            return "an amount is not a finite number"
    if cost["basis"] == "unknown" and amounts is not None:
        return "amounts present although cost is unknown"
    if cost["basis"] == "estimated" and blank(cost.get("estimate_source")):
        return "estimated cost without estimate_source"
    if (cost["basis"] == "estimated" and (amounts or {}).get("money_usd") is not None
            and not names_tariff_date(cost.get("estimate_source") or "")):
        return "money estimate without the tariff date in estimate_source"
    for value in (amounts or {}).values():
        if isinstance(value, dict):
            if cost["basis"] != "estimated":
                return "a range is allowed only in an estimate"
            if value["min"] > value["max"]:
                return "range minimum exceeds maximum"
    return None


class Validator:
    def __init__(self, root: Path):
        self.root = root
        self.report = Report()
        self.schemas = load_schemas()
        self.index: dict[str, object] = {}      # ID -> usable record (ACT: {"start", "end"})
        self.seen: set[str] = set()             # every ID met, including records that failed their schema
        self.unreadable: set[str] = set()       # prefixes whose file could not be read as a whole
        self.registries: dict[str, list[dict]] = {p: [] for _, p in REGISTRIES.values()}
        self.journal: list[dict] = []
        self.extractions: dict[str, list[dict] | None] = {}   # SRC ID -> blocks, None when unusable
        self.case_id = root.name
        self.case_document = None

    # --- findings -------------------------------------------------------

    def error(self, code, file, record_id, message):
        self.report.errors.append(Finding(code, file, record_id, message))

    def warn(self, code, file, record_id, message):
        self.report.warnings.append(Finding(code, file, record_id, message))

    # --- stages ---------------------------------------------------------

    def run(self) -> Report:
        case = self.case_document = self.load_case()
        raw = self.load_files()
        if not self.check_versions(case, raw):
            self.judge_checkpoints(raw["checkpoints"])
            return self.report
        self.check_schemas(case, raw)
        self.register_ids(raw)
        self.check_records()
        self.check_journal_semantics()
        self.check_outputs()
        self.check_checkpoints(raw["checkpoints"])
        return self.report

    def read(self, rel: str):
        """Return ('missing' | 'corrupt' | 'ok', document)."""
        path = self.root / rel
        try:
            if not path.exists():
                return "missing", None
            if not path.is_file():
                return "corrupt", None
            return "ok", parse_json(path.read_bytes())
        except (ValueError, OSError):
            return "corrupt", None

    def load_case(self):
        status, case = self.read("case.json")
        if status == "missing" or (status == "ok" and case is None):
            self.error("CASE_INVALID", "case.json", None, "case.json is missing or empty")
            return None
        if status == "corrupt":
            self.error("FILE_CORRUPT", "case.json", None, "not valid UTF-8 JSON")
            return None
        return case

    def load_files(self) -> dict:
        raw = {"registries": {}, "journal": self.load_journal(), "checkpoints": [], "hidden": []}
        for rel, (_, prefix) in REGISTRIES.items():
            status, document = self.read(rel)
            if status == "corrupt":
                self.error("FILE_CORRUPT", rel, None, "not valid UTF-8 JSON")
                self.unreadable.add(prefix)
            elif status == "ok":
                raw["registries"][rel] = document
        folder = self.root / CHECKPOINT_DIR
        if folder.is_dir():
            for path in sorted(folder.iterdir()):
                if path.name.startswith("."):
                    hidden = HIDDEN_CHECKPOINT_RE.fullmatch(path.name)
                    if hidden:
                        raw["hidden"].append(f"CKP-{hidden.group(1)}")
                    continue
                match = CHECKPOINT_FILE_RE.fullmatch(path.name)
                status, document = self.read(f"{CHECKPOINT_DIR}/{path.name}") if match else ("stray", None)
                raw["checkpoints"].append({
                    "name": f"CKP-{match.group(1)}" if match else None,
                    "number": int(match.group(1)) if match else None,
                    "status": status, "document": document,
                })
        return raw

    def load_journal(self) -> list[dict]:
        path = self.root / JOURNAL
        try:
            if not path.exists():
                return []
            if not path.is_file():
                raise OSError("not a file")
            lines = [line for line in path.read_bytes().split(b"\n") if line.strip()]
        except OSError:
            self.error("FILE_CORRUPT", JOURNAL, None, "cannot be read")
            self.unreadable.add("ACT")
            return []
        parsed = []
        for number, line in enumerate(lines, start=1):
            try:
                parsed.append(parse_json(line))
            except ValueError:
                if number == len(lines):
                    self.warn("JOURNAL_TORN", JOURNAL, None, f"last line {number} is cut off and ignored")
                else:
                    self.error("FILE_CORRUPT", JOURNAL, None, f"line {number} is not valid UTF-8 JSON")
                    self.unreadable.add("ACT")
                    return []
        return parsed

    def check_versions(self, case, raw) -> bool:
        """False when processing must stop (unknown version). Checkpoints are judged separately."""
        files = [("case.json", case)] if case is not None else []
        files += list(raw["registries"].items())
        files += [(JOURNAL, line) for line in raw["journal"]]
        unsupported = {}
        for file, document in files:
            version = version_of(document)
            if version is not None and version not in SUPPORTED_VERSIONS:
                unsupported.setdefault(file, version)
        for file in unsupported:
            self.error("FORMAT_UNSUPPORTED", file, None, "format version is not supported")
        if unsupported:
            return False
        case_version = version_of(case)
        if case_version is None:
            return True
        mixed = set()
        for file, document in files:
            version = version_of(document)
            if version is not None and version != case_version and file not in mixed:
                mixed.add(file)
                self.error("FORMAT_MIXED", file, None, "format version differs from case.json")
        for file in mixed:
            if file in REGISTRIES:
                raw["registries"].pop(file, None)
                self.unreadable.add(REGISTRIES[file][1])
            if file == JOURNAL:
                raw["journal"] = []
                self.unreadable.add("ACT")
        return True

    def check_schemas(self, case, raw):
        if case is not None:
            for _, note in schema_notes(self.schemas["case"], case):
                self.error("SCHEMA", "case.json", None, note)
            if isinstance(case, dict) and isinstance(case.get("case_id"), str):
                self.case_id = case["case_id"]
                if self.case_id != self.root.name:
                    self.error("CASE_INVALID", "case.json", None, "case_id differs from the folder name")
        for rel, document in raw["registries"].items():
            schema_name, prefix = REGISTRIES[rel]
            self.read_registry(rel, schema_name, prefix, document)
        self.read_journal(raw["journal"])

    def read_registry(self, rel, schema_name, prefix, document):
        notes = schema_notes(self.schemas[schema_name], document)
        records = document.get("records") if isinstance(document, dict) else None
        bad = set()
        for path, note in notes:
            index = path[1] if len(path) > 1 and path[0] == "records" and isinstance(path[1], int) else None
            record = records[index] if index is not None and isinstance(records, list) else None
            self.error("SCHEMA", rel, safe_id(record), note)
            if index is None:
                self.unreadable.add(prefix)
            else:
                bad.add(index)
        if prefix in self.unreadable or not isinstance(records, list):
            return
        for position, record in enumerate(records):
            record_id = safe_id(record)
            if position in bad:
                # A record of another type in this file is not registered under its stated ID.
                if record_id and prefix_of(record_id) == prefix:
                    self.register(record_id, None, rel)
            else:
                self.registries[prefix].append(record)
                self.register(record_id, record, rel)

    def register(self, record_id, record, file):
        if record_id in self.seen:
            self.error("ID_DUPLICATE", file, record_id, "ID is used more than once")
            return
        self.seen.add(record_id)
        if record is not None:
            self.index[record_id] = record

    def read_journal(self, lines):
        for position, line in enumerate(lines, start=1):
            notes = schema_notes(self.schemas["action"], line)
            for _, note in notes:
                self.error("SCHEMA", JOURNAL, safe_id(line), f"line {position}: {note}")
            if notes:
                if safe_id(line) and prefix_of(line["id"]) == "ACT":
                    self.seen.add(line["id"])
            else:
                self.journal.append(line)

    def register_ids(self, raw):
        for line in self.journal:
            self.seen.add(line["id"])
            entry = self.index.setdefault(line["id"], {"start": None, "end": None})
            if line["phase"] == "start":
                if entry["start"] is not None:
                    self.error("ID_DUPLICATE", JOURNAL, line["id"], "ID is used more than once")
                else:
                    entry["start"] = line
            elif entry["end"] is not None:
                self.error("ACTION_ORDER", JOURNAL, line["id"], "second end line")
            else:
                if entry["start"] is None:
                    self.error("ACTION_ORDER", JOURNAL, line["id"], "end line without start")
                entry["end"] = line
        for item in raw["checkpoints"]:
            if item["name"]:
                self.seen.add(item["name"])
        self.seen.update(raw["hidden"])

    # --- references -----------------------------------------------------

    def ref(self, ref_id, types, file, owner):
        """Resolve a reference; report a missing or mistyped target."""
        kind = prefix_of(ref_id)
        if kind not in types:
            self.error("ID_WRONG_TYPE", file, owner, f"{ref_id} is not of type {'/'.join(sorted(types))}")
            return None
        if kind in self.unreadable:
            return None
        if ref_id in self.index:
            return self.index[ref_id]
        if ref_id not in self.seen:
            self.error("ID_MISSING", file, owner, f"{ref_id} does not exist")
        return None

    def revoked(self, ref_id) -> bool:
        record = self.index.get(ref_id)
        return (prefix_of(ref_id) not in ("ACT", "CKP") and isinstance(record, dict)
                and not is_active(ref_id, record))

    # --- record rules ---------------------------------------------------

    def check_records(self):
        self.check_sources()
        self.check_claims()
        self.check_calculations()
        self.check_questions()
        self.check_options()
        self.check_decisions()
        self.check_times()

    def check_times(self):
        """Registries are written by hand: a guessed stamp would pass for the case's real timing."""
        latest = current_time() + CLOCK_TOLERANCE
        for prefix, records in self.registries.items():
            for record in records:
                nested = [record.get(key) if isinstance(record.get(key), dict) else {} for key in ("proposal", "choice")]
                for stamp in (record.get("created_at"), nested[0].get("proposed_at"), nested[1].get("chosen_at")):
                    moment = datetime.fromisoformat(stamp) if isinstance(stamp, str) else None
                    if moment is not None and moment.tzinfo is not None and moment > latest:
                        self.error("TIME_IN_FUTURE", REGISTRY_FILE[prefix], record["id"],
                                    f"{stamp} is later than now: take the time from the system clock")

    def check_reason(self, file, record):
        status = record.get("record_status", record.get("status"))
        if status == "withdrawn" and blank(record.get("status_reason")):
            self.error("STATUS_NO_REASON", file, record["id"], "withdrawn without status_reason")

    def check_sources(self):
        file = "evidence/sources.json"
        for record in self.registries["SRC"]:
            rid = record["id"]
            self.check_reason(file, record)
            location = record["location"]
            if "url" in location and not is_http_url(location["url"]):
                self.error("PATH_UNSAFE", file, rid, "location.url is not an http or https address")
            if "source_url" in record and not is_http_url(record["source_url"]):
                self.error("PATH_UNSAFE", file, rid, "source_url is not an http or https address")
            path = location.get("path")
            if path is None:
                continue
            if is_unsafe_path(path) or not path.startswith("sources/"):
                self.error("PATH_UNSAFE", file, rid, "location.path is unsafe or outside sources/")
                continue
            if not is_active(rid, record):
                continue
            target = self.root / path
            digest = sha256_of(target) if self.inside_root(target) and target.is_file() else None
            if digest is None:
                self.error("SOURCE_MISSING", file, rid, "source file is missing or unreadable")
            elif digest != record["sha256"]:
                self.error("SOURCE_CHANGED", file, rid, "source file differs from its sha256")

    def inside_root(self, target: Path) -> bool:
        try:
            target.resolve().relative_to(self.root.resolve())
            return True
        except (ValueError, OSError):
            return False

    def extraction_blocks(self, source) -> list[dict] | None:
        """Blocks of a source, read once; None when the file is absent or corrupt (already reported)."""
        source_id = source["id"]
        if source_id not in self.extractions:
            self.extractions[source_id] = self.load_extraction(source)
        return self.extractions[source_id]

    def load_extraction(self, source) -> list[dict] | None:
        rel = f"sources/extracted/{source['id']}.jsonl"
        target = self.root / rel
        try:
            if not target.exists() and not target.is_symlink():
                if source["extraction"]["status"] in ("ok", "partial"):
                    self.warn("EXTRACT_MISSING", rel, source["id"], "no extracted text: locators cannot be checked")
                return None
            if not target.is_file() or not self.inside_root(target):
                raise ValueError("not a file inside the case")
            return load_blocks(target)
        except (ValueError, OSError):
            self.error("FILE_CORRUPT", rel, source["id"], "extracted text is not valid UTF-8 JSON blocks")
            return None

    def check_locator(self, file, owner, source, locator):
        if source is None:
            return
        if source["type"] not in LOCATOR_SOURCE_TYPES[locator["kind"]]:
            self.error("LOCATOR_MISMATCH", file, owner, f"locator {locator['kind']} does not fit source type {source['type']}")
            return
        if source["extraction"]["status"] in UNREADABLE_EXTRACTION:
            return
        blocks = self.extraction_blocks(source)
        if blocks is None:
            return
        found = matching_blocks(blocks, locator)
        if not found:
            self.error("LOCATOR_NOT_FOUND", file, owner, f"locator {locator['kind']} points to no block of {source['id']}")
        elif "quote" in locator and not any(quote_in(locator["quote"], block["text"]) for block in found):
            self.error("QUOTE_NOT_FOUND", file, owner, f"quote is not in the text of the block of {source['id']}")

    def check_claims(self):
        file = "evidence/claims.json"
        for claim in self.registries["CLM"]:
            self.check_reason(file, claim)
            if is_active(claim["id"], claim):
                self.check_claim(file, claim)

    def check_claim(self, file, claim):
        cid = claim["id"]
        confirmed = claim["status"] == "confirmed"
        sources = {}
        for item in claim["supports"] + claim["contradicts"]:
            source = self.ref(item["source_id"], {"SRC"}, file, cid)
            self.check_locator(file, cid, source, item["locator"])
            sources[item["source_id"]] = source
        derived = [(d, self.ref(d, {"CLM", "CALC"}, file, cid)) for d in claim["derived_from"]]
        if claim["kind"] == "inference" and not claim["derived_from"]:
            self.error("CLAIM_NO_BASIS", file, cid, "inference without derived_from")
        if confirmed and claim["kind"] != "inference" and not claim["supports"]:
            self.error("CLAIM_NO_BASIS", file, cid, "confirmed without supports")
        if confirmed and claim["kind"] in ("hypothesis", "forecast"):
            self.error("FACT_NOT_CONFIRMABLE", file, cid, f"a {claim['kind']} cannot be confirmed")
        if claim["status"] == "contested" and not claim["contradicts"]:
            self.error("CLAIM_NOT_CONTESTED", file, cid, "contested without contradicts")
        if not confirmed:
            return
        if any(blank(item.get("resolution")) for item in claim["contradicts"]):
            self.error("CLAIM_CONFLICT_HIDDEN", file, cid, "contradiction without resolution")
        partial = {}
        for source_id in dict.fromkeys(item["source_id"] for item in claim["supports"]):
            source = sources[source_id]
            if source is None:
                continue
            if is_weak_source(source):
                self.error("CLAIM_WEAK_SOURCE", file, cid, f"{source_id} is withdrawn, not allowed or unreadable")
            elif is_partial_source(source):
                partial[source_id] = True
        for derived_id, target in derived:
            if target is None:
                continue
            if prefix_of(derived_id) == "CLM":
                weak = target["status"] != "confirmed"
            else:
                weak, calc_partial = self.calculation_weakness(target, {target["id"]})
                partial.update(dict.fromkeys(calc_partial, True))
            if weak:
                self.error("CLAIM_WEAK_DERIVATION", file, cid, f"{derived_id} is not a confirmed or computed basis")
        if self.derives_from_itself(claim):
            self.error("CLAIM_WEAK_DERIVATION", file, cid, "derived_from chain leads back to this claim")
        for source_id in partial:
            self.warn("SOURCE_PARTIAL", file, cid, f"{source_id} was read only in part")

    def calculation_weakness(self, calc, visiting):
        """Return (weak, partially read source IDs) for a calculation and everything it is built on."""
        weak = calc["record_status"] != "active" or calc["result"]["status"] != "computed"
        partial = []
        for item in calc["inputs"]:
            if "source_id" in item:
                source = self.index.get(item["source_id"])
                if prefix_of(item["source_id"]) != "SRC" or not isinstance(source, dict):
                    continue
                if is_weak_source(source):
                    weak = True
                elif is_partial_source(source):
                    partial.append(item["source_id"])
            else:
                inner = self.index.get(item["calculation_id"])
                if prefix_of(item["calculation_id"]) != "CALC" or not isinstance(inner, dict) or inner["id"] in visiting:
                    continue
                inner_weak, inner_partial = self.calculation_weakness(inner, visiting | {inner["id"]})
                weak = weak or inner_weak
                partial += inner_partial
        return weak, partial

    def derives_from_itself(self, claim):
        seen = set()
        pending = list(claim["derived_from"])
        while pending:
            current = pending.pop()
            if current == claim["id"]:
                return True
            if current in seen:
                continue
            seen.add(current)
            target = self.index.get(current)
            if isinstance(target, dict):
                pending.extend(target.get("derived_from", []))
        return False

    def check_calculations(self):
        file = "evidence/calculations.json"
        for calc in self.registries["CALC"]:
            cid = calc["id"]
            self.check_reason(file, calc)
            if has_absolute_token(calc["reproduce"]):
                self.error("PATH_UNSAFE", file, cid, "reproduce contains an absolute path")
            if not is_active(cid, calc):
                continue
            errors_before, resolved = len(self.report.errors), True
            for item in calc["inputs"]:
                if "source_id" in item:
                    source = self.ref(item["source_id"], {"SRC"}, file, cid)
                    self.check_locator(file, cid, source, item["locator"])
                    target = item["source_id"]
                else:
                    source = self.ref(item["calculation_id"], {"CALC"}, file, cid)
                    target = item["calculation_id"]
                resolved = resolved and source is not None
                if self.revoked(target):
                    self.error("WITHDRAWN_USED", file, cid, f"input uses withdrawn {target}")
            if "formula" not in calc["method"]:
                if calc["result"]["status"] == "computed":
                    self.warn("CALC_UNVERIFIED", file, cid, "computed by hand: no formula to recompute it")
                continue
            # A record with a link, locator or withdrawal error is already reported; recomputing it would repeat that.
            if resolved and len(self.report.errors) == errors_before:
                reasons, checked = self.recompute(calc)
                for reason in reasons:
                    self.error("CALC_MISMATCH", file, cid, reason)
                if not checked and not reasons:
                    self.warn("CALC_UNVERIFIED", file, cid, "inputs not rechecked: a source has no extracted text")

    def recompute(self, calc) -> tuple[list[str], bool]:
        def source_of(source_id):
            source = self.index.get(source_id)
            return source if isinstance(source, dict) and prefix_of(source_id) == "SRC" else None

        def blocks_of(source):
            if source["extraction"]["status"] in UNREADABLE_EXTRACTION:
                return None
            return self.extraction_blocks(source)

        def calculation_of(calc_id):
            record = self.index.get(calc_id)
            return record if isinstance(record, dict) and prefix_of(calc_id) == "CALC" else None

        try:
            return calc_engine.recompute(calc, source_of, blocks_of, calculation_of)
        except Exception as error:  # a damaged record must be a finding, never a validator crash
            return [f"recompute failed: {type(error).__name__}"], False

    def check_questions(self):
        file = "research/questions.json"
        for question in self.registries["Q"]:
            self.check_reason(file, question)
            if not is_active(question["id"], question):
                continue
            for finding in question["findings"]:
                self.ref(finding, {"CLM", "CALC"}, file, question["id"])
                if self.revoked(finding):
                    self.error("WITHDRAWN_USED", file, question["id"], f"uses withdrawn {finding}")

    def unconfirmed_basis(self, option):
        """IDs of claims and calculations under an option that are not firm."""
        weak = []
        for basis_id in option["basis"]:
            target = self.index.get(basis_id)
            if not isinstance(target, dict):
                continue
            if prefix_of(basis_id) == "CLM" and target["status"] in ("unconfirmed", "contested"):
                weak.append(basis_id)
            elif prefix_of(basis_id) == "CALC" and target["result"]["status"] != "computed":
                weak.append(basis_id)
        return weak

    def check_options(self):
        file = "decisions/options.json"
        for option in self.registries["OPT"]:
            self.check_reason(file, option)
            if not is_active(option["id"], option):
                continue
            for basis_id in option["basis"]:
                self.ref(basis_id, {"CLM", "CALC"}, file, option["id"])
                if self.revoked(basis_id):
                    self.error("WITHDRAWN_USED", file, option["id"], f"basis uses withdrawn {basis_id}")
            weak = self.unconfirmed_basis(option)
            if not option["basis"]:
                self.warn("BASIS_UNCONFIRMED", file, option["id"], "option has no basis")
            elif weak:
                self.warn("BASIS_UNCONFIRMED", file, option["id"], f"basis is not confirmed: {', '.join(weak)}")

    # --- decisions ------------------------------------------------------

    def check_decisions(self):
        file = "decisions/decisions.json"
        decisions = self.registries["DEC"]
        for decision in decisions:
            if decision.get("supersedes"):
                self.ref(decision["supersedes"], {"DEC"}, file, decision["id"])
            if decision["proposal"] is None and decision.get("choice") is None:
                # A replaced decision was once a proposal or a choice: an empty record only marks
                # a choice as void, and that is what a replacement with supersedes says.
                self.error("DECISION_CHOICE_STATE", file, decision["id"], "neither a proposal nor a choice")
            if is_active(decision["id"], decision):
                self.check_decision(file, decision)
        self.check_supersede_chains(file, decisions)
        by_topic = {}
        for decision in decisions:
            if decision["status"] != "superseded":
                by_topic.setdefault(decision["topic"], []).append(decision["id"])
        for ids in by_topic.values():
            if len(ids) > 1:
                self.error("DECISION_MULTIPLE_ACTIVE", file, ids[1], f"{len(ids)} unreplaced decisions on one topic")

    def check_decision(self, file, decision):
        did = decision["id"]
        proposal, choice = decision["proposal"], decision.get("choice")
        if decision["status"] == "chosen" and choice is None:
            self.error("DECISION_CHOICE_STATE", file, did, "chosen without choice")
        if decision["status"] == "proposed" and choice is not None:
            self.error("DECISION_CHOICE_STATE", file, did, "proposed with a choice")
        if decision["status"] == "proposed" and proposal is None:
            self.error("DECISION_CHOICE_STATE", file, did, "proposed without a proposal")
        if choice is not None and choice["option_id"] is None and decision["topic"] == "direction":
            self.error("DECISION_CHOICE_STATE", file, did, "a direction choice names no option")
        if choice is not None and choice.get("delegated") and (
                proposal is None or proposal["option_id"] is None or choice["option_id"] != proposal["option_id"]):
            self.error("DECISION_CHOICE_STATE", file, did, "a choice left to the agent must take the proposed option")
        elif choice is not None and choice.get("delegated") and not self.proposal_came_first(proposal, choice):
            self.error("DECISION_CHOICE_STATE", file, did, "a choice left to the agent comes before the proposal it takes")
        weak = []
        for part in (proposal, choice):
            if part is None or part["option_id"] is None:
                continue
            option = self.ref(part["option_id"], {"OPT"}, file, did)
            if option is None:
                continue
            if not is_active(option["id"], option):
                self.error("WITHDRAWN_USED", file, did, f"uses withdrawn {option['id']}")
            weak += self.unconfirmed_basis(option)
        if weak:
            self.warn("BASIS_UNCONFIRMED", file, did, f"basis is not confirmed: {', '.join(dict.fromkeys(weak))}")
        if proposal is not None:
            action = self.ref(proposal["action_id"], {"ACT"}, file, did)
            if action is not None and self.action_kind(action) != "model":
                self.error("DECISION_NOT_HUMAN", file, did, "proposal.action_id is not a model action")
        if choice is not None:
            action = self.ref(choice["action_id"], {"ACT"}, file, did)
            if action is not None and not self.is_human_input_for(action, did):
                self.error("DECISION_NOT_HUMAN", file, did, "choice.action_id is not a completed human_input for this decision")

    def proposal_came_first(self, proposal, choice):
        """A choice left to the agent takes a recommendation the person had already been shown.

        The model action of the proposal must end in the journal before the person's action starts,
        so a proposal written after "decide yourself" cannot pass as the one the person delegated to.
        """
        made = self.index.get(proposal["action_id"]) if isinstance(proposal.get("action_id"), str) else None
        asked = self.index.get(choice["action_id"]) if isinstance(choice.get("action_id"), str) else None
        if not isinstance(made, dict) or not isinstance(asked, dict) or made.get("end") is None or asked.get("start") is None:
            return False
        lines = [id(line) for line in self.journal]
        return lines.index(id(made["end"])) < lines.index(id(asked["start"]))

    @staticmethod
    def action_kind(action):
        return action["start"]["kind"] if action["start"] else None

    def is_human_input_for(self, action, decision_id):
        end = action["end"]
        return (self.action_kind(action) == "human_input" and end is not None
                and end["outcome"] == "completed" and decision_id in end["result_refs"]
                and end.get("human", {}).get("help", "choice") == "choice")

    def check_supersede_chains(self, file, decisions):
        by_id = {d["id"]: d for d in decisions}
        replacers = {}
        for decision in decisions:
            if decision.get("supersedes"):
                replacers.setdefault(decision["supersedes"], []).append(decision["id"])
        for decision in decisions:
            did, target = decision["id"], decision.get("supersedes")
            if target is not None:
                if blank(decision.get("change_reason")):
                    self.error("DECISION_SUPERSEDE", file, did, "supersedes without change_reason")
                if target in by_id:
                    if by_id[target]["status"] != "superseded":
                        self.error("DECISION_SUPERSEDE", file, did, f"{target} is replaced but not marked superseded")
                    if by_id[target]["topic"] != decision["topic"]:
                        self.error("DECISION_SUPERSEDE", file, did, f"topic differs from the replaced {target}")
            if decision["status"] == "superseded" and did not in replacers:
                self.error("DECISION_SUPERSEDE", file, did, "superseded but nothing supersedes it")
        for target, ids in replacers.items():
            if len(ids) > 1:
                self.error("DECISION_SUPERSEDE", file, ids[1], f"{target} is replaced by {len(ids)} decisions")
        reported = set()
        for start in sorted(by_id):
            seen, node = [], start
            while node in by_id and node not in seen and node not in reported:
                seen.append(node)
                node = by_id[node].get("supersedes")
            if node in seen:
                cycle = seen[seen.index(node):]
                reported.update(cycle)
                self.error("DECISION_SUPERSEDE", file, min(cycle), "supersedes chain forms a cycle")

    # --- journal --------------------------------------------------------

    def check_journal_semantics(self):
        for line in self.journal:
            rid = line["id"]
            if line["phase"] == "start":
                for ref_id in line["input_refs"]:
                    self.ref(ref_id, ALL_PREFIXES, JOURNAL, rid)
                self.check_retry(rid, line)
            else:
                for ref_id in line["result_refs"]:
                    self.ref(ref_id, ALL_PREFIXES, JOURNAL, rid)
                problem = cost_problem(line["cost"])
                if problem:
                    self.error("ACTION_COST", JOURNAL, rid, problem)
                if "human" in line:
                    self.check_human(rid, line)
        for rid, entry in self.index.items():
            if prefix_of(rid) == "ACT" and entry["start"] is not None:
                if entry["end"] is None or entry["end"]["outcome"] == "unknown":
                    self.warn("ACTION_UNKNOWN", JOURNAL, rid, "outcome is unknown: check before repeating")

    def check_human(self, rid, line):
        entry = self.index.get(rid)
        start = entry.get("start") if isinstance(entry, dict) else None
        human = line["human"]
        if start is None or start["kind"] != "human_input":
            self.error("ACTION_HUMAN", JOURNAL, rid, "human help is recorded on an action that is not human_input")
        elif human["help"] == "choice" and not any(prefix_of(ref) == "DEC" for ref in line["result_refs"]):
            self.error("ACTION_HUMAN", JOURNAL, rid, "a choice without a decision in result_refs")
        elif human["help"] == "workaround" and blank(human.get("note")):
            self.error("ACTION_HUMAN", JOURNAL, rid, "a workaround without a note")
        elif not finite(human.get("reported_active_seconds")):
            self.error("ACTION_HUMAN", JOURNAL, rid, "reported time is not a finite number")
        elif human.get("covers_case") and "reported_active_seconds" not in human:
            self.error("ACTION_HUMAN", JOURNAL, rid, "a total for the case without the time")

    def check_retry(self, rid, line):
        if "retry_of" in line:
            self.ref(line["retry_of"], {"ACT"}, JOURNAL, rid)
            if line["retry_of"] == rid:
                self.error("ACTION_RETRY", JOURNAL, rid, "an action cannot retry itself")
            if blank(line.get("retry_reason")):
                self.error("ACTION_RETRY", JOURNAL, rid, "retry_of without retry_reason")
        elif "retry_reason" in line:
            self.error("ACTION_RETRY", JOURNAL, rid, "retry_reason without retry_of")

    # --- outputs --------------------------------------------------------

    def check_outputs(self):
        folder = self.root / "outputs"
        if not folder.is_dir():
            return
        for path in sorted(folder.rglob("*.md")):
            file = path.relative_to(self.root).as_posix()
            if any(part.startswith(".") for part in file.split("/")):
                continue
            try:
                text = path.read_bytes().decode("utf-8-sig")
            except (UnicodeDecodeError, OSError):
                self.error("FILE_CORRUPT", file, None, "not valid UTF-8")
                continue
            links, malformed = {}, False
            for match in BRACKET_RE.finditer(text):
                parts = [part.strip() for part in match.group(1).split(",")]
                if not all(LOOKS_LIKE_ID_RE.fullmatch(part) for part in parts):
                    continue
                for part in parts:
                    if ID_RE.fullmatch(part):
                        links[part] = True
                    else:
                        malformed = True
            if malformed:
                self.error("OUTPUT_LINK", file, None, "a bracket looks like a record reference but is malformed")
            for link in links:
                self.check_output_link(file, link)

    def check_output_link(self, file, link):
        kind = prefix_of(link)
        if kind in ("ACT", "CKP"):
            self.error("OUTPUT_LINK", file, link, "link to an action or checkpoint")
        elif kind in self.unreadable:
            return
        elif link not in self.seen:
            self.error("OUTPUT_LINK", file, link, "link to a record that does not exist")
        elif self.revoked(link):
            self.error("WITHDRAWN_USED", file, link, "text uses a withdrawn record")
        elif kind == "CLM" and link in self.index and self.index[link]["status"] in ("unconfirmed", "contested"):
            self.warn("BASIS_UNCONFIRMED", file, link, "text relies on an unconfirmed claim")

    # --- checkpoints ----------------------------------------------------

    def judge_checkpoints(self, items):
        """Decide which checkpoint files are fit and record the status of each. Returns (fit, unfit)."""
        seq_of = {}
        for item in items:
            doc = item["document"]
            seq = doc.get("seq") if isinstance(doc, dict) else None
            if item["name"] and isinstance(seq, int) and not isinstance(seq, bool):
                seq_of[item["name"]] = seq
        names = {item["name"] for item in items if item["name"]}
        fit, unfit = [], []
        for item in items:
            reason = self.checkpoint_problem(item, seq_of, names)
            (unfit if reason else fit).append(item)
            item["reason"] = reason
            document = item["document"]
            seq = document.get("seq") if isinstance(document, dict) else None
            previous = document.get("previous") if isinstance(document, dict) else None
            self.report.checkpoints.append(CheckpointStatus(
                item["name"], item["number"],
                seq if isinstance(seq, int) and not isinstance(seq, bool) else None,
                reason, item["missing"], previous if isinstance(previous, str) else None))
        return fit, unfit

    def check_checkpoints(self, items):
        if not items:
            return
        fit, unfit = self.judge_checkpoints(items)
        if not fit:
            self.error("CHECKPOINT_NONE_VALID", CHECKPOINT_DIR, None,
                       "no valid checkpoint (" + "; ".join(self.describe(i) for i in unfit) + ")")
            return
        for item in fit:
            self.check_checkpoint_links(item["document"])
        current = max(fit, key=lambda i: (i["document"]["seq"], i["name"]))["document"]
        self.report.current_checkpoint = current["id"]
        file = f"{CHECKPOINT_DIR}/{current['id']}.json"
        before = CHECKPOINT_FILE_RE.fullmatch(f"{current['previous']}.json") if current["previous"] else None
        floor = int(before.group(1)) if before else 0
        lost = [i for i in unfit if i["number"] is None or i["number"] > floor]
        if lost:
            self.warn("CHECKPOINT_FALLBACK", file, current["id"],
                      f"{current['id']} (seq {current['seq']}) is current; unusable newer files: "
                      + "; ".join(self.describe(i) for i in lost))
        self.check_checkpoint_claims(file, current)
        stale = [path for path, digest in
                 next(i for i in fit if i["document"] is current)["hashes"].items() if digest != self.stored(current, path)]
        if stale:
            self.warn("CHECKPOINT_STALE", file, current["id"], f"changed since the checkpoint: {', '.join(stale)}")

    @staticmethod
    def describe(item):
        label = f"{item['name']} (seq {item['number']})" if item["name"] else "unexpected file"
        return f"{label}: {item['reason']}"

    @staticmethod
    def stored(document, path):
        return next(a["sha256"] for a in document["artifacts"] if a["path"] == path)

    def checkpoint_problem(self, item, seq_of, names) -> str | None:
        name, document = item["name"], item["document"]
        file = f"{CHECKPOINT_DIR}/{name}.json"
        item["hashes"] = {}
        item["missing"] = []
        if item["status"] == "stray":
            return "unexpected file name"
        if item["status"] != "ok":
            return "cannot be parsed"
        version, case_version = version_of(document), version_of(self.case_document)
        if version is not None and version not in SUPPORTED_VERSIONS:
            return "unsupported format version"
        if version is not None and case_version is not None and version != case_version:
            return "format version differs from case.json"
        if not self.schemas["checkpoint"].is_valid(document):
            return "violates the schema"
        if document["id"] != name:
            return "id differs from the file name"
        if document["case_id"] != self.case_id:
            return "belongs to another case"
        if cost_problem(document["totals"]["cost"]):
            return "inconsistent cost"
        previous = document["previous"]
        if previous is not None:
            if previous not in names:
                return "previous checkpoint is missing"
            # An unreadable previous file has no seq to compare with.
            if previous in seq_of and seq_of[previous] + 1 != document["seq"]:
                return "seq does not follow previous"
        for artifact in document["artifacts"]:
            if is_unsafe_path(artifact["path"]):
                self.error("PATH_UNSAFE", file, name, "artifact path is unsafe")
                continue
            target = self.root / artifact["path"]
            digest = sha256_of(target) if self.inside_root(target) and target.is_file() else None
            if digest is None:
                item["missing"].append(artifact["path"])
                continue
            item["hashes"][artifact["path"]] = digest
        if item["missing"]:
            return "an artifact is missing or unreadable"
        return None

    def check_checkpoint_links(self, document):
        file = f"{CHECKPOINT_DIR}/{document['id']}.json"
        for action_id in document["completed_actions"]:
            self.ref(action_id, {"ACT"}, file, document["id"])
        for decision_id in document["human_choices"]:
            self.ref(decision_id, {"DEC"}, file, document["id"])

    def check_checkpoint_claims(self, file, document):
        for action_id in document["completed_actions"]:
            entry = self.index.get(action_id)
            if prefix_of(action_id) == "ACT" and isinstance(entry, dict):
                if entry["end"] is None or entry["end"]["outcome"] != "completed":
                    self.error("CHECKPOINT_CLAIM", file, document["id"], f"{action_id} is listed as completed but did not complete")
        for decision_id in document["human_choices"]:
            decision = self.index.get(decision_id)
            if prefix_of(decision_id) == "DEC" and isinstance(decision, dict) and decision.get("choice") is None:
                self.error("CHECKPOINT_CLAIM", file, document["id"], f"{decision_id} is listed as a human choice but has no choice")


def validate_case(case_dir: Path) -> Report:
    case_dir = Path(case_dir).resolve()
    if not case_dir.is_dir():
        raise NotADirectoryError(case_dir.name)
    return Validator(case_dir).run()


def format_text(report: Report) -> str:
    lines = []
    for label, findings in (("ERROR", report.errors), ("WARNING", report.warnings)):
        for f in findings:
            lines.append(f"{label} {f.code} {f.file} {f.record_id or '-'} {f.message}")
    lines.append(f"errors: {len(report.errors)}, warnings: {len(report.warnings)}, "
                 f"current checkpoint: {report.current_checkpoint or '-'}")
    return "\n".join(lines)


def main(argv=None) -> int:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description="Validate a case folder.")
    parser.add_argument("case_dir", type=Path)
    parser.add_argument("--json", action="store_true", help="print machine-readable lists")
    args = parser.parse_args(argv)
    try:
        report = validate_case(args.case_dir)
    except NotADirectoryError:
        print("error: the case folder does not exist", file=sys.stderr)
        return 2
    except Exception:  # noqa: BLE001 - the text of an exception could quote case data
        if args.json:
            print(json.dumps({"errors": [], "warnings": [], "current_checkpoint": None, "internal_error": True}))
        else:
            print("error: internal validator failure", file=sys.stderr)
        return 3
    if args.json:
        print(json.dumps({
            "errors": [asdict(f) for f in report.errors],
            "warnings": [asdict(f) for f in report.warnings],
            "current_checkpoint": report.current_checkpoint,
        }, ensure_ascii=False, indent=2))
    else:
        print(format_text(report))
    return 1 if report.errors else 0


if __name__ == "__main__":
    sys.exit(main())
