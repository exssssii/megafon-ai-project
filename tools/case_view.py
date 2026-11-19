"""Build one static HTML page of a case from its folder. Writes only outputs/case-page.html.

Commands (run from the repository root):
  build <case folder>

The first output line is JSON: {"status": "ok", "path": ..., "warnings": [...]}. Exit codes: 0 built,
4 refused (the folder or case.json is unreadable), 5 internal failure. The page has no scripts, no
clock and no network: the same folder gives the same bytes. Every string of the case is escaped;
links are only http(s) URLs of the source registry and relative paths inside the case. The page is
not pinned by a checkpoint: it can always be built again. The contract is in agent/contracts/README.md.
"""
import argparse
import html
import json
import os
import re
import sys
from datetime import datetime
from pathlib import Path
from urllib.parse import quote

from case_view_md import INLINE_RE, render, render_inline
from checkpoint import Parser, Refused, UsageError
from outputs_md import STATUS_CHOSEN_RE, STATUS_PRELIMINARY_RE, split_sections
from team import normalize, owners_of, read_team
from validate_project import CHECKPOINT_FILE_RE, ID_RE, is_http_url, is_synthetic_source, is_unsafe_path

EXIT_OK, EXIT_REFUSED, EXIT_INTERNAL = 0, 4, 5
PAGE = "outputs/case-page.html"
EMPTY = "ещё нет"
STAGES = {
    "intake": "Постановка", "research": "Исследование", "options": "Варианты", "decision": "Выбор",
    "strategies": "Стратегии", "validation_plan": "План проверки", "done": "Готово",
}
TEXTS = ("outputs/product-strategy.md", "outputs/communication-strategy.md", "outputs/validation-plan.md",
         "outputs/commitments.md")
DELIVERABLE = "outputs/deliverable.md"  # shown first among the documents when the brief set a form of the result
PLAN, COMMITMENTS, REVISIONS, ACCOUNTING = TEXTS[2], TEXTS[3], "outputs/revisions.md", "outputs/accounting.md"
ACTION_COLUMNS = ("Действие", "Ответственный", "Срок", "Показатель", "Основание")
KIND_LABELS = {"fact": ("Факт", "fact"), "inference": ("Вывод", "inference"),
               "hypothesis": ("Гипотеза", "hypothesis"), "forecast": ("Прогноз", "forecast")}
STRATEGY_KINDS = {"keep": "Сохранить", "close": "Закрыть", "develop": "Развивать"}
COST_LABELS = {"estimated": "оценена", "none": "затрат нет", "not_estimated": "не оценена"}
COMPARED_LABELS = {"direct_costs": "прямые расходы",
                   "direct_costs_and_lost_revenue": "прямые расходы и недополученная выручка"}
LIMIT_LABELS = {"fits": "укладывается", "exceeds": "не укладывается", "unknown": "неизвестно",
                "no_limit": "в брифе нет ограничения"}
BLOCKS = (("decision", "Решение"), ("numbers", "Ключевые числа"), ("team", "Команда и сроки"),
          ("options", "Варианты"), ("texts", "Стратегии и план"), ("basis", "Основания"),
          ("revisions", "Пересмотры"), ("reviews", "Независимая проверка"), ("accounting", "Учёт"))
REVIEW_TITLE_RE = re.compile(r"#\s*(Проверка\s+[0-9]+)(?:\s*[—-]\s*(\S+))?\s*")
REVIEW_TOTAL_RE = re.compile(r"замечаний\s+(\d+),\s*принято\s+(\d+),\s*отклонено\s+(\d+)")
REVIEW_AFTER_RE = re.compile(r"После решения:\s*(DEC-[0-9]{4,})")
REVISION_HEADING_RE = re.compile(r"Пересмотр (\d+)(?: — (.*))?")
MAX_NUMBERS = 4

e = html.escape


MONTHS = ("января", "февраля", "марта", "апреля", "мая", "июня", "июля", "августа", "сентября", "октября",
          "ноября", "декабря")


# A journal timestamp inside the text of the accounting report.
TIMESTAMP_RE = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})")


def human_time(value: str) -> str:
    """`2026-11-05T12:30:00+03:00` as `5 ноября 2026 года, 12:30 (UTC+03:00)`; unreadable text is returned as is."""
    try:
        moment = datetime.fromisoformat(value)
    except ValueError:
        return value
    text = f"{moment.day} {MONTHS[moment.month - 1]} {moment.year} года, {moment:%H:%M}"
    offset = moment.utcoffset()
    if offset is None:
        return text
    minutes = int(offset.total_seconds()) // 60
    sign = "+" if minutes >= 0 else "-"
    return f"{text} (UTC{sign}{abs(minutes) // 60:02d}:{abs(minutes) % 60:02d})"


def text_of(value) -> str:
    return value if isinstance(value, str) else ""


def items_of(value) -> list:
    return [item for item in value if isinstance(item, (str, dict))] if isinstance(value, list) else []


def warning(code: str, file: str, note: str) -> dict:
    return {"code": code, "file": file, "note": note}


# --- reading the case --------------------------------------------------------

def read_json_file(path: Path):
    return json.loads(path.read_bytes().decode("utf-8-sig"))


def read_lines(root: Path, rel: str) -> list[str] | None:
    try:
        return (root / rel).read_bytes().decode("utf-8-sig").splitlines()
    except (OSError, UnicodeDecodeError):
        return None


def load_registry(root: Path, rel: str, warnings: list) -> list[dict]:
    path = root / rel
    if not path.is_file():
        return []
    try:
        document = read_json_file(path)
        records = document["records"]
        if not isinstance(records, list):
            raise ValueError
    except (OSError, ValueError, KeyError, TypeError):
        warnings.append(warning("REGISTRY_UNREADABLE", rel, "the registry is not read; its blocks are empty"))
        return []
    return [record for record in records if isinstance(record, dict) and ID_RE.fullmatch(text_of(record.get("id")))]


