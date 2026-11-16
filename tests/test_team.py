import shutil
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))

import team  # noqa: E402

HEAD = "# Команда\n\nИсточник: слова пользователя, действие ACT-0031\n\n"
TABLE = "| Имя | Роль |\n|---|---|\n| Анна К. | аналитик |\n| Игорь С. | разработка |\n"


class ReadTeam(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    def read(self, text, newline="\n", bom=False):
        data = text.replace("\n", newline).encode("utf-8")
        (self.tmp / "team.md").write_bytes((b"\xef\xbb\xbf" if bom else b"") + data)
        return team.read_team(self.tmp)

    def test_no_file(self):
        self.assertIsNone(team.read_team(self.tmp))

    def test_table_in_order(self):
        result = self.read(HEAD + TABLE)
        self.assertEqual(result.members, [("Анна К.", "аналитик"), ("Игорь С.", "разработка")])
        self.assertEqual((result.action_id, result.problems), ("ACT-0031", []))

    def test_not_named(self):
        result = self.read(HEAD + team.NOT_NAMED + "\n")
        self.assertEqual((result.members, result.problems), (None, []))

    def test_bom_and_crlf_parse_the_same(self):
        plain = self.read(HEAD + TABLE)
        self.assertEqual(self.read(HEAD + TABLE, "\r\n", bom=True), plain)

    def test_format_errors(self):
        cases = {
            "no source": "# Команда\n\n" + TABLE,
            "wrong columns": HEAD + "| Имя | Должность |\n|---|---|\n| Анна К. | аналитик |\n",
            "empty cell": HEAD + "| Имя | Роль |\n|---|---|\n| Анна К. | |\n",
            "duplicate": HEAD + TABLE + "| Анна   К. | тестировщик |\n",
            "comma in a name": HEAD + "| Имя | Роль |\n|---|---|\n| Крылова, Анна | аналитик |\n",
            "nothing": HEAD,
            "both": HEAD + TABLE + team.NOT_NAMED + "\n",
        }
        for name, text in cases.items():
            with self.subTest(name):
                self.assertTrue(self.read(text).problems)

    def test_owners_of(self):
        self.assertEqual(team.owners_of("Анна  К. , Игорь С."), ["Анна К.", "Игорь С."])
        self.assertEqual(team.owners_of(" , "), [])


if __name__ == "__main__":
    unittest.main()
