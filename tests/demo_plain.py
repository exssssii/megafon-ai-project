"""The contract example without its interview extension.

The example holds a method choice, one synthetic conversation and a guide. Tests that count
records or actions of the plain example, or that add interview records of their own, start
from this copy.
"""

import json
from pathlib import Path
import shutil

MARKED_LINE = "Если живые интервью подтвердят, что участники уходят при введении платы [CLM-0009] (синтетика), пересматриваем решение.\n\n"
EXTRAS = {
    "evidence/sources.json": "SRC-0005",
    "evidence/claims.json": "CLM-0009",
    "decisions/decisions.json": "DEC-0006",
    "research/questions.json": "Q-0003",
}


def strip_interviews(case: Path) -> None:
    for name in ("outputs/interview-guide.md", "research/interviews", "sources/interviews",
                 "sources/extracted/SRC-0005.jsonl", "sources/extracted/SRC-0005.report.json"):
        path = case / name
        shutil.rmtree(path) if path.is_dir() else path.unlink()
    for name, record_id in EXTRAS.items():
        path = case / name
        document = json.loads(path.read_text(encoding="utf-8"))
        document["records"] = [r for r in document["records"] if r["id"] != record_id]
        path.write_text(json.dumps(document, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    journal = case / "state" / "actions.jsonl"
    lines = journal.read_text(encoding="utf-8").splitlines(keepends=True)
    journal.write_text("".join(line for line in lines if '"ACT-0013"' not in line and '"ACT-0014"' not in line),
                       encoding="utf-8")
    checkpoint = case / "state" / "checkpoints" / "CKP-0002.json"
    if checkpoint.exists():
        document = json.loads(checkpoint.read_text(encoding="utf-8"))
        document["completed_actions"] = [i for i in document["completed_actions"] if i not in ("ACT-0013", "ACT-0014")]
        document["human_choices"] = [i for i in document["human_choices"] if i != "DEC-0006"]
        checkpoint.write_text(json.dumps(document, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    commitments = case / "outputs" / "commitments.md"
    commitments.write_text(commitments.read_text(encoding="utf-8").replace(MARKED_LINE, ""), encoding="utf-8")


def copy_plain_demo(source: Path, target: Path) -> None:
    shutil.copytree(source, target)
    strip_interviews(target)
