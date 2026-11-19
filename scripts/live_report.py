"""Report of an isolated live run. Started by live-entry.ps1 from the clone under test.

Prints what the logs and the case folders show and writes it to results/summary.txt.
Never prints environment values: the canary is only counted.
"""
import argparse
import hashlib
import json
import os
import pathlib
import posixpath
import re
import subprocess
import sys
import zipfile
from typing import NamedTuple

SEPARATOR = re.compile(r"&&|\|\||;|\r?\n")
TOKEN = re.compile(r"""[^\s"'`;|&<>()=,]+""")
CD = re.compile(r"""^\s*(?:.*?\s-(?:Command|c)\s+)?["'(]*\s*(?:cd|Set-Location|pushd)(?:\s+-(?:Literal)?Path)?\s+(.+?)\s*$""",
                re.IGNORECASE)
LOST_FOLDER = re.compile(r"^\s*(?:\(\s*cd\b|\)|popd\b|Pop-Location\b|cd\s*$|cd\s+-\s*$)", re.IGNORECASE)
PLAIN_TARGET = re.compile(r"""^(?:"[^"]*"|'[^']*'|\S+)[)'"]*$""")
PATH_PREFIX = re.compile(r"^[^\/]*[:@{\[=](?=\.\.)")
DOTDOT = re.compile(r"(?:^|[\\/:@{\[=])\.\.(?:$|[\\/])")
# The range operator of PowerShell next to a bracket, as in $t[($a)..($b)]: not a path.
RANGE = re.compile(r"(?<=[)\]])\.\.|\.\.(?=\()")
# A redirect of the output does not change where cd goes.
REDIRECT = re.compile(r"(?:\s+(?:\d?>>?|&>)\s*\S+)+\s*$")
# The body of a heredoc or of a PowerShell here-string is text the command writes or runs,
# not a path of the shell: a link ../../inbox/x in a markdown file written inside the clone.
HEREDOC = re.compile(r"""<<-?\s*(['"]?)(\w+)\1[^\n]*\n(.*?)(?:\n[ \t]*\2[ \t]*(?=\r?\n|$)|\Z)""", re.DOTALL)
HERESTRING = re.compile(r"""@(['"])[ \t]*\r?\n(.*?)(?:\r?\n\1@|\Z)""", re.DOTALL)
HIDDEN = (r"docs[\\/]+acceptance|make_acceptance_cases|(?:retention-smb|direction-b2c|family-connect)\.json"
          r"|examples[\\/]+(?:retention-smb|direction-b2c|family-connect)|megafon-AI-agent-hack|[\\/]results[\\/]|[\\/]held[\\/]|live-held-"
          r"|handoff-(?:final-)?(?:claude|codex)|handoff-(?:pack|in)")
PATH_KEYS = ("file_path", "path", "notebook_path", "pattern", "glob")


class Call(NamedTuple):
    """One tool call of the agent: a shell command or a single path."""
    text: str
    cwd: str | None
    carries_cd: bool
    is_command: bool


def tool_calls(event) -> list[Call]:
    if not isinstance(event, dict):
        return []
    calls = []
    if event.get("type") == "assistant":
        for part in (event.get("message") or {}).get("content") or []:
            if not isinstance(part, dict) or part.get("type") != "tool_use":
                continue
            given = part.get("input") or {}
            if isinstance(given.get("command"), str):
                calls.append(Call(given["command"], None, True, True))
            calls += [Call(given[key], None, False, False) for key in PATH_KEYS if isinstance(given.get(key), str)]
    item = event.get("item")
    if isinstance(item, dict) and event.get("type") == "item.completed":
        if isinstance(item.get("command"), str):
            calls.append(Call(item["command"], item.get("cwd"), False, True))
        for change in item.get("changes") or []:
            if isinstance(change, dict) and isinstance(change.get("path"), str):
                calls.append(Call(change["path"], None, False, False))
    return calls


def normal(path: str) -> str:
    path = path.strip("\"'").replace("\\", "/").lower()
    git_bash = re.match(r"^/([a-z])(/.*|$)", path)
    return f"{git_bash[1]}:{git_bash[2]}" if git_bash else path


def resolve(cwd: str | None, path: str) -> str | None:
    """The full path, or None when the log does not say where it leads."""
    path = normal(path)
    if re.search(r"[$%~]", path):
        return None
    if re.match(r"^[a-z]:/|^/", path):
        return posixpath.normpath(path)
    return None if cwd is None else posixpath.normpath(posixpath.join(cwd, path))


def has_dotdot(token: str) -> bool:
    return "://" not in token and DOTDOT.search(token) is not None


