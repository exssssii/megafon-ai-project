"""Team roster of a case: parse team.md.

The file names the people of the team as the manager said them. The gate checks the owners of the
commitments against it; the case page shows it.
"""

import re
from dataclasses import dataclass, field
from pathlib import Path

TEAM = "team.md"
NOT_NAMED = "Состав не назван: ответственные записаны ролями."
COLUMNS = ["Имя", "Роль"]
SOURCE_RE = re.compile(r"Источник: слова пользователя, действие (ACT-[0-9]{4,})")


@dataclass
class Team:
    action_id: str | None
    members: list[tuple[str, str]] | None
    problems: list[str] = field(default_factory=list)


def normalize(name: str) -> str:
    """Strip the ends and collapse inner whitespace."""
    return " ".join(name.split())


def owners_of(cell: str) -> list[str]:
    """Names of an owner cell: split by comma, normalized, empty ones dropped."""
    return [name for name in (normalize(part) for part in cell.split(",")) if name]


def read_team(root: Path) -> Team | None:
    """The parsed roster, or None when the case has no team.md."""
    try:
        text = (Path(root) / TEAM).read_bytes().decode("utf-8-sig")
    except OSError:
        return None
    except UnicodeDecodeError:
        return Team(None, None, ["the file is not UTF-8"])
    return parse(text.splitlines())


def parse(lines: list[str]) -> Team:
    problems = []
    action_id = None
    not_named = False
    rows = []
    for line in lines:
        stripped = line.strip()
        match = SOURCE_RE.fullmatch(stripped)
        if match and action_id is None:
            action_id = match.group(1)
        elif stripped == NOT_NAMED:
            not_named = True
        elif stripped.startswith("|"):
            rows.append([normalize(cell) for cell in stripped.strip("|").split("|")])
    if action_id is None:
        problems.append("no line «Источник: слова пользователя, действие ACT-NNNN»")
    if not_named and rows:
        problems.append("the file has both a table and the line «Состав не назван»")
        return Team(action_id, None, problems)
    if not_named:
        return Team(action_id, None, problems)
    if not rows:
        problems.append("neither a table with the columns Имя, Роль nor the line «Состав не назван»")
        return Team(action_id, [], problems)
    if rows[0] != COLUMNS:
        problems.append("columns must be: " + ", ".join(COLUMNS))
        return Team(action_id, [], problems)
    members = []
    seen = set()
    for cells in rows[1:]:
        if all(set(cell) <= set(":- ") for cell in cells):
            continue
        if len(cells) != len(COLUMNS) or not all(cells):
            problems.append("a row needs a name and a role")
        elif "," in cells[0]:
            # A comma separates several owners in the commitments table.
            problems.append("a name must not contain a comma")
        elif cells[0] in seen:
            problems.append(f"the name is repeated: {cells[0]}")
        else:
            seen.add(cells[0])
            members.append((cells[0], cells[1]))
    if not members and not problems:
        problems.append("the table has no rows")
    return Team(action_id, members, problems)
