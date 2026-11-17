import contextlib
import hashlib
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))

import role_check  # noqa: E402

DEMO = ROOT / "agent" / "contracts" / "examples" / "valid" / "demo-case"
TALK = "sources/interviews/talk.md"
CARDS = "research/interviews/personas.md"
CARDS_MD = """# Карточки

## Скептик

Тратит 12 000 рублей в год, скидка 15 %. Заходит раз в 2 недели.

## Активный участник

Приносит 40 книг в год.
"""


def talk_md(*sections, persona="Скептик", card=CARDS, header=True):
    """sections: (question, answer) pairs; answer None means the paragraph is absent."""
    lines = ["# Беседа", ""]
    if header:
        lines += [f"Персонаж: {persona}, карточка {card}", "Дата: 2026-11-10", ""]
    for number, (question, answer) in enumerate(sections, start=1):
        lines += [f"## Вопрос {number}", "", question, ""]
        if answer is not None:
            lines += [answer, ""]
    return "\n".join(lines)


BOM = bytes([0xEF, 0xBB, 0xBF])


class RoleCheckCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.case = Path(self._tmp.name) / "case"
        self.write(CARDS, CARDS_MD)

    def write(self, relative, text):
        path = self.case / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        return path

    def run_check(self, talk=TALK, case=None):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = role_check.main(["check", str(case or self.case), str(talk)])
        return code, json.loads(out.getvalue().splitlines()[0]), out.getvalue()

    def check_answers(self, *pairs, **options):
        self.write(TALK, talk_md(*pairs, **options))
        return self.run_check()

    def codes(self, report):
        return [item["code"] for item in report["violations"]]