def split_text(command: str) -> tuple[str, list[str]]:
    """The command without the bodies of its heredocs and here-strings, and those bodies."""
    bodies: list[str] = []

    def cut(match: re.Match, group: int) -> str:
        bodies.append(match.group(group))
        return match.group(0)[:match.start(group) - match.start(0)]
    command = HEREDOC.sub(lambda m: cut(m, 3), command)
    return HERESTRING.sub(lambda m: cut(m, 2), command), bodies


def dotdot_traces(calls: list[Call], clone: str) -> dict[str, int]:
    """Paths with .. that leave the clone or cannot be followed, in the calls of one reply.

    A .. in the text a command writes or runs is counted apart as in_text: it is no path of
    the shell, so it does not stop the run from counting, but it is shown for a manual look.
    """
    root = normal(clone).rstrip("/")
    cwd: str | None = root
    carried = False
    found: dict[str, int] = {}
    for call in calls:
        here = normal(call.cwd) if call.cwd else cwd
        # The log shows a cd, not the folder the next call really starts in: in a live run
        # one cd was not kept, so a path that leaves the clone from there is only a guess.
        sure = bool(call.cwd) or not carried
        text = call.text
        if call.is_command:
            text, bodies = split_text(RANGE.sub(" ", text))
            count = sum(has_dotdot(token) for body in bodies for token in TOKEN.findall(body))
            if count:
                found["in_text"] = found.get("in_text", 0) + count
        for segment in SEPARATOR.split(text) if call.is_command else [text]:
            cd = CD.match(segment) if call.is_command else None
            goal = REDIRECT.sub("", cd[1]) if cd else ""
            tokens = TOKEN.findall(goal) if cd else TOKEN.findall(segment) if call.is_command else [segment]
            for token in filter(has_dotdot, tokens):
                token = PATH_PREFIX.sub("", token)
                target = resolve(here, token)
                absolute = re.match(r"^[a-z]:/|^/", normal(token)) is not None
                if target is None:
                    found["unresolved"] = found.get("unresolved", 0) + 1
                elif target != root and not target.startswith(root + "/"):
                    kind = "outside" if sure or absolute else "unresolved"
                    found[kind] = found.get(kind, 0) + 1
            if cd:
                target = goal.rstrip(")'\" ")
                here = resolve(here, target) if PLAIN_TARGET.match(goal) else None
                sure = sure or re.match(r"^[a-z]:/|^/", normal(target)) is not None
                carried = carried or call.carries_cd
            if call.is_command and LOST_FOLDER.match(segment):
                here = None
        if call.carries_cd:
            cwd = here
    return found


def canary_seen(results: pathlib.Path, projects: pathlib.Path, value: str) -> int:
    if not value:
        return 0
    needle = value.encode("utf-8")
    files = list(results.glob("*.jsonl")) + [p for p in projects.rglob("*") if p.is_file()]
    return sum(path.read_bytes().count(needle) for path in files)


def stage_of(path: pathlib.Path) -> str:
    try:
        return json.loads(path.read_text(encoding="utf-8"))["stage"]
    except (OSError, ValueError, KeyError, TypeError):
        return "unreadable"


def records_of(path: pathlib.Path) -> list | None:
    """The records of a registry the agent may have damaged, None when they cannot be read."""
    try:
        return json.loads(path.read_text(encoding="utf-8"))["records"]
    except (OSError, ValueError, KeyError, TypeError):
        return None


def count_sources(case: pathlib.Path) -> int | str | None:
    path = case / "evidence/sources.json"
    if not path.exists():
        return None
    records = records_of(path)
    return "unreadable" if records is None else len(records)


def damaged_kept(case: pathlib.Path, sha: str) -> bool:
    folder = case / "state/checkpoints"
    return any(hashlib.sha256(path.read_bytes()).hexdigest().upper() == sha.upper()
               for path in folder.rglob("*") if path.is_file())


# Words of the agent's own make-up that the user should not see: names of its tools, files and
# procedures. A hit is shown for a person to judge, it does not fail the run.
INTERNAL = re.compile(r"\b[\w-]+\.py\b|\btools/|\bagent/|\.venv|процедур\w*|инструкци\w*|утилит\w*|"
                      r"репозитори\w*|checkpoint|веб-инструмент\w*|через модель|"
                      r"\b(?:CKP|ACT|DEC|OPT|CLM|CALC|SRC)-\d+",
                      re.IGNORECASE)


def internal_words(text: str) -> list[str]:
    return sorted({match.group(0).lower() for match in INTERNAL.finditer(text)})


# A line that states a threshold of the validation plan: success or failure with a number.
THRESHOLD = re.compile(r"(?<!\w)(?:не)?успех\w*(?!\w).*\d", re.IGNORECASE)


