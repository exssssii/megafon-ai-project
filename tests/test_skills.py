"""Project skills (E14): one thin wrapper per procedure, the same file for Claude Code and Codex."""
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PLACES = (ROOT / ".claude" / "skills", ROOT / ".agents" / "skills")
NAMES = {"resuming-case", "running-interviews", "revising-on-new-data"}
TARGETS = {
    "resuming-case": {"agent/workflows/intake.md", "agent/workflows/checkpoint.md#новый-чат"},
    "running-interviews": {"agent/workflows/interviews.md"},
    "revising-on-new-data": {"agent/workflows/revision.md"},
}
PATH = re.compile(r"`(agent/[^`#\s]+\.md)(?:#([^`\s]+))?`")
BODY_LIMIT = 1200


def folders(place: Path) -> set[str]:
    return {p.name for p in place.iterdir() if p.is_dir()} if place.is_dir() else set()


def split(text: str) -> tuple[dict[str, str], str]:
    match = re.match(r"---\n(.*?)\n---\n(.*)", text, re.S)
    if not match:
        raise AssertionError("no frontmatter")
    fields = {}
    for line in match.group(1).splitlines():
        key, _, value = line.partition(":")
        fields[key.strip()] = value.strip()
    return fields, match.group(2)


def slug(heading: str) -> str:
    return re.sub(r"[^\w\- ]", "", heading.strip().lower()).replace(" ", "-")


def anchors(path: Path) -> set[str]:
    return {slug(line.lstrip("#")) for line in path.read_text(encoding="utf-8").splitlines() if line.startswith("#")}


class SkillsTest(unittest.TestCase):
    def skill(self, name: str, place: Path = PLACES[0]) -> Path:
        return place / name / "SKILL.md"

    def test_both_environments_have_the_same_skills(self):
        self.assertEqual(folders(PLACES[0]), NAMES)
        self.assertEqual(folders(PLACES[1]), NAMES)

    def test_copies_are_byte_identical(self):
        for name in NAMES:
            self.assertEqual(self.skill(name).read_bytes(), self.skill(name, PLACES[1]).read_bytes(), name)

    def test_files_use_lf(self):
        for place in PLACES:
            for name in NAMES:
                self.assertNotIn(b"\r", self.skill(name, place).read_bytes(), f"{place.parent.name}/{name}")

    def test_frontmatter(self):
        for name in NAMES:
            fields, _ = split(self.skill(name).read_text(encoding="utf-8"))
            self.assertEqual(set(fields), {"name", "description"}, name)
            self.assertEqual(fields["name"], name)
            self.assertRegex(fields["name"], r"^[a-z0-9-]{1,64}$")
            self.assertLessEqual(len(fields["description"]), 1024, name)
            self.assertNotRegex(fields["description"], r"[<>]", name)

    def test_body_is_a_thin_wrapper(self):
        for name in NAMES:
            _, body = split(self.skill(name).read_text(encoding="utf-8"))
            self.assertLessEqual(len(body), BODY_LIMIT, name)
            for needle in ("projects/", "разработ", "agent/instructions/product-mode.md", "ещё не прочитан"):
                self.assertIn(needle, body, f"{name}: {needle}")

    def test_links_resolve(self):
        for name in NAMES:
            _, body = split(self.skill(name).read_text(encoding="utf-8"))
            for path, anchor in PATH.findall(body):
                target = ROOT / path
                self.assertTrue(target.is_file(), f"{name}: {path}")
                if anchor:
                    self.assertIn(anchor, anchors(target), f"{name}: {path}#{anchor}")

    def test_each_skill_points_at_its_procedure(self):
        for name, wanted in TARGETS.items():
            _, body = split(self.skill(name).read_text(encoding="utf-8"))
            found = {path + (f"#{anchor}" if anchor else "") for path, anchor in PATH.findall(body)}
            self.assertLessEqual(wanted, found, name)


if __name__ == "__main__":
    unittest.main()