def load_case(root: Path) -> dict:
    try:
        case = read_json_file(root / "case.json")
    except (OSError, ValueError):
        raise Refused("case_unreadable", "case.json не читается") from None
    if not isinstance(case, dict):
        raise Refused("case_unreadable", "case.json не читается")
    return case


def current_checkpoint(root: Path) -> dict | None:
    folder = root / "state" / "checkpoints"
    try:
        names = [name for name in os.listdir(folder) if CHECKPOINT_FILE_RE.fullmatch(name)]
    except OSError:
        return None
    for name in sorted(names, key=lambda n: int(CHECKPOINT_FILE_RE.fullmatch(n).group(1)), reverse=True):
        try:
            document = read_json_file(folder / name)
        except (OSError, ValueError):
            continue
        if (isinstance(document, dict) and isinstance(document.get("id"), str)
                and isinstance(document.get("created_at"), str)):
            return document
    return None


def is_active(record: dict) -> bool:
    prefix = text_of(record.get("id")).split("-")[0]
    if prefix == "CLM":
        return record.get("status") != "withdrawn"
    if prefix == "DEC":
        return record.get("status") != "superseded"
    return record.get("record_status", "active") == "active"


def refs_in(lines: list[str]) -> list[str]:
    found = []
    for line in lines:
        for match in INLINE_RE.finditer(line):
            if match.group(1):
                found.extend(part.strip() for part in match.group(1).split(","))
    return found


def ordered(items) -> list:
    return list(dict.fromkeys(items))


def format_value(value) -> str:
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value)


# --- the page ----------------------------------------------------------------