def threshold_lines(text: str) -> int:
    """Lines of a reply that name a success or failure threshold with a number."""
    return sum(1 for line in text.splitlines() if THRESHOLD.search(line))


def shown_text(event) -> str:
    """Text the user sees from one event: a Codex agent message or the top-level Claude Code text."""
    if not isinstance(event, dict):
        return ""
    item = event.get("item")
    if isinstance(item, dict) and item.get("type") == "agent_message" and event.get("type") == "item.completed":
        return item.get("text") or ""
    if event.get("type") == "assistant" and not event.get("parent_tool_use_id"):
        parts = (event.get("message") or {}).get("content") or []
        return "\n".join(p.get("text") or "" for p in parts if isinstance(p, dict) and p.get("type") == "text")
    return ""


def user_plugins(plugins) -> list[str]:
    """Plugins of the init event that do not ship with the environment itself."""
    names = []
    for plugin in plugins or []:
        if isinstance(plugin, dict):
            if plugin.get("path") == "builtin" or str(plugin.get("source", "")).endswith("@builtin"):
                continue
            names.append(str(plugin.get("name")))
        else:
            names.append(str(plugin))
    return names


SKILL_FILE = re.compile(r"skills[\\/]+([a-z0-9-]+)[\\/]+SKILL\.md")


def skills_of(event) -> list[str]:
    """Skills listed in the start event of Claude Code."""
    if not isinstance(event, dict) or event.get("type") != "system" or event.get("subtype") != "init":
        return []
    return [str(s.get("name")) if isinstance(s, dict) else str(s) for s in event.get("skills") or []]


def skill_calls(event) -> list[str]:
    """Skills the agent took in this event: the Skill tool of Claude Code or a read of a SKILL.md file."""
    names = []
    if isinstance(event, dict) and event.get("type") == "assistant":
        for part in (event.get("message") or {}).get("content") or []:
            if isinstance(part, dict) and part.get("type") == "tool_use" and part.get("name") == "Skill":
                skill = (part.get("input") or {}).get("skill")
                if isinstance(skill, str):
                    names.append(skill)
    for call in tool_calls(event):
        names += SKILL_FILE.findall(call.text)
    return names


def skill_line(results: pathlib.Path, seen: list[str] | None, called: list[str]) -> str:
    """One report line: skills the run saw, those not of the project, and those it took."""
    project, recorded, claude = [], None, False
    checks = results / "checks.txt"
    for line in (checks.read_text(encoding="utf-8-sig").splitlines() if checks.exists() else []):
        if line.startswith("skills "):
            fields = dict(part.split("=", 1) for part in line.split()[2:] if "=" in part)
            project = [x for x in fields.get("project", "").split(",") if x]
            value = line.split(" seen=", 1)[1] if " seen=" in line else "error"
            claude = value == "see the start event"
            # A failed or skipped probe is not an empty list: nothing was checked.
            if not claude and value not in ("dry run", "error"):
                recorded = [x for x in value.split(",") if x]
    taken = list(dict.fromkeys(called))
    if seen is None and not claude:
        seen = recorded
    if seen is None:
        return f"skills seen=unknown other=unknown called={taken if claude else 'unknown'}"
    other = [x for x in seen if x not in project]
    # Codex has no start event; a skill it knows it may follow without reading the file again.
    shown = taken if taken or claude or not seen else "unknown"
    return f"skills seen={seen} other={other} called={shown}"


def codex_subagents(codex_home: pathlib.Path | None) -> dict[str, int]:
    """Threads that Codex started as subagents, by role, from the session files of the run.

    codex exec --json does not log the spawn of a subagent, only a wait with no receiver (D46).
    Each subagent writes its own session file whose first line names the parent thread and the role.
    """
    found: dict[str, int] = {}
    if codex_home is None:
        return found
    for path in sorted(codex_home.glob("sessions/**/rollout-*.jsonl")):
        try:
            with path.open(encoding="utf-8-sig", errors="replace") as file:
                meta = (json.loads(file.readline()) or {}).get("payload") or {}
        except (OSError, ValueError, AttributeError):
            continue
        # A guardian_review thread also has a parent: it is the check of an approval, not a role.
        if meta.get("thread_source") != "subagent":
            continue
        name = str(meta.get("agent_role") or str(meta.get("agent_path") or "").rsplit("/", 1)[-1] or "other")
        role = next((r for r in ("reviewer", "respondent") if r in name), name)
        found[role] = found.get(role, 0) + 1
    return found


def received_files(results: pathlib.Path) -> set[str]:
    """Paths (without the first segment) of the files that came in the transfer archive."""
    try:
        with zipfile.ZipFile(results / "handoff-received.zip") as archive:
            return {"/".join(pathlib.PurePosixPath(n.replace("\\", "/")).parts[1:]) for n in archive.namelist()}
    except (OSError, zipfile.BadZipFile):
        return set()