class RoleCheckTests(RoleCheckCase):
    def test_the_sample_talk_of_the_demo_case_passes(self):
        code, report, _ = self.run_check("sources/interviews/synthetic-skeptic-1.md", case=DEMO)
        self.assertEqual((code, report["status"], report["card_scope"]), (0, "passed", "section"))
        self.assertEqual((report["persona"], report["checked"], report["violations"]), ("Скептик", 3, []))

    def test_role_not_loaded_is_a_violation_and_never_passes(self):
        code, report, _ = self.check_answers(("Как дела?", "РОЛЬ не загружена."))
        self.assertEqual((code, report["status"], self.codes(report)), (1, "failed", ["ROLE_NOT_LOADED"]))
        code, report, _ = self.check_answers(("Как дела?", "Нормально, спасибо."))
        self.assertEqual((code, self.codes(report)), (0, []))

    def test_empty_and_missing_answers_are_named(self):
        code, report, _ = self.check_answers(("Первый?", None), ("Второй?", "   "), ("Третий?", "Да."))
        self.assertEqual(code, 1)
        self.assertEqual([(v["code"], v["question"]) for v in report["violations"]],
                         [("ANSWER_MISSING", 1), ("ANSWER_MISSING", 2)])

    def test_six_sentences_pass_and_seven_fail(self):
        six = " ".join(["Это так."] * 5 + ["Правда?"])
        seven = six + " Вот!"
        _, report, _ = self.check_answers(("Как?", six))
        self.assertEqual(self.codes(report), [])
        _, report, _ = self.check_answers(("Как?", seven))
        self.assertEqual([(v["code"], v["detail"]) for v in report["violations"]], [("TOO_LONG", "7 предложений")])
        self.write(CARDS, CARDS_MD.replace("раз в 2 недели.", "раз в 2 недели. Оценка 3.5 балла."))
        decimal = " ".join(["Это так."] * 5 + ["Оценка 3.5 балла."])
        _, report, _ = self.check_answers(("Как?", decimal))
        self.assertEqual(self.codes(report), [])

    def test_a_number_from_the_card_or_the_question_is_not_foreign(self):
        answer = "Трачу 12000 рублей, иногда 15% скидки, а вообще 12 000. Ещё 777 из вопроса."
        _, report, _ = self.check_answers(("Что значит 777?", answer))
        self.assertEqual(self.codes(report), [])
        _, report, _ = self.check_answers(("Сколько?", "Выходит 4321 рубль."))
        self.assertEqual([(v["code"], v["detail"]) for v in report["violations"]], [("FOREIGN_NUMBER", "4321")])
        _, report, _ = self.check_answers(("Сколько?", "Выходит 1,5 раза."))
        self.assertEqual(self.codes(report), ["FOREIGN_NUMBER"])

    def test_single_digit_integers_are_ignored(self):
        _, report, _ = self.check_answers(("Как часто?", "Раз в 5 недель, не чаще."))
        self.assertEqual(self.codes(report), [])
        _, report, _ = self.check_answers(("Как часто?", "Примерно 37 процентов."))
        self.assertEqual(self.codes(report), ["FOREIGN_NUMBER"])

    def test_card_scope_falls_back_to_the_whole_file(self):
        answer = "Приношу 40 книг."
        _, report, _ = self.check_answers(("Сколько?", answer))
        self.assertEqual((report["card_scope"], self.codes(report)), ("section", ["FOREIGN_NUMBER"]))
        _, report, _ = self.check_answers(("Сколько?", answer), persona="Незнакомец")
        self.assertEqual((report["card_scope"], self.codes(report)), ("file", []))
        self.write(CARDS, CARDS_MD + "\n## скептик\n\nЕщё одна.\n")
        _, report, _ = self.check_answers(("Сколько?", answer))
        self.assertEqual((report["card_scope"], self.codes(report)), ("file", []))

    def test_speaking_for_others_phrases(self):
        for phrase in role_check.SPEAKS_FOR_OTHERS_PHRASES:
            with self.subTest(phrase=phrase):
                _, report, _ = self.check_answers(("Что думаете?", f"Знаете, {phrase.capitalize()}, и точка."))
                self.assertEqual(self.codes(report), ["SPEAKS_FOR_OTHERS"])
        _, report, _ = self.check_answers(("Что думаете?", "Я так считаю."))
        self.assertEqual(self.codes(report), [])

    def test_out_of_role_phrases(self):
        for phrase in role_check.OUT_OF_ROLE_PHRASES:
            with self.subTest(phrase=phrase):
                _, report, _ = self.check_answers(("Что?", f"Ну, {phrase.upper().replace('Е', 'Ё')} вот."))
                self.assertEqual(self.codes(report), ["OUT_OF_ROLE"], phrase)
                _, report, _ = self.check_answers(("Что?", f"Ну, {phrase.capitalize()}, вот."))
                self.assertEqual(self.codes(report), ["OUT_OF_ROLE"], phrase)
        _, report, _ = self.check_answers(("Что?", "Лежит навкарточке и вв карточке не значится."))
        self.assertEqual(self.codes(report), [])

    def test_a_broken_talk_is_refused_not_passed(self):
        cases = {
            "talk_malformed": [talk_md(("Q?", "A."), header=False), "# Беседа\n\nПерсонаж: Скептик, карточка " + CARDS],
            "talk_unreadable": [b"\xff\xfe\x00bad"],
        }
        for reason, variants in cases.items():
            for variant in variants:
                with self.subTest(reason=reason):
                    path = self.case / TALK
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_bytes(variant if isinstance(variant, bytes) else variant.encode("utf-8"))
                    code, report, _ = self.run_check()
                    self.assertEqual((code, report["status"], report["reason"]), (4, "refused", reason))
        (self.case / TALK).unlink()
        code, report, _ = self.run_check()
        self.assertEqual((code, report["status"], report["reason"]), (4, "refused", "talk_unreadable"))

    def test_a_missing_card_file_is_refused(self):
        self.write(TALK, talk_md(("Q?", "A."), card="research/nope.md"))
        code, report, _ = self.run_check()
        self.assertEqual((code, report["status"], report["reason"]), (4, "refused", "persona_card_missing"))

    def test_question_paragraph_is_checked_for_role_not_loaded_and_out_of_role(self):
        talk = ("# Беседа\n\nПерсонаж: Скептик, карточка " + CARDS
                + "\n\n## Вопрос 1\n\nРоль не загружена.\n\nОк.\n")
        self.write(TALK, talk)
        code, report, _ = self.run_check()
        self.assertEqual((code, self.codes(report)), (1, ["ROLE_NOT_LOADED"]))
        code, report, _ = self.check_answers(("Как ИИ вы это видите?", "Ок."))
        self.assertEqual((code, self.codes(report)), (1, ["OUT_OF_ROLE"]))
        code, report, _ = self.check_answers(("Как вы это видите?", "Ок."))
        self.assertEqual((code, self.codes(report)), (0, []))

    def test_abbreviations_and_ellipses_do_not_split_sentences(self):
        for answer in ("т.е. т.к. и т.д. так бывает", "Ну... не знаю... может... да... ладно... хм... ок",
                       "Ну… не знаю… может… да… ладно… хм… ок"):
            with self.subTest(answer=answer):
                self.assertEqual(role_check.count_sentences(answer), 1)
                _, report, _ = self.check_answers(("Как?", answer))
                self.assertEqual(self.codes(report), [])
        seven = "Раз. Два! Три? Четыре. Пять. Шесть. 7 дней."
        self.assertEqual(role_check.count_sentences(seven), 7)
        _, report, _ = self.check_answers(("Как?", seven))
        self.assertEqual(self.codes(report), ["TOO_LONG"])

    def test_a_year_is_not_a_foreign_number(self):
        _, report, _ = self.check_answers(("Когда?", "С 2019 года так."))
        self.assertEqual(self.codes(report), [])
        for text in ("Выходит 2019 % сверху.", "Выходит 2500 рублей."):
            with self.subTest(text=text):
                _, report, _ = self.check_answers(("Сколько?", text))
                self.assertEqual(self.codes(report), ["FOREIGN_NUMBER"])

    def test_a_bom_in_the_talk_and_in_the_cards_is_accepted(self):
        self.write(TALK, talk_md(("Q?", "Нормально.")))
        (self.case / TALK).write_bytes(BOM + (self.case / TALK).read_bytes())
        (self.case / CARDS).write_bytes(BOM + (self.case / CARDS).read_bytes())
        code, report, _ = self.run_check()
        self.assertEqual((code, report["status"]), (0, "passed"))

    def test_an_absurdly_long_number_is_foreign_not_a_crash(self):
        code, report, _ = self.check_answers(("Сколько?", "Выходит " + "9" * 400 + " рублей."))
        self.assertEqual((code, self.codes(report)), (1, ["FOREIGN_NUMBER"]))

    def test_a_card_path_outside_the_case_is_refused(self):
        outside = Path(self._tmp.name) / "cards.md"
        outside.write_text(CARDS_MD, encoding="utf-8")
        for card in ("../cards.md", str(outside)):
            with self.subTest(card=card):
                self.write(TALK, talk_md(("Q?", "A."), card=card))
                code, report, _ = self.run_check()
                self.assertEqual((code, report["reason"]), (4, "persona_card_missing"))

    def test_a_talk_outside_the_case_is_refused(self):
        outside = Path(self._tmp.name) / "outside.md"
        outside.write_text(talk_md(("Q?", "A.")), encoding="utf-8")
        for talk in ("../outside.md", str(outside)):
            with self.subTest(talk=talk):
                code, report, _ = self.run_check(talk)
                self.assertEqual((code, report["reason"]), (4, "outside_case"))

    def test_nothing_is_written_to_the_case(self):
        self.write(TALK, talk_md(("Q?", "Слишком " * 3 + "много. " * 8)))

        def snapshot():
            return {str(p.relative_to(self.case)): hashlib.sha256(p.read_bytes()).hexdigest()
                    for p in sorted(self.case.rglob("*")) if p.is_file()}

        before = snapshot()
        self.run_check()
        self.assertEqual(snapshot(), before)

    def test_output_never_quotes_the_answer(self):
        answer = "Секретнаяфраза про 98765 рублей. " + "Длинно. " * 7 + "Как ИИ я молчу."
        code, report, raw = self.check_answers(("Что?", answer))
        self.assertEqual(code, 1)
        self.assertEqual(sorted(self.codes(report)), ["FOREIGN_NUMBER", "OUT_OF_ROLE", "TOO_LONG"])
        self.assertNotIn("Секретнаяфраза", raw)
        self.assertNotIn("Длинно", raw)

    def test_cli_first_line_is_json_and_exit_codes(self):
        script = str(ROOT / "tools" / "role_check.py")
        self.write(TALK, talk_md(("Q?", "Нормально.")))
        self.write("sources/interviews/bad.md", talk_md(("Q?", "Роль не загружена.")))
        runs = [(TALK, 0, "passed"), ("sources/interviews/bad.md", 1, "failed"), ("../x.md", 4, "refused")]
        for talk, expected, status in runs:
            with self.subTest(talk=talk):
                done = subprocess.run([sys.executable, script, "check", str(self.case), talk], cwd=ROOT,
                                      capture_output=True, text=True, encoding="utf-8")
                self.assertEqual(done.returncode, expected, done.stderr)
                self.assertEqual(json.loads(done.stdout.splitlines()[0])["status"], status)
        done = subprocess.run([sys.executable, script, "check"], cwd=ROOT, capture_output=True, text=True,
                              encoding="utf-8")
        first = json.loads(done.stdout.splitlines()[0])
        self.assertEqual((done.returncode, first["status"], first["reason"]), (4, "refused", "usage"))


if __name__ == "__main__":
    unittest.main()