class View:
    def __init__(self, root: Path):
        self.root = root
        self.warnings: list[dict] = []
        self.case = load_case(root)
        self.checkpoint = current_checkpoint(root)
        self.sources = {r["id"]: r for r in load_registry(root, "evidence/sources.json", self.warnings)}
        self.claims = {r["id"]: r for r in load_registry(root, "evidence/claims.json", self.warnings)}
        self.calcs = {r["id"]: r for r in load_registry(root, "evidence/calculations.json", self.warnings)}
        self.questions = {r["id"]: r for r in load_registry(root, "research/questions.json", self.warnings)}
        self.options = {r["id"]: r for r in load_registry(root, "decisions/options.json", self.warnings)}
        self.decisions = {r["id"]: r for r in load_registry(root, "decisions/decisions.json", self.warnings)}
        self.lines = {rel: read_lines(root, rel) for rel in (DELIVERABLE, *TEXTS, REVISIONS, ACCOUNTING)}
        self.active_options = [r for _, r in sorted(self.options.items()) if is_active(r)]
        self.option_no = {r["id"]: number for number, r in enumerate(self.active_options, 1)}
        self.direction = self.find_direction()
        self.team = read_team(root)
        if self.team:
            for problem in self.team.problems:
                self.warnings.append(warning("TEAM_FORMAT", "team.md", problem))
        self.basis_ids = self.collect_basis()
        self.basis_no = {rid: number for number, rid in enumerate(self.basis_ids, 1)}

    # decisions and options

    def find_direction(self) -> dict | None:
        found = [r for _, r in sorted(self.decisions.items())
                 if r.get("topic") == "direction" and is_active(r)]
        return found[-1] if found else None

    def option_title(self, option_id) -> str:
        option = self.options.get(option_id)
        return text_of(option.get("title")) if option else text_of(option_id)

    # basis

    def collect_basis(self) -> list[str]:
        wanted = []
        for lines in self.lines.values():
            if lines:
                wanted += refs_in(lines)
        for option in self.active_options:
            wanted += [i for i in option.get("basis", []) if isinstance(i, str)]
        wanted += self.number_ids()
        seen = set()
        stack = list(wanted)
        while stack:
            rid = stack.pop()
            if rid in seen:
                continue
            seen.add(rid)
            record = self.claims.get(rid)
            if record and is_active(record):
                stack += [i for i in record.get("derived_from", []) if isinstance(i, str)]
        for rid, question in self.questions.items():
            if is_active(question) and question.get("status") in ("partial", "blocked"):
                seen.add(rid)
        found = []
        for rid in seen:
            record = self.claims.get(rid) or self.calcs.get(rid) or self.questions.get(rid)
            if record and is_active(record):
                found.append(rid)
        order = {"CLM": 0, "CALC": 1, "Q": 2}
        return sorted(found, key=lambda rid: (order[rid.split("-")[0]], rid))

    def is_synthetic(self, rid: str, seen=None) -> bool:
        seen = seen or set()
        if rid in seen:
            return False
        seen.add(rid)
        record = self.claims.get(rid) or self.calcs.get(rid) or {}
        links = [*items_of(record.get("supports")), *items_of(record.get("inputs"))]
        for link in links:
            source = self.sources.get(link.get("source_id")) if isinstance(link, dict) else None
            if source and is_synthetic_source(source):
                return True
            if isinstance(link, dict) and link.get("calculation_id") and self.is_synthetic(link["calculation_id"], seen):
                return True
        return any(self.is_synthetic(i, seen) for i in record.get("derived_from", []) if isinstance(i, str))

    def footnote(self, rid: str) -> str | None:
        prefix = rid.split("-")[0]
        if rid in self.basis_no:
            return f'<a class="fn" href="#basis-{rid}" title="{rid}">{self.basis_no[rid]}</a>'
        if rid in self.option_no:
            title = e(self.option_title(rid))
            return f'<a class="fn fn-wide" href="#option-{rid}" title="{title}">вариант {self.option_no[rid]}</a>'
        source = self.sources.get(rid)
        if source:
            return f'<span class="ref" title="{e(text_of(source.get("title")))}">{rid}</span>'
        if prefix in ("DEC", "Q") and rid in {**self.decisions, **self.questions}:
            return f'<span class="ref">{rid}</span>'
        return None

    def inline(self, text: str) -> str:
        return render_inline(text, self.footnote)

    def md(self, lines: list[str]) -> str:
        return render(lines, self.footnote)

    # links

    def source_links(self, source: dict) -> str:
        location = source.get("location") if isinstance(source.get("location"), dict) else {}
        links = []
        path = location.get("path")
        if isinstance(path, str) and not is_unsafe_path(path) and (self.root / path).is_file():
            links.append(f'<a href="../{quote(path, safe="/")}">файл</a>')
        url = location.get("url")
        if isinstance(url, str) and is_http_url(url):
            links.append(f'<a href="{e(url)}" rel="noopener noreferrer">сайт</a>')
        return " · ".join(links)

    def locator_text(self, locator) -> str:
        if not isinstance(locator, dict):
            return ""
        kind = locator.get("kind")
        parts = {
            "section": lambda: f"раздел «{e(text_of(locator.get('heading')))}»",
            "paragraph": lambda: f"абзац {e(str(locator.get('index')))}",
            "page": lambda: f"страница {e(str(locator.get('page')))}",
            "cells": lambda: f"лист {e(text_of(locator.get('sheet')))}, ячейки {e(text_of(locator.get('range')))}",
            "rows": lambda: f"строки {e(str(locator.get('from')))}–{e(str(locator.get('to')))}",
            "doc_table": lambda: f"таблица {e(str(locator.get('table')))}, строка {e(str(locator.get('row')))}",
            "region": lambda: f"область {e(str(locator.get('index')))}: {e(text_of(locator.get('area')))}",
        }
        text = parts[kind]() if kind in parts else ""
        if isinstance(locator.get("quote"), str):
            text += f" — <q>{e(locator['quote'])}</q>"
        return text

    def evidence_line(self, link: dict) -> str:
        if not isinstance(link, dict):
            return ""
        source = self.sources.get(link.get("source_id"))
        if not source:
            if link.get("calculation_id"):
                return f"расчёт {self.footnote(link['calculation_id']) or e(str(link['calculation_id']))}"
            return ""
        synthetic = ' <span class="mark mark-synthetic">синтетика</span>' if is_synthetic_source(source) else ""
        pieces = [f'<span class="src">{e(text_of(source.get("title")))}</span>{synthetic}']
        place = self.locator_text(link.get("locator"))
        if place:
            pieces.append(place)
        links = self.source_links(source)
        if links:
            pieces.append(links)
        if text_of(link.get("note")):
            pieces.append(f'<span class="note">{e(link["note"])}</span>')
        return " · ".join(pieces)

    # blocks

    def block_decision(self) -> str:
        decision = self.direction
        if not decision:
            return ""
        proposal = decision.get("proposal") if isinstance(decision.get("proposal"), dict) else None
        choice = decision.get("choice") if isinstance(decision.get("choice"), dict) else None
        out = ['<div class="card decision">']
        if choice:
            out.append(f'<p class="eyebrow">Выбран вариант</p><h3>{e(self.option_title(choice.get("option_id")))}</h3>')
            if text_of(choice.get("user_statement")):
                out.append(f'<blockquote>{e(choice["user_statement"])}</blockquote>')
            if choice.get("delegated") is True:
                out.append('<p><span class="badge badge-note">выбор передан агенту</span></p>')
            elif proposal and proposal.get("option_id") != choice.get("option_id"):
                out.append('<p><span class="badge badge-note">выбор отличается от рекомендации агента</span></p>')
        else:
            out.append('<p class="eyebrow">Выбор ещё не сделан</p>')
        if proposal and proposal.get("option_id"):
            rationale = f" — {e(text_of(proposal.get('rationale')))}" if text_of(proposal.get("rationale")) else ""
            out.append(f'<p class="muted">Агент рекомендовал: <strong>{e(self.option_title(proposal["option_id"]))}'
                       f'</strong>{rationale}</p>')
        if text_of(decision.get("change_reason")):
            out.append(f'<p class="muted">Что изменилось: {e(decision["change_reason"])}</p>')
        lines = self.lines.get(COMMITMENTS)
        section = split_sections(lines).get("Решение") if lines else None
        if section and any(line.strip() for _, line in section):
            out.append('<div class="prose">' + self.md([line for _, line in section]) + "</div>")
        out.append("</div>")
        return "".join(out)

    def number_ids(self) -> list[str]:
        lines = self.lines.get(PLAN) or []
        found = [rid for rid in refs_in(lines) if rid.startswith("CALC-") and rid in self.calcs
                 and is_active(self.calcs[rid])]
        return ordered(found)[:MAX_NUMBERS]

    def block_numbers(self) -> str:
        cards = []
        plan = self.lines.get(PLAN) or []
        for rid in self.number_ids():
            calc = self.calcs[rid]
            result = calc.get("result") if isinstance(calc.get("result"), dict) else {}
            method = calc.get("method") if isinstance(calc.get("method"), dict) else {}
            if result.get("status") == "computed" and result.get("value") is not None:
                value = f'<span class="value">{e(format_value(result["value"]))}</span>'
                unit = f' <span class="unit">{e(text_of(result.get("unit")))}</span>' if text_of(result.get("unit")) else ""
            else:
                value, unit = '<span class="value value-unknown">не вычислено</span>', ""
            period = (f'<p class="muted">Период: {e(text_of(result.get("period")))}</p>'
                      if text_of(result.get("period")) else "")
            assumption = any(f"[{rid}" in line or f", {rid}" in line for line in plan if "(допущение)" in line)
            badge = ' <span class="mark mark-assumption">допущение</span>' if assumption else ""
            cards.append(f'<article class="card number"><p class="big">{value}{unit}</p>'
                         f'<p>{e(text_of(method.get("description")))}</p>{period}'
                         f'<p class="muted">{self.footnote(rid) or ""}{badge}</p></article>')
        return '<div class="grid grid-numbers">' + "".join(cards) + "</div>" if cards else ""

    def action_rows(self) -> list[dict]:
        lines = self.lines.get(COMMITMENTS)
        section = split_sections(lines).get("Действия") if lines else None
        if not section:
            return []
        rows = [[cell.strip() for cell in line.strip().strip("|").split("|")]
                for _, line in section if line.lstrip().startswith("|")]
        if not rows:
            return []
        header = rows[0]
        if not all(name in header for name in ACTION_COLUMNS[:2]):
            return []
        found = []
        for cells in rows[1:]:
            if all(cell and set(cell) <= set(":- ") for cell in cells):
                continue
            found.append({name: cells[header.index(name)] if name in header and header.index(name) < len(cells)
                          else "" for name in ACTION_COLUMNS})
        return found

    @staticmethod
    def initials(name: str) -> str:
        letters = []
        for word in name.split()[:2]:
            letters.append(next((c for c in word if c.isalnum()), word[:1]))
        return "".join(letters).upper()

    def person_card(self, name: str, role: str | None, actions: list[dict], note: str = "") -> str:
        avatar = sum(ord(c) for c in normalize(name)) % 6
        role_html = f'<p class="role">{e(role)}</p>' if role else ""
        note_html = f'<p class="role">{e(note)}</p>' if note else ""
        if actions:
            tasks = []
            for action in actions:
                meta = []
                for label, key in (("Срок", "Срок"), ("Показатель", "Показатель"), ("Основание", "Основание")):
                    if action.get(key):
                        meta.append(f"<div><dt>{label}</dt><dd>{self.inline(action[key])}</dd></div>")
                tasks.append(f'<li><p class="task">{self.inline(action["Действие"])}</p><dl class="meta">'
                             f'{"".join(meta)}</dl></li>')
            body = f'<ul class="tasks">{"".join(tasks)}</ul>'
        else:
            body = '<p class="empty">Действий не назначено</p>'
        return (f'<article class="person"><header class="who"><span class="avatar avatar-{avatar}" '
                f'aria-hidden="true">{e(self.initials(name))}</span><div><h3>{e(name)}</h3>{role_html}{note_html}'
                f'</div></header>{body}</article>')

    def block_team(self) -> str:
        actions = self.action_rows()
        roster = self.team.members if self.team and self.team.members else None
        cards = []
        intro = ""
        if roster:
            names = {normalize(name) for name, _ in roster}
            for name, role in roster:
                mine = [a for a in actions if name in owners_of(a["Ответственный"])]
                cards.append(self.person_card(name, role, mine))
            extra = ordered(owner for a in actions for owner in owners_of(a["Ответственный"]) if owner not in names)
            for owner in extra:
                mine = [a for a in actions if owner in owners_of(a["Ответственный"])]
                cards.append(self.person_card(owner, None, mine, "нет в составе"))
        else:
            if not self.team:
                intro = ('<p class="muted">Состав команды не записан: ответственные показаны так, '
                         'как они указаны в обязательствах.</p>')
            else:
                intro = '<p class="muted">Состав не назван: ответственные записаны ролями.</p>'
            owners = ordered(normalize(a["Ответственный"]) for a in actions if normalize(a["Ответственный"]))
            for owner in owners:
                mine = [a for a in actions if normalize(a["Ответственный"]) == owner]
                cards.append(self.person_card(owner, None, mine))
        return intro + '<div class="grid grid-people">' + "".join(cards) + "</div>" if cards else ""

    @staticmethod
    def cost_texts(option: dict) -> tuple[str, str, str, str]:
        """Labels and explanations of `cost` and `limit_check`; empty strings for a record of version 1."""
        cost, limit = option.get("cost"), option.get("limit_check")
        cost = cost if isinstance(cost, dict) else {}
        limit = limit if isinstance(limit, dict) else {}
        cost_label = COST_LABELS.get(cost.get("status"), "")
        limit_label = LIMIT_LABELS.get(limit.get("result"), "")
        cost_note = text_of(cost.get("note") or cost.get("reason"))
        if cost.get("question_id"):
            cost_note = f"{cost_note} ({text_of(cost['question_id'])})".strip()
        limit_note = text_of(limit.get("note"))
        compared = COMPARED_LABELS.get(limit.get("compared"))
        if compared:
            limit_note = f"сравнивались {compared}. {limit_note}".strip()
        return cost_label, cost_note, limit_label, limit_note

    def option_card(self, option: dict) -> str:
        rid = option["id"]
        chosen = self.direction and isinstance(self.direction.get("choice"), dict) \
            and self.direction["choice"].get("option_id") == rid
        proposed = self.direction and isinstance(self.direction.get("proposal"), dict) \
            and self.direction["proposal"].get("option_id") == rid
        badges = [f'<span class="badge badge-kind">{e(STRATEGY_KINDS.get(option.get("strategy_kind"), ""))}</span>']
        if chosen:
            badges.append('<span class="badge badge-chosen">выбрано</span>')
        if proposed:
            badges.append('<span class="badge badge-note">рекомендовал агент</span>')
        objections = [o for o in option.get("objections", []) if isinstance(o, str)]
        objection = ""
        if objections:
            more = f' <span class="muted">и ещё {len(objections) - 1}</span>' if len(objections) > 1 else ""
            objection = f'<p class="objection"><span class="label">Главное возражение</span>{e(objections[0])}{more}</p>'
        cost_label, cost_note, limit_label, limit_note = self.cost_texts(option)
        cost_line = ""
        if cost_label or limit_label:
            parts = [f"Стоимость: {e(cost_label)}" if cost_label else "",
                     f"Ограничение брифа: {e(limit_label)}" if limit_label else ""]
            cost_line = f'<p class="cost-line">{" · ".join(part for part in parts if part)}</p>'
        fields = (("Аудитория и задача", "audience_task"), ("Действие", "action"),
                  ("От чего отказываемся", "rejected_actions"), ("Чем полезно", "benefit_mechanism"),
                  ("Экономика", "economics"), ("Условия", "preconditions"), ("Возражения", "objections"),
                  ("Риски", "risks"), ("Обратимость", "reversibility"), ("Как проверить", "verification"))
        rows = []
        for label, key in fields:
            value = option.get(key)
            if isinstance(value, list):
                value = "".join(f"<li>{e(v)}</li>" for v in value if isinstance(v, str))
                value = f"<ul>{value}</ul>" if value else ""
            else:
                value = e(text_of(value))
            if value:
                rows.append(f"<div><dt>{label}</dt><dd>{value}</dd></div>")
        for label, status, note in (("Стоимость", cost_label, cost_note), ("Сверка с ограничением", limit_label, limit_note)):
            if status:
                rows.append(f"<div><dt>{label}</dt><dd>{e(status)}{': ' + e(note) if note else ''}</dd></div>")
        basis = [self.footnote(i) or e(i) for i in option.get("basis", []) if isinstance(i, str)]
        if basis:
            rows.append(f'<div><dt>Основания</dt><dd>{" ".join(basis)}</dd></div>')
        return (f'<article class="card option{" option-chosen" if chosen else ""}" id="option-{rid}">'
                f'<p class="badges">{"".join(badges)}</p><h3>{e(text_of(option.get("title")))}</h3>'
                f'<p>{e(text_of(option.get("action")))}</p>{cost_line}{objection}'
                f'<details><summary>Все поля</summary><dl class="fields">{"".join(rows)}</dl></details></article>')

    def block_options(self) -> str:
        if not self.active_options:
            return ""
        return '<div class="grid grid-options">' + "".join(self.option_card(o) for o in self.active_options) + "</div>"

    def block_texts(self) -> str:
        docs = []
        for rel in (DELIVERABLE, *TEXTS):
            lines = self.lines.get(rel)
            if lines is None:
                continue
            title = next((l[2:].strip() for l in lines if l.startswith("# ")), Path(rel).stem)
            body = [l for l in lines if not l.startswith("# ")]
            status = next((l.strip() for l in body if l.strip()), "")
            badge = ""
            if STATUS_PRELIMINARY_RE.fullmatch(status):
                badge = ' <span class="badge badge-note">предварительно</span>'
            elif not STATUS_CHOSEN_RE.fullmatch(status):
                badge = ""
            docs.append(f'<details class="doc"><summary>{e(title)}{badge}</summary>'
                        f'<div class="prose">{self.md(body)}</div></details>')
        return "".join(docs)

    def basis_item(self, number: int, rid: str) -> str:
        prefix = rid.split("-")[0]
        head = f'<span class="num">{number}</span>'
        extra = ""
        if prefix == "CLM":
            claim = self.claims[rid]
            label, css = KIND_LABELS.get(claim.get("kind"), ("Утверждение", "inference"))
            badges = f'<span class="badge badge-{css}">{label}</span>'
            if claim.get("status") == "unconfirmed":
                badges += '<span class="badge badge-warn">не подтверждено</span>'
            elif claim.get("status") == "contested":
                badges += '<span class="badge badge-warn">спорно</span>'
            if self.is_synthetic(rid):
                badges += '<span class="mark mark-synthetic">синтетика</span>'
            body = f'<p class="claim">{e(text_of(claim.get("text")))}</p>'
            lines = [self.evidence_line(s) for s in items_of(claim.get("supports"))]
            lines = [f"<li>{l}</li>" for l in lines if l]
            if lines:
                body += f'<ul class="evidence">{"".join(lines)}</ul>'
            derived = [self.footnote(i) or e(i) for i in claim.get("derived_from", []) if isinstance(i, str)]
            if derived:
                extra += f'<p class="muted">Основано на: {" ".join(derived)}</p>'
            for link in items_of(claim.get("contradicts")):
                line = self.evidence_line(link)
                if line:
                    resolved = (f" Снято: {e(link['resolution'])}"
                                if isinstance(link, dict) and text_of(link.get("resolution")) else "")
                    extra += f'<p class="muted">Противоречит: {line}.{resolved}</p>'
            if text_of(claim.get("limits")):
                extra += f'<p class="muted">Ограничение: {e(claim["limits"])}</p>'
        elif prefix == "CALC":
            calc = self.calcs[rid]
            result = calc.get("result") if isinstance(calc.get("result"), dict) else {}
            method = calc.get("method") if isinstance(calc.get("method"), dict) else {}
            badges = '<span class="badge badge-calc">Расчёт</span>'
            if self.is_synthetic(rid):
                badges += '<span class="mark mark-synthetic">синтетика</span>'
            if result.get("status") == "computed" and result.get("value") is not None:
                outcome = " ".join(p for p in (e(format_value(result["value"])), e(text_of(result.get("unit")))) if p)
                if text_of(result.get("period")):
                    outcome += f", {e(result['period'])}"
            else:
                outcome = "не вычислено"
            body = f'<p class="claim">{e(text_of(method.get("description")))}: <strong>{outcome}</strong></p>'
            lines = []
            for item in items_of(calc.get("inputs")):
                if not isinstance(item, dict):
                    continue
                value = " ".join(p for p in (e(format_value(item.get("value"))), e(text_of(item.get("unit")))) if p)
                where = self.evidence_line(item)
                lines.append(f"<li>{e(text_of(item.get('name')))}: {value}" + (f" · {where}" if where else "") + "</li>")
            if lines:
                body += f'<ul class="evidence">{"".join(lines)}</ul>'
            if text_of(method.get("formula")):
                extra += f'<p class="muted">Формула: {e(method["formula"])}</p>'
            for key, label in (("assumptions", "Допущения"), ("gaps", "Пробелы")):
                values = [v for v in calc.get(key, []) if isinstance(v, str)]
                if values:
                    extra += f'<p class="muted">{label}: {e("; ".join(values))}</p>'
        else:
            question = self.questions[rid]
            badges = '<span class="badge badge-unknown">Не знаем</span>'
            body = f'<p class="claim">{e(text_of(question.get("question")))}</p>'
            if text_of(question.get("gap")):
                body += f'<p>{e(question["gap"])}</p>'
            if text_of(question.get("affects")):
                extra += f'<p class="muted">Влияет на: {e(question["affects"])}</p>'
            found = [self.footnote(i) or e(i) for i in question.get("findings", []) if isinstance(i, str)]
            if found:
                extra += f'<p class="muted">Что известно: {" ".join(found)}</p>'
        return (f'<article class="basis-item" id="basis-{rid}"><header>{head}{badges}'
                f'<span class="rid">{rid}</span></header>{body}{extra}</article>')

    def block_basis(self) -> str:
        return "".join(self.basis_item(number, rid) for rid, number in self.basis_no.items())

    def block_revisions(self) -> str:
        lines = self.lines.get(REVISIONS)
        if not lines:
            return ""
        cards = []
        for heading, body in split_sections(lines).items():
            match = REVISION_HEADING_RE.fullmatch(heading)
            if not match:
                continue
            folder = self.root / "outputs" / ".history" / match.group(1)
            links = []
            if folder.is_dir():
                for path in sorted(folder.iterdir()):
                    if path.is_file():
                        links.append(f'<a href="{quote(".history/" + match.group(1) + "/" + path.name)}">'
                                     f'{e(path.name)}</a>')
            history = (f'<p class="muted">Прежние версии: {" · ".join(links)}</p>') if links else ""
            cards.append(f'<article class="card revision"><h3>{e(heading)}</h3>'
                         f'{self.md([line for _, line in body])}{history}</article>')
        return "".join(cards)

    def block_reviews(self) -> str:
        folder = self.root / "state" / "reviews"
        try:
            names = sorted(n for n in os.listdir(folder) if re.fullmatch(r"review-[0-9]+\.md", n))
        except OSError:
            return ""
        rows = []
        for name in names:
            lines = read_lines(self.root, f"state/reviews/{name}") or []
            title = next((REVIEW_TITLE_RE.fullmatch(l.strip()) for l in lines if l.startswith("# ")), None)
            total = next((REVIEW_TOTAL_RE.search(l) for l in lines if REVIEW_TOTAL_RE.search(l)), None)
            after = next((REVIEW_AFTER_RE.search(l) for l in lines if REVIEW_AFTER_RE.search(l)), None)
            label = e(title.group(1)) if title else e(Path(name).stem)
            date = e(title.group(2)) if title and title.group(2) else ""
            counts = ('<span class="count">замечаний <b>{}</b></span><span class="count">принято <b>{}</b></span>'
                      '<span class="count">отклонено <b>{}</b></span>').format(*total.groups()) if total else \
                '<span class="muted">итог не указан</span>'
            later = f'<span class="muted">после {after.group(1)}</span>' if after else ""
            rows.append(f'<li><span class="rev-title">{label}</span><span class="muted">{date}</span>'
                        f'{counts}{later}</li>')
        return f'<ul class="reviews">{"".join(rows)}</ul>' if rows else ""

    def block_accounting(self) -> str:
        lines = self.lines.get(ACCOUNTING)
        section = split_sections(lines).get("Итог") if lines else None
        if not section or not any(line.strip() for _, line in section):
            return ""
        readable = [TIMESTAMP_RE.sub(lambda match: human_time(match.group(0)), line) for _, line in section]
        return '<div class="card prose">' + self.md(readable) + "</div>"

    # frame

    def header(self) -> str:
        title = text_of(self.case.get("title")) or text_of(self.case.get("case_id")) or self.root.name
        stage = STAGES.get(self.checkpoint.get("stage")) if self.checkpoint else None
        chips = [f'<span class="chip">Этап: {e(stage) if stage else "не записан"}</span>']
        lines = self.lines.get(REVISIONS) or []
        count = sum(1 for heading in split_sections(lines) if REVISION_HEADING_RE.fullmatch(heading))
        chips.append(f'<span class="chip">Пересмотров: {count}</span>')
        if self.lines.get(COMMITMENTS) is not None:
            agreed = any(d.get("topic") == "commitments" and d.get("status") == "chosen"
                         for d in self.decisions.values())
            chips.append('<span class="chip chip-ok">Обязательства согласованы</span>' if agreed
                         else '<span class="chip">Обязательства не согласованы</span>')
        if self.checkpoint:
            saved = f'Сохранение {e(self.checkpoint["id"])} от {e(human_time(self.checkpoint["created_at"]))}'
        else:
            saved = "сохранений нет"
        return (f'<header class="top"><p class="eyebrow">Страница кейса</p><h1>{e(title)}</h1>'
                f'<p class="chips">{"".join(chips)}</p><p class="muted">{saved}</p></header>')

    def render(self) -> str:
        builders = {"decision": self.block_decision, "numbers": self.block_numbers, "team": self.block_team,
                    "options": self.block_options, "texts": self.block_texts, "basis": self.block_basis,
                    "revisions": self.block_revisions, "reviews": self.block_reviews,
                    "accounting": self.block_accounting}
        sections = []
        for name, title in BLOCKS:
            try:
                body = builders[name]()
            except Exception as error:  # noqa: BLE001 - a malformed record must not stop the page
                self.warnings.append(warning("BLOCK_FAILED", name, type(error).__name__))
                body = ""
            body = body or f'<p class="empty">{EMPTY}</p>'
            sections.append(f'<section class="block" id="{name}"><h2>{title}</h2>{body}</section>')
        nav = "".join(f'<a href="#{name}">{title}</a>' for name, title in BLOCKS)
        title = text_of(self.case.get("title")) or self.root.name
        return (f'<!doctype html>\n<html lang="ru">\n<head>\n<meta charset="utf-8">\n'
                f'<meta name="viewport" content="width=device-width, initial-scale=1">\n'
                f'<meta name="color-scheme" content="light dark">\n'
                f'<title>{e(title)} — страница кейса</title>\n<style>{STYLE}</style>\n</head>\n<body>\n'
                f'<main class="page">\n{self.header()}\n<nav class="toc" aria-label="Разделы">{nav}</nav>\n'
                f'{chr(10).join(sections)}\n'
                f'<footer>Страница собрана из папки кейса утилитой <code>tools/case_view.py</code>. '
                f'Числа и формулировки взяты из кейса как есть.</footer>\n</main>\n</body>\n</html>\n')