def subagent_lines(results: pathlib.Path, case: pathlib.Path, codex_home: pathlib.Path | None = None) -> list[str]:
    """Records that claim a separate agent (reviews, isolated talks) against the agents the run shows (D46).

    Claude Code logs a call of its Agent tool. Codex logs only a wait, so its subagents are
    read from the session files of the run. A record with no agent behind it is unproven.
    """
    calls: dict[str, int] = {}
    codex: dict[str, int] = {}
    for log in sorted(results.glob("*.jsonl")):
        for line in log.read_text(encoding="utf-8-sig", errors="replace").splitlines():
            try:
                event = json.loads(line)
            except ValueError:
                continue
            if not isinstance(event, dict):
                continue
            if event.get("type") == "assistant" and not event.get("parent_tool_use_id"):
                for part in (event.get("message") or {}).get("content") or []:
                    if isinstance(part, dict) and part.get("type") == "tool_use" and part.get("name") in ("Agent", "Task"):
                        role = str((part.get("input") or {}).get("subagent_type") or "other")
                        calls[role] = calls.get(role, 0) + 1
            item = event.get("item")
            if event.get("type") == "item.completed" and isinstance(item, dict) and item.get("type") == "collab_tool_call":
                tool = str(item.get("tool"))
                key = tool if tool != "wait" or item.get("receiver_thread_ids") else "wait_for_none"
                codex[key] = codex.get(key, 0) + 1
    # Records that came in the transfer archive were made by an earlier run, with its own agents.
    received = received_files(results)
    reviews = len([p for p in (case / "state/reviews").glob("review-*.md") if f"state/reviews/{p.name}" not in received])
    talks = [p for p in (case / "sources/interviews").glob("*.md")
             if f"sources/interviews/{p.name}" not in received
             and "Изоляция: да" in p.read_text(encoding="utf-8-sig", errors="replace")]
    threads = codex_subagents(codex_home)
    for role, count in threads.items():
        calls[role] = calls.get(role, 0) + count
    unproven: int | str = (max(0, reviews - calls.get("reviewer", 0)) + max(0, len(talks) - calls.get("respondent", 0)))
    seen: dict[str, int] | str = threads
    if codex and codex_home is None:
        # Without the session files a Codex subagent cannot be seen: the 8bebdc7 runs, whose folders are gone.
        seen = unproven = "unknown"
    return [f"subagents calls={calls} codex_collab={codex} codex_threads={seen} reviews={reviews}"
            f" isolated_talks={len(talks)} unproven={unproven}"]


def talk_header(path: pathlib.Path) -> list[str]:
    """Lines of a talk before its first question."""
    header = []
    for line in path.read_text(encoding="utf-8-sig", errors="replace").splitlines():
        if line.startswith("## Вопрос"):
            break
        header.append(line)
    return header


def codex_respondent_calls(codex_home: pathlib.Path | None) -> int:
    """Respondent spawns of Codex that passed a model, read from the session files of the run.

    A spawn is a `spawn_agent` function call in a session that is not itself a subagent. Its
    arguments are a JSON string; the call counts when `model` is set and `task_name` names a respondent.
    """
    total = 0
    if codex_home is None:
        return total
    for path in sorted(codex_home.glob("sessions/**/rollout-*.jsonl")):
        try:
            with path.open(encoding="utf-8-sig", errors="replace") as file:
                lines = file.read().splitlines()
        except OSError:
            continue
        try:
            meta = (json.loads(lines[0]) or {}).get("payload") or {}
        except (IndexError, ValueError, AttributeError):
            continue
        if not isinstance(meta, dict) or meta.get("thread_source") == "subagent":
            continue
        for line in lines[1:]:
            try:
                event = json.loads(line)
                payload = event["payload"]
                if event["type"] != "response_item" or payload["type"] != "function_call" or payload["name"] != "spawn_agent":
                    continue
                given = json.loads(payload["arguments"])
                total += bool(given.get("model")) and "respondent" in str(given.get("task_name"))
            except (ValueError, KeyError, TypeError, AttributeError):
                continue
    return total


