from pathlib import Path
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))

import image_header  # noqa: E402

try:
    import make_ux_fixtures
    FONT = next((item for item in make_ux_fixtures.FONT_CANDIDATES if item.exists()), None)
except ImportError:
    make_ux_fixtures = None
    FONT = None

NAMES = (
    [f"own-checkout-{number}.png" for number in range(1, 5)]
    + [f"rival-checkout-{number}.png" for number in range(1, 3)]
    + ["tiny-text.png", "cookie-wall.png", "captcha.png", "blank.png"]
)


@unittest.skipUnless(make_ux_fixtures and FONT, "Pillow and a Cyrillic font are needed (fixtures group)")
class UxFixtures(unittest.TestCase):
    def test_generator_writes_screens_and_answers(self):
        with tempfile.TemporaryDirectory() as directory:
            out = Path(directory)
            self.assertEqual(make_ux_fixtures.main(["make_ux_fixtures.py", str(out)]), 0)
            for name in NAMES:
                info = image_header.read_header((out / name).read_bytes())
                self.assertEqual((info.format, info.width, info.height), ("png", 1366, 768), name)
            answers = (out / "answers.md").read_text(encoding="utf-8")
            for name in NAMES:
                self.assertIn(f"## {name}", answers)
            self.assertIn("| Шагов до оформления | 4 | 2 |", answers)

    def test_answers_name_a_screen_label_for_each_file(self):
        labels = {screen.name: screen.label for screen in make_ux_fixtures.screens()}
        self.assertEqual(sorted(labels), sorted(NAMES))
        self.assertEqual(labels["cookie-wall.png"], "заслонён")
        self.assertEqual(labels["captcha.png"], "капча")
        self.assertEqual(labels["blank.png"], "пусто")


if __name__ == "__main__":
    unittest.main()