STYLE = """
:root{--bg:#f6f6f3;--surface:#fff;--text:#1d2125;--muted:#5d6670;--border:#e0e2e5;--accent:#2d5bd0;
--accent-soft:#e8eefc;--ok:#1d6b3f;--ok-soft:#e3f3ea;--warn:#8a5a00;--warn-soft:#fbf0d9;--violet:#5a46a8;
--violet-soft:#eeeafa;--grey-soft:#eceef0;--bad:#8f2f2f;--bad-soft:#f8e6e6;
--av0:#dbe6fb;--av1:#dcefe3;--av2:#f6e7cf;--av3:#eadcf3;--av4:#f5dcdc;--av5:#dcecec;
--radius:10px;--font:system-ui,-apple-system,"Segoe UI",Roboto,"Helvetica Neue",Arial,sans-serif}
@media (prefers-color-scheme:dark){:root{--bg:#121416;--surface:#1b1e22;--text:#e6e8ea;--muted:#9aa3ad;
--border:#2c3137;--accent:#8fb0ff;--accent-soft:#232f4d;--ok:#7fd4a2;--ok-soft:#1a3326;--warn:#e8bf6a;
--warn-soft:#3a2e14;--violet:#b6a8f0;--violet-soft:#2b2646;--grey-soft:#262a2f;--bad:#f0a0a0;--bad-soft:#3a2020;
--av0:#26345a;--av1:#1f3a2c;--av2:#443618;--av3:#3a2a4a;--av4:#4a2727;--av5:#1f3b3b}}
*{box-sizing:border-box}
html{-webkit-text-size-adjust:100%}
body{margin:0;background:var(--bg);color:var(--text);font:16px/1.55 var(--font)}
.page{max-width:980px;margin:0 auto;padding:32px 16px 64px}
h1{font-size:2rem;line-height:1.2;margin:.2rem 0 .8rem;letter-spacing:-.01em}
h2{font-size:1.25rem;margin:0 0 1rem;letter-spacing:-.005em}
h3{font-size:1.05rem;margin:0 0 .35rem}
h4,h5,h6{font-size:1rem;margin:1.2rem 0 .4rem}
p{margin:.4rem 0}
a{color:var(--accent);text-underline-offset:2px}
code{font-family:ui-monospace,Consolas,monospace;font-size:.9em}
.eyebrow{font-size:.78rem;text-transform:uppercase;letter-spacing:.08em;color:var(--muted);margin:0}
.muted{color:var(--muted);font-size:.92rem}
.empty{color:var(--muted);font-style:italic;margin:.2rem 0}
.top{padding:8px 0 20px}
.chips{display:flex;flex-wrap:wrap;gap:8px;margin:.4rem 0}
.chip{border:1px solid var(--border);background:var(--surface);border-radius:999px;padding:3px 12px;font-size:.88rem}
.chip-ok{background:var(--ok-soft);color:var(--ok);border-color:transparent}
.toc{display:flex;flex-wrap:wrap;gap:6px 16px;padding:12px 0 20px;border-top:1px solid var(--border);
font-size:.92rem}
.toc a{text-decoration:none}
.block{margin:0 0 40px;scroll-margin-top:16px}
.card,.person,.basis-item,.doc{background:var(--surface);border:1px solid var(--border);border-radius:var(--radius);
padding:16px 18px}
.grid{display:grid;gap:14px}
.grid-numbers{grid-template-columns:repeat(auto-fit,minmax(200px,1fr))}
.grid-people{grid-template-columns:repeat(auto-fit,minmax(300px,1fr))}
.grid-options{grid-template-columns:repeat(auto-fit,minmax(260px,1fr))}
.decision h3{font-size:1.4rem}
blockquote{margin:.6rem 0;padding:.2rem 0 .2rem 14px;border-left:3px solid var(--accent);color:var(--text)}
.big{margin:0 0 .4rem}
.value{font-size:2.2rem;font-weight:650;line-height:1.1;letter-spacing:-.02em}
.value-unknown{font-size:1.2rem;color:var(--muted)}
.unit{color:var(--muted)}
.badges{display:flex;flex-wrap:wrap;gap:6px;margin:0 0 .5rem}
.badge,.mark{display:inline-block;border-radius:6px;padding:1px 8px;font-size:.78rem;font-weight:600;
white-space:nowrap;line-height:1.5;margin-right:6px}
.badge-fact{background:var(--ok-soft);color:var(--ok)}
.badge-inference,.badge-calc,.badge-kind{background:var(--accent-soft);color:var(--accent)}
.badge-hypothesis,.badge-warn{background:var(--warn-soft);color:var(--warn)}
.badge-forecast{background:var(--violet-soft);color:var(--violet)}
.badge-unknown{background:var(--bad-soft);color:var(--bad)}
.badge-note{background:var(--grey-soft);color:var(--text)}
.badge-chosen{background:var(--accent);color:var(--surface)}
.mark{background:transparent;border:1px solid var(--border);color:var(--muted);font-weight:500;padding:0 6px}
.mark-synthetic{border-color:var(--violet);color:var(--violet)}
.mark-assumption{border-color:var(--warn);color:var(--warn)}
.fn{display:inline-block;min-width:1.35em;padding:0 5px;border-radius:5px;background:var(--accent-soft);
color:var(--accent);font-size:.75rem;font-weight:600;text-align:center;text-decoration:none;
vertical-align:super;line-height:1.5;margin:0 1px}
.fn-wide{vertical-align:baseline;font-size:.8rem}
.refs{white-space:normal}
.ref,.rid{font-family:ui-monospace,Consolas,monospace;font-size:.75rem;color:var(--muted)}
.person .who{display:flex;gap:12px;align-items:center;margin-bottom:10px}
.who h3{margin:0}
.role{margin:0;color:var(--muted);font-size:.9rem}
.avatar{flex:none;width:44px;height:44px;border-radius:50%;display:flex;align-items:center;justify-content:center;
font-weight:650;font-size:.95rem;color:var(--text)}
.avatar-0{background:var(--av0)}.avatar-1{background:var(--av1)}.avatar-2{background:var(--av2)}
.avatar-3{background:var(--av3)}.avatar-4{background:var(--av4)}.avatar-5{background:var(--av5)}
.tasks{list-style:none;margin:0;padding:0}
.tasks li{border-top:1px solid var(--border);padding:10px 0}
.tasks li:first-child{border-top:0;padding-top:0}
.task{font-weight:550;margin:0 0 4px}
.meta{display:flex;flex-wrap:wrap;gap:4px 18px;margin:0;font-size:.88rem}
.meta div{display:flex;gap:6px}
.meta dt{color:var(--muted)}
.meta dd{margin:0}
.option-chosen{border-color:var(--accent);box-shadow:0 0 0 1px var(--accent)}
.objection{font-size:.92rem}
.label{display:block;font-size:.75rem;text-transform:uppercase;letter-spacing:.06em;color:var(--muted)}
details{margin:.5rem 0}
summary{cursor:pointer;color:var(--accent);font-size:.92rem}
.doc{margin:0 0 10px;padding:12px 18px}
.doc>summary{font-size:1.02rem;font-weight:600;color:var(--text)}
.cost-line{margin:.3rem 0;color:var(--muted);font-size:.9rem}
.fields{margin:.6rem 0 0}
.fields div{padding:6px 0;border-top:1px solid var(--border)}
.fields dt{font-size:.78rem;text-transform:uppercase;letter-spacing:.05em;color:var(--muted)}
.fields dd{margin:0}
.fields ul{margin:0;padding-left:1.2em}
.basis-item{margin:0 0 10px;scroll-margin-top:16px}
.basis-item header{display:flex;flex-wrap:wrap;align-items:center;gap:6px;margin-bottom:6px}
.num{display:inline-flex;width:1.7em;height:1.7em;align-items:center;justify-content:center;border-radius:50%;
background:var(--accent-soft);color:var(--accent);font-size:.8rem;font-weight:700}
.rid{margin-left:auto}
.claim{margin:.2rem 0}
.evidence{margin:.4rem 0;padding-left:1.1em;font-size:.9rem;color:var(--muted)}
.evidence .src{color:var(--text)}
.note{font-style:italic}
.prose p,.prose li{max-width:72ch}
.prose ul,.prose ol{padding-left:1.3em;margin:.4rem 0}
.table-wrap{overflow-x:auto;margin:.6rem 0}
table{border-collapse:collapse;width:100%;font-size:.92rem}
th,td{text-align:left;vertical-align:top;padding:7px 10px;border-bottom:1px solid var(--border)}
th{font-size:.78rem;text-transform:uppercase;letter-spacing:.05em;color:var(--muted)}
.revision{margin:0 0 10px}
.reviews{list-style:none;margin:0;padding:0}
.reviews li{display:flex;flex-wrap:wrap;gap:6px 18px;align-items:baseline;background:var(--surface);
border:1px solid var(--border);border-radius:var(--radius);padding:10px 16px;margin:0 0 8px}
.rev-title{font-weight:600}
footer{color:var(--muted);font-size:.85rem;border-top:1px solid var(--border);padding-top:16px}
@media (max-width:640px){
.page{padding:20px 16px 48px}
h1{font-size:1.5rem}
.grid-people,.grid-options{grid-template-columns:1fr}
.table-wrap{overflow:visible}
table,thead,tbody,tr,td{display:block}
thead{display:none}
tr{border:1px solid var(--border);border-radius:8px;margin:0 0 8px;padding:4px 0}
td{border:0;padding:3px 10px}
td::before{content:attr(data-label);display:block;font-size:.72rem;text-transform:uppercase;
letter-spacing:.05em;color:var(--muted)}
.rid{margin-left:0}}
@media print{
:root{--bg:#fff;--surface:#fff;--text:#000;--muted:#444;--border:#bbb}
body{font-size:11pt}
.page{max-width:none;padding:0}
.toc{display:none}
.card,.person,.basis-item,.doc,.reviews li{break-inside:avoid;box-shadow:none}
.block{margin-bottom:22px}
a{color:inherit}
details::details-content{content-visibility:visible;display:block}
details>summary{list-style:none}}
"""


# --- command line --------------------------------------------------------------

def build_page(root: Path) -> tuple[str, list[dict]]:
    view = View(Path(root))
    page = view.render()
    return page, view.warnings


def write_page(root: Path, page: str) -> None:
    target = root / PAGE
    target.parent.mkdir(parents=True, exist_ok=True)
    temp = target.with_name(target.name + ".tmp")
    try:
        temp.write_bytes(page.encode("utf-8"))
        os.replace(temp, target)
    finally:
        if temp.exists():
            temp.unlink()


def cmd_build(args) -> int:
    root = args.case.resolve()
    if not root.is_dir():
        raise Refused("case_missing", "папка кейса не найдена")
    page, warnings = build_page(root)
    write_page(root, page)
    print(json.dumps({"status": "ok", "path": PAGE, "warnings": warnings}, ensure_ascii=False))
    return EXIT_OK


def build_parser() -> Parser:
    parser = Parser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    commands = parser.add_subparsers(dest="command", required=True, parser_class=Parser)
    build = commands.add_parser("build")
    build.add_argument("case", type=Path)
    build.set_defaults(run=cmd_build)
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