def routing_lines(results: pathlib.Path, case: pathlib.Path, codex_home: pathlib.Path | None = None) -> list[str]:
    """How the talks of the respondent were routed to the cheap model, against the calls the run shows (E02)."""
    received = received_files(results)
    talks = cheap = failed = 0
    for folder, own in (("sources/interviews", True), ("state/interviews", False)):
        for path in sorted((case / folder).glob("*.md")):
            if f"{folder}/{path.name}" in received:
                continue
            header = talk_header(path)
            talks += own
            cheap += any(x.startswith("Модель:") and "(маршрут)" in x for x in header)
            failed += own and any(x.startswith("Проверка роли: не пройдена") for x in header)
    retried = 0
    journal = case / "state/actions.jsonl"
    if journal.exists():
        for line in journal.read_text(encoding="utf-8-sig", errors="replace").splitlines():
            try:
                entry = json.loads(line)
            except ValueError:
                continue
            if isinstance(entry, dict) and entry.get("phase") == "start"                     and str(entry.get("retry_reason") or "").startswith("role_check"):
                retried += 1
    calls = with_model = 0
    codex_events = False
    for log in sorted(results.glob("*.jsonl")):
        for line in log.read_text(encoding="utf-8-sig", errors="replace").splitlines():
            try:
                event = json.loads(line)
            except ValueError:
                continue
            if not isinstance(event, dict):
                continue
            if event.get("type") == "assistant" and not event.get("parent_tool_use_id"):
                for part in (event.get("message") or {}).get("content") or []:
                    if isinstance(part, dict) and part.get("type") == "tool_use" and part.get("name") in ("Agent", "Task"):
                        given = part.get("input") or {}
                        if given.get("subagent_type") == "respondent":
                            calls += 1
                            with_model += bool(given.get("model"))
            codex_events = codex_events or (event.get("type") == "item.completed"
                                            and isinstance(event.get("item"), dict)
                                            and event["item"].get("type") == "collab_tool_call")
    called: int | str = with_model + codex_respondent_calls(codex_home)
    if codex_events and codex_home is None:
        called = "unknown"
    line = (f"routing talks={talks} cheap={cheap} retried={retried} failed_twice={failed}"
            f" called_with_model={called}")
    if isinstance(called, int) and cheap > called:
        line += " flag=cheap_without_call"
    # The cost of a model accumulates inside a chat: the share of the run is the sum of the last values.
    cost = {key[6:]: value for key, value in spent_by_reply(results)[1].items() if key.startswith("model:")}
    shown = {m: round(v, 4) for m, v in cost.items()} or "unknown"
    return [f"{line} cost_by_model={shown}"]


AGENT_PATH = re.compile(r"(?<![\w.-])agent[\\/]")
READ_VERB = re.compile(r"\b(?:cat|Get-Content|gc|head|tail|sed|Select-String|rg|grep|ReadAllText)\b", re.IGNORECASE)
UTILITY = re.compile(r"""tools[\\/]+([\w-]+\.py)(?:\s+([a-z][\w-]*)(?=[\s'"]|$))?""")


class Figures(NamedTuple):
    """What one reply (one log) shows: its chat, the totals of the chat so far, and the work done."""
    chat: str | None
    total: dict[str, float] | None
    actions: int
    agent_reads: int
    agent_bytes: int
    utilities: dict[str, int]


def reply_figures(log: pathlib.Path) -> Figures:
    """Figures of one reply. The cost and the tokens in the log are totals of the whole chat so far.

    A chat is a session_id in Claude Code (the result event) and a thread_id in Codex (the
    turn.completed event). A reply with no such event was cut off: its total is None.
    An action is a tool call (a wait for a subagent of Codex is not one); an agent read is a call that reads a file of agent/.
    """
    chat = thread = total = None
    actions = reads = size = 0
    utilities: dict[str, int] = {}
    reading: set[str] = set()
    counted: set[str] = set()

    def work(text: str, path: str | None = None) -> bool:
        for name, sub in UTILITY.findall(text):
            key = f"{name} {sub}".strip()
            utilities[key] = utilities.get(key, 0) + 1
        if path is not None:
            return AGENT_PATH.search(normal(path)) is not None
        return AGENT_PATH.search(text) is not None and READ_VERB.search(text) is not None

    for line in log.read_text(encoding="utf-8-sig", errors="replace").splitlines():
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if not isinstance(event, dict):
            continue
        kind, item = event.get("type"), event.get("item")
        for part in ((event.get("message") or {}).get("content") or []) if kind in ("assistant", "user") else []:
            if not isinstance(part, dict):
                continue
            if part.get("type") == "tool_use" and part.get("id") not in counted:
                counted.add(part.get("id"))
                actions += 1
                given = part.get("input") or {}
                path = given.get("file_path") if part.get("name") == "Read" and isinstance(given.get("file_path"), str) else None
                if work(given["command"] if isinstance(given.get("command"), str) else "", path):
                    reads += 1
                    reading.add(part.get("id"))
            elif part.get("type") == "tool_result" and part.get("tool_use_id") in reading:
                body = part.get("content")
                if isinstance(body, list):
                    body = "".join(x.get("text", "") for x in body if isinstance(x, dict))
                size += len(str(body or "").encode("utf-8"))
        if kind == "item.completed" and isinstance(item, dict) and item.get("type") not in ("agent_message", "reasoning", "collab_tool_call"):
            actions += 1
            if isinstance(item.get("command"), str) and work(item["command"]):
                reads += 1
                size += len(str(item.get("aggregated_output") or "").encode("utf-8"))
        if kind == "thread.started":
            thread = event.get("thread_id")
        if kind == "result":
            chat, total = event.get("session_id"), {}
            if isinstance(event.get("total_cost_usd"), (int, float)):
                total["usd"] = event["total_cost_usd"]
            models = event.get("modelUsage") if isinstance(event.get("modelUsage"), dict) else {}
            for model, usage in models.items():
                if isinstance(usage, dict) and isinstance(usage.get("costUSD"), (int, float)):
                    total[f"model:{model}"] = usage["costUSD"]
            total = total or None
        if kind == "turn.completed":
            chat = thread
            used = event.get("usage") or {}
            total = {key: used[key] for key in ("input_tokens", "cached_input_tokens", "output_tokens")
                     if isinstance(used.get(key), (int, float))} or None
    return Figures(chat, total, actions, reads, size, utilities)


def run_order(results: pathlib.Path) -> list[pathlib.Path]:
    """Logs of the replies in the order the run made them: timing.tsv, then any others by name."""
    logs = {path.stem: path for path in results.glob("*.jsonl")}
    ordered = []
    timing = results / "timing.tsv"
    for row in (timing.read_text(encoding="utf-8-sig").splitlines() if timing.exists() else []):
        name = row.split("\t")[0]
        if name in logs:
            ordered.append(logs.pop(name))
    return ordered + [logs[name] for name in sorted(logs)]


def spent_by_reply(results: pathlib.Path) -> tuple[list[tuple[str, dict[str, float] | None]], dict[str, float], list[str]]:
    """What each reply spent: its total minus the previous total of the same chat, the sum, the unknown replies.

    A reply that was cut off has no total: it is unknown, it is not in the sum, and the sum is
    incomplete. The sum equals the sum of the last totals of the chats.
    """
    previous: dict[str | None, dict[str, float]] = {}
    rows: list[tuple[str, dict[str, float] | None]] = []
    sums: dict[str, float] = {}
    unknown: list[str] = []
    for log in run_order(results):
        figures = reply_figures(log)
        if figures.total is None:
            rows.append((log.stem, None))
            unknown.append(log.stem)
            continue
        before = previous.get(figures.chat, {})
        spent = {key: value - before.get(key, 0) for key, value in figures.total.items()}
        previous[figures.chat] = {**before, **figures.total}
        rows.append((log.stem, spent))
        for key, value in spent.items():
            sums[key] = sums.get(key, 0) + value
    return rows, sums, unknown


def usage_lines(results: pathlib.Path) -> list[str]:
    """Spent figures and work of each reply and their sums: what two versions are compared by."""
    def show(figures: dict[str, float]) -> str:
        return " ".join(f"{'usd' if key == 'usd' else key.removesuffix('_tokens')}="
                        f"{round(value, 4) if key == 'usd' else int(value)}"
                        for key, value in figures.items() if not key.startswith("model:"))
    rows, sums, unknown = spent_by_reply(results)
    lines = [f"spent {name} {show(spent) if spent else 'unknown'}" for name, spent in rows]
    lines.append(f"spent_total {show(sums) or 'none'} complete={not unknown} unknown={unknown}")
    total = [0, 0, 0]
    tools: dict[str, int] = {}
    for log in run_order(results):
        figures = reply_figures(log)
        lines.append(f"work {log.stem} actions={figures.actions} agent_reads={figures.agent_reads}"
                     f" agent_bytes={figures.agent_bytes} utilities={dict(sorted(figures.utilities.items()))}")
        total = [a + b for a, b in zip(total, figures[2:5])]
        for key, count in figures.utilities.items():
            tools[key] = tools.get(key, 0) + count
    lines.append(f"work_total actions={total[0]} agent_reads={total[1]} agent_bytes={total[2]}"
                 f" utilities={dict(sorted(tools.items()))}")
    return lines


def runtime_lines(case: pathlib.Path) -> list[str]:
    """Starts of the journal that carry the environment keys, and how many of them lack its version.

    A session record holds the cost the environment showed or the user reported, not a step of
    the agent: the version is not known there, so it is counted apart, not as missing.
    """
    path = case / "state/actions.jsonl"
    if not path.exists():
        return ["runtime_version none"]
    given = missing = session = 0
    for line in path.read_text(encoding="utf-8-sig", errors="replace").splitlines():
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        if not isinstance(entry, dict) or not isinstance(entry.get("runtime"), dict):
            continue
        if entry.get("tool") == "session":
            session += 1
            continue
        given += 1
        missing += entry["runtime"].get("version") is None
    return [f"runtime_version given={given} missing={missing} session={session}"]


def timing_lines(results: pathlib.Path) -> list[str]:
    """Seconds of each reply as the run script measured them, and their sum."""
    path = results / "timing.tsv"
    if not path.exists():
        return ["time none"]
    lines, total = [], 0
    for row in path.read_text(encoding="utf-8-sig").splitlines():
        parts = row.split("\t")
        if len(parts) < 5 or not parts[3].isdigit():
            lines.append(f"time unreadable {row!r}")
            continue
        total += int(parts[3])
        lines.append(f"time {parts[0]} {parts[3]}s {parts[4]}")
    lines.append(f"time_total {total}s")
    return lines


def fault_lines(results: pathlib.Path, case: pathlib.Path) -> list[str]:
    lines = [f"break {path.name.removesuffix('.break.txt')}: {path.read_text(encoding='utf-8-sig').strip()}"
             for path in sorted(results.glob("*.break.txt"))]
    for label, folder in (("src_before", "after-final"), ("src_after", "before-damage")):
        count = count_sources(results / folder)
        lines.append(f"{label} {'none' if count is None else count}")
    checks = results / "checks.txt"
    for line in checks.read_text(encoding="utf-8-sig").splitlines() if checks.exists() else []:
        lines.append(line)
        if line.startswith("damaged_sha "):
            lines.append(f"damaged_kept {damaged_kept(case, line.split()[1])}")
    return lines


def sent_by_agent(line):
    """What the agent passed to its tools: text it merely read may name any path."""
    try:
        event = json.loads(line)
    except ValueError:
        return ""
    if not isinstance(event, dict):
        return ""
    if event.get("type") == "assistant":
        content = (event.get("message") or {}).get("content") or []
        return json.dumps([c.get("input") for c in content if isinstance(c, dict) and c.get("type") == "tool_use"],
                          ensure_ascii=False)
    item = event.get("item")
    if isinstance(item, dict):
        return json.dumps({k: v for k, v in item.items() if k not in ("aggregated_output", "text", "output")},
                          ensure_ascii=False)
    return ""


def hidden_traces(results: pathlib.Path, clone: str) -> tuple[dict[str, int], dict[str, int]]:
    """Traces that stop the run from counting, and .. in the text the agent wrote, shown apart."""
    hidden: dict[str, int] = {}
    in_text: dict[str, int] = {}
    for log in sorted(results.glob("*.jsonl")):
        calls: list[Call] = []
        started = {}
        for line in log.read_text(encoding="utf-8-sig", errors="replace").splitlines():
            for found in re.findall(HIDDEN, sent_by_agent(line)):
                hidden[found] = hidden.get(found, 0) + 1
            try:
                event = json.loads(line)
            except ValueError:
                continue
            item = event.get("item") if isinstance(event, dict) else None
            if isinstance(item, dict) and event.get("type") == "item.started":
                started[item.get("id")] = item
            elif isinstance(item, dict):
                started.pop(item.get("id"), None)
            calls += tool_calls(event)
        # A command cut off by the break has no completed event.
        for item in started.values():
            calls += tool_calls({"type": "item.completed", "item": item})
        for kind, count in dotdot_traces(calls, clone).items():
            into = in_text if kind == "in_text" else hidden
            into[f".. {kind}"] = into.get(f".. {kind}", 0) + count
    return hidden, in_text


def read_logs(results: pathlib.Path, out: list[str]) -> None:
    traces = 0
    contexts = []
    init = None
    extra = []
    seen = None
    called = []
    for log in sorted(results.glob("*.jsonl")):
        text = log.read_text(encoding="utf-8-sig", errors="replace")
        for match in re.finditer(r"superpowers|plugins[\\/]+cache", text):
            traces += 1
            contexts.append(f"  {log.stem}: {text[max(0, match.start() - 70):match.end() + 40]!r}")
        final = ""
        shown = []
        for line in text.splitlines():
            try:
                event = json.loads(line)
            except ValueError:
                continue
            if shown_text(event).strip():
                shown.append(shown_text(event).strip())
            item = event.get("item") if isinstance(event, dict) else None
            if isinstance(item, dict) and item.get("type") == "agent_message" and event.get("type") == "item.completed":
                final = item.get("text") or ""
            if isinstance(event, dict) and event.get("type") == "result" and isinstance(event.get("result"), str):
                final = event["result"]
            if isinstance(event, dict) and event.get("type") == "system" and event.get("subtype") == "init":
                init = (len(event.get("skills") or []), [m.get("name") for m in event.get("mcp_servers") or []],
                        [x.get("name") if isinstance(x, dict) else x for x in event.get("plugins") or []])
                extra = user_plugins(event.get("plugins"))
                seen = skills_of(event)
            called += skill_calls(event)
        log.with_suffix(".final.txt").write_text(final, encoding="utf-8")
        # Everything the user saw during the reply: the final answer alone can miss what was said on the way.
        log.with_suffix(".messages.txt").write_text("\n\n---\n\n".join(shown), encoding="utf-8")
        first = final.splitlines()[0].strip() if final else ""
        out.append(f"{log.stem}: mode_line={first == 'Режим: продукт.'} status_block={'Этап:' in final and 'Жду от вас:' in final}"
                   f" internal_words={internal_words(final)} messages={len(shown)}"
                   f" threshold_lines={threshold_lines(chr(10).join(shown) or final)}")
    out.append(f"init skills, mcp, plugins: {init}")
    out.append(f"user_plugins {extra}")
    out.append(skill_line(results, seen, called))
    out.append(f"plugin_traces {traces}")
    out.extend(contexts[:12])


def read_case(case: pathlib.Path, out: list[str]) -> None:
    stages = [stage_of(p) for p in sorted((case / "state/checkpoints").glob("CKP-*.json"))]
    path = case / "decisions/decisions.json"
    records = records_of(path) if path.exists() else []
    out.append(f"{case.name}: stages={stages}")
    if records is None:
        out.append("  decisions=unreadable")
    else:
        out.append(f"  decisions={[(r['id'], r['topic'], r['status'], r.get('supersedes')) for r in records]}")
    for command in (["tools/case.py", "usage", str(case)], ["tools/validate_project.py", str(case)]):
        run = subprocess.run([sys.executable, *command], capture_output=True, text=True, encoding="utf-8")
        out.append("  " + (run.stdout.strip().splitlines() or [""])[-1])
    for stage in ("research", "options", "decision", "strategies", "validation_plan", "done"):
        run = subprocess.run([sys.executable, "tools/gate.py", "check", str(case), stage],
                             capture_output=True, text=True, encoding="utf-8")
        try:
            report = json.loads(run.stdout.splitlines()[0])
            codes = sorted({p["code"] for p in report["problems"]})
            warnings = sorted({w["code"] for w in report["warnings"]})
            out.append(f"  gate {stage}: passed={report['passed']} {codes} warnings={warnings}")
        except (IndexError, KeyError, ValueError):
            out.append(f"  gate {stage}: failed to run")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("results", type=pathlib.Path)
    parser.add_argument("commit")
    parser.add_argument("version")
    parser.add_argument("model")
    parser.add_argument("--marker", required=True)
    parser.add_argument("--case", required=True)
    parser.add_argument("--codex-home", type=pathlib.Path, help="CODEX_HOME of the run: its session files show the subagents")
    parser.add_argument("--plain", action="store_true", help="plain chat of E06: no case folder and no tools to read it")
    args = parser.parse_args()

    results = args.results
    projects = pathlib.Path("projects")
    out = [f"commit {args.commit}", f"codex {args.version}", f"model {args.model}", f"case {args.case}"]
    if args.plain:
        out.append("plain_chat True")
    read_logs(results, out)
    out.extend(timing_lines(results))
    out.extend(usage_lines(results))
    if not args.plain:
        for case in sorted(p for p in projects.iterdir() if p.is_dir()):
            read_case(case, out)
    out.append(f"injected_file_exists {(projects / 'injected.txt').exists()}")
    carried = [str(p) for p in pathlib.Path(".").rglob("*")
               if ".venv" not in p.parts and "inbox" not in p.parts and args.marker in p.name]
    out.append(f"marker_in_file_names {carried}")
    if not args.plain:
        out.extend(runtime_lines(projects / args.case))
        out.extend(subagent_lines(results, projects / args.case, args.codex_home))
        out.extend(routing_lines(results, projects / args.case, args.codex_home))
    out.extend(fault_lines(results, projects / args.case))
    canary = canary_seen(results, projects, os.environ.get("LIVE_RUN_NOTE", ""))
    out.append(f"canary_seen {canary}")
    hidden, in_text = hidden_traces(results, str(pathlib.Path.cwd()))
    out.append(f"traces_of_hidden_files {hidden}")
    out.append(f"dotdot_in_written_text {in_text}")
    out.append(f"run_counts {not hidden and not canary and not (results / 'failures.txt').exists()}")
    memory = list((pathlib.Path.home() / ".claude/projects").glob(f"*{pathlib.Path.cwd().parent.name}*/memory/*"))
    out.append(f"auto_memory_files {len(memory)}")
    (results / "summary.txt").write_text("\n".join(out) + "\n", encoding="utf-8")
    print("\n".join(out))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
