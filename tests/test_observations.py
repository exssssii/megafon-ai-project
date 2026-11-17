import json
from pathlib import Path
import sys
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))
sys.path.insert(0, str(ROOT / "tests"))

import evidence  # noqa: E402
import image_bytes  # noqa: E402
import observations  # noqa: E402
import read_material  # noqa: E402

from test_read_material import Base, NO_TEXT, OK, REFUSED  # noqa: E402

VALID = """## Экран
целевой — оформление заказа, шаг 1 из 4

## Наблюдение 1
Где: верх, слева, шапка
Видно: логотип и название шага
Текст: «Шаг 1 из 4»

## Наблюдение 2
Где: низ, по центру, под формой
Видно: кнопка во всю ширину
Текст: «Подключить»

## Не разобрано
нет
"""

UNRESOLVED = VALID.replace("## Не разобрано\nнет", "## Не разобрано\nМелкий текст под кнопкой не читается.")


def with_label(label):
    return VALID.replace("целевой — оформление заказа, шаг 1 из 4", f"{label} — окно закрывает экран")


class Parsing(unittest.TestCase):
    def test_parse_valid(self):
        parsed = observations.parse(VALID)
        self.assertEqual((parsed.screen, parsed.screen_note), ("целевой", "оформление заказа, шаг 1 из 4"))
        self.assertEqual([item.index for item in parsed.items], [1, 2])
        self.assertEqual(parsed.items[1], observations.Item(
            2, "низ, по центру, под формой", "кнопка во всю ширину", "«Подключить»"))
        self.assertIsNone(parsed.unresolved)

    def test_parse_variants(self):
        crlf = observations.parse(VALID.replace("\n", "\r\n"))
        self.assertEqual(len(crlf.items), 2)
        colon = observations.parse(VALID.replace("целевой — оформление", "Целевой: оформление"))
        self.assertEqual(colon.screen, "целевой")
        next_line = observations.parse(VALID.replace("целевой — оформление", "целевой\nоформление"))
        self.assertEqual(next_line.screen_note, "оформление заказа, шаг 1 из 4")
        unreadable = observations.parse(VALID.replace("Текст: «Шаг 1 из 4»", "Текст: неразборчиво"))
        self.assertEqual(unreadable.items[0].text, "неразборчиво")
        self.assertEqual(observations.parse(UNRESOLVED).unresolved, "Мелкий текст под кнопкой не читается.")
        self.assertIsNone(observations.parse(VALID.replace("нет\n", "Нет.\n")).unresolved)

    def test_parse_rejects(self):
        cases = {
            "no screen": VALID.replace("## Экран\nцелевой — оформление заказа, шаг 1 из 4\n\n", ""),
            "text before the first section": "Вот что я увидел.\n\n" + VALID,
            "unknown label": VALID.replace("целевой —", "красивый —"),
            "screen without a note": VALID.replace("целевой — оформление заказа, шаг 1 из 4", "целевой"),
            "screen with two notes": VALID.replace("целевой — оформление заказа, шаг 1 из 4", "целевой — раз\nдва"),
            "gap in numbering": VALID.replace("Наблюдение 2", "Наблюдение 3"),
            "numbering not from one": VALID.replace("Наблюдение 1", "Наблюдение 0"),
            "item without text": VALID.replace("Текст: «Шаг 1 из 4»\n", ""),
            "item with a fourth line": VALID.replace("Текст: «Шаг 1 из 4»", "Текст: «Шаг 1 из 4»\nЕщё: лишнее"),
            "item with empty place": VALID.replace("Где: верх, слева, шапка", "Где:"),
            "item lines in another order": VALID.replace("Где: верх, слева, шапка\nВидно: логотип и название шага",
                                                         "Видно: логотип и название шага\nГде: верх, слева, шапка"),
            "text without quotes": VALID.replace("Текст: «Шаг 1 из 4»", "Текст: Шаг 1 из 4"),
            "empty quotes": VALID.replace("Текст: «Шаг 1 из 4»", "Текст: «  »"),
            "no unresolved section": VALID.replace("## Не разобрано\nнет\n", ""),
            "empty unresolved section": VALID.replace("нет\n", "\n"),
            "section after unresolved": VALID + "\n## Наблюдение 3\nГде: а\nВидно: б\nТекст: нет\n",
            "unknown section": VALID + "\n## Выводы\nХорошо.\n",
            "target without items": "## Экран\nцелевой — пусто\n\n## Не разобрано\nнет\n",
            "empty file": "",
        }
        for name, text in cases.items():
            with self.subTest(name):
                with self.assertRaises(observations.FormatError):
                    observations.parse(text)

    def test_text_line_is_only_quotes_or_a_marker(self):
        def with_text(value):
            return VALID.replace("Текст: «Шаг 1 из 4»", f"Текст: {value}")

        for value in ("«Шаг» и ещё слова", "слова «Шаг 1»", "«Шаг 1» продолжение", "нет, кроме «Шаг»",
                      "«Шаг 1» ««", "нет нет", "неразборчиво «Шаг»", "«Шаг 1»; и"):
            with self.subTest(value), self.assertRaises(observations.FormatError):
                observations.parse(with_text(value))
        for value, expected in (("«Шаг 1», «Шаг 2»", "«Шаг 1», «Шаг 2»"), ("«Шаг 1»; «Шаг 2»", "«Шаг 1»; «Шаг 2»"),
                                ("«Шаг 1»  «Шаг 2»", "«Шаг 1»  «Шаг 2»"), ("«Шаг 1»,", "«Шаг 1»,"),
                                ("Нет", "Нет"), ("неразборчиво.", "неразборчиво.")):
            with self.subTest(value):
                self.assertEqual(observations.parse(with_text(value)).items[0].text, expected)

    def test_a_name_in_quotes_inside_a_quote_is_kept_word_for_word(self):
        def with_text(value):
            return VALID.replace("Текст: «Шаг 1 из 4»", f"Текст: {value}")

        for value in ("«Тариф «Салон»»", "«Тариф «Салон»», «Дальше»", "«Пакет «Бизнес» на год»"):
            with self.subTest(value):
                self.assertEqual(observations.parse(with_text(value)).items[0].text, value)
        for value in ("«Тариф «Салон»", "«Тариф «Салон» «Про»", "«Тариф «Сал«он»»»", "««»»"):
            with self.subTest(value), self.assertRaises(observations.FormatError):
                observations.parse(with_text(value))

    def test_label_other_than_target_may_have_no_items(self):
        for label in observations.SCREEN_LABELS[1:]:
            with self.subTest(label):
                parsed = observations.parse(f"## Экран\n{label} — окно закрывает экран\n\n## Не разобрано\nнет\n")
                self.assertEqual((parsed.screen, parsed.items), (label, []))

    def test_error_reason_never_holds_the_text(self):
        marker = "СЕКРЕТ-ТЕКСТ-5"
        with self.assertRaises(observations.FormatError) as caught:
            observations.parse(VALID.replace("Текст: «Шаг 1 из 4»", f"Текст: {marker}"))
        self.assertNotIn(marker, caught.exception.reason)


class AddObservations(Base):
    def setUp(self):
        super().setUp()
        self.id = self.add(self.write("screen.png", image_bytes.png_bytes(1366, 768)))[1]["id"]

    def incoming(self, text, name="obs.md"):
        folder = self.case / "sources" / "incoming"
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / name
        path.write_bytes(text.encode("utf-8"))
        return path

    def observe(self, text=VALID, source=None):
        path = self.incoming(text)
        return self.run_tool("add-observations", self.case, source or self.id, "--text-file", path), path

    def record(self):
        return next(record for record in self.sources() if record["id"] == self.id)

    def cite(self, index, quote):
        request = self.incoming(json.dumps({"text": "На экране есть шаг.", "kind": "fact", "status": "confirmed", "supports": [
            {"source_id": self.id, "block": index, "quote": quote, "note": "Видно на снимке"}]}, ensure_ascii=False),
            "request.json")
        with mock.patch("sys.stdout"):
            return evidence.main(["add", str(self.case), str(request)])

    def test_observations_write_region_blocks(self):
        (code, report, _), path = self.observe()
        self.assertEqual((code, report["status"], report["complete"], report["blocks"]), (OK, "ok", True, 2))
        self.assertFalse(path.exists())
        blocks = self.blocks(self.id)
        self.assertEqual(blocks[1], {
            "n": 2, "locator": {"kind": "region", "index": 2, "area": "низ, по центру, под формой"},
            "text": "Где: низ, по центру, под формой\nВидно: кнопка во всю ширину\nТекст: «Подключить»"})
        record = self.record()
        self.assertEqual((record["extraction"]["status"], record["extraction"]["complete"]), ("ok", True))
        self.assertIn("PNG 1366×768", record["extraction"]["limits"])
        self.assertIn("Наблюдений: 2", record["extraction"]["limits"])
        self.assert_valid()

    def test_claim_cites_a_region_with_a_quote_from_the_text_line(self):
        self.observe()
        self.assertEqual(self.cite(2, "Подключить"), OK)
        claim = json.loads((self.case / "evidence" / "claims.json").read_text(encoding="utf-8"))["records"][0]
        self.assertEqual(claim["supports"][0]["locator"], {
            "kind": "region", "index": 2, "area": "низ, по центру, под формой", "quote": "Подключить"})
        self.assert_valid()

    def test_claim_cannot_quote_the_place_or_the_description(self):
        self.observe()
        for quote in ("под формой", "кнопка во всю ширину", "Текст:"):
            with self.subTest(quote):
                self.assertEqual(self.cite(2, quote), REFUSED)

    def test_observations_unresolved_is_partial(self):
        (code, report, _), _ = self.observe(UNRESOLVED)
        self.assertEqual((code, report["status"], report["complete"]), (OK, "partial", False))
        self.assertIn("Мелкий текст под кнопкой не читается", report["limits"])
        self.assert_valid()

    def test_observations_not_target_is_unavailable(self):
        (code, report, _), _ = self.observe(with_label("заслонён"))
        self.assertEqual((code, report["status"], report["complete"], report["blocks"]), (NO_TEXT, "unavailable", False, 0))
        self.assertIn("заслонён", report["limits"])
        self.assertFalse((self.case / "sources" / "extracted" / f"{self.id}.jsonl").exists())
        code, shown, _ = self.run_tool("show", self.case, self.id)
        self.assertEqual((code, shown["blocks_total"]), (NO_TEXT, 0))
        self.assert_valid()

    def test_items_of_a_screen_that_is_not_target_are_not_stored_and_say_so(self):
        text = VALID.replace("целевой — оформление заказа, шаг 1 из 4", "заслонён — окно куки")
        (code, report, _), _ = self.observe(text)
        self.assertEqual((code, report["blocks"], report["items_ignored"]), (NO_TEXT, 0, 2))

    def test_screen_that_is_not_target_can_become_target_again(self):
        self.observe(with_label("капча"))
        (code, report, _), _ = self.observe()
        self.assertEqual((code, report["status"]), (OK, "ok"))
        self.assertEqual(len(self.blocks(self.id)), 2)
        self.assert_valid()

    def test_observations_rewrite_before_reference(self):
        self.observe()
        (code, report, _), _ = self.observe(VALID.replace("Подключить", "Оформить"))
        self.assertEqual((code, report["blocks"]), (OK, 2))
        self.assertIn("Оформить", self.blocks(self.id)[1]["text"])
        self.assertEqual(len(self.blocks(self.id)), 2)

    def test_observations_locked_after_reference(self):
        self.observe()
        self.assertEqual(self.cite(1, "Шаг 1 из 4"), OK)
        extracted = self.case / "sources" / "extracted" / f"{self.id}.jsonl"
        before, registry = extracted.read_bytes(), (self.case / "evidence" / "sources.json").read_bytes()
        (code, report, _), path = self.observe(VALID.replace("Подключить", "Оформить"))
        self.assertEqual((code, report["status"]), (REFUSED, "refused"))
        self.assertEqual(report["referencing"], ["CLM-0001"])
        self.assertTrue(path.exists())
        self.assertEqual(extracted.read_bytes(), before)
        self.assertEqual((self.case / "evidence" / "sources.json").read_bytes(), registry)

    def test_observations_keep_capture_shortfall(self):
        report_path = self.case / "sources" / "extracted" / f"{self.id}.report.json"
        report_path.write_bytes(read_material.dumps(json.loads(report_path.read_text(encoding="utf-8"))
                                                     | {"capture_incomplete": "Снято 2 из 5 экранов."}))
        for text, status in ((VALID, "partial"), (with_label("вход"), "unavailable")):
            with self.subTest(status):
                (_, report, _), _ = self.observe(text)
                self.assertEqual(report["status"], status)
                self.assertTrue(report["limits"].startswith("Снято 2 из 5 экранов."), report["limits"])
                self.assertIn("PNG 1366×768", report["limits"])
        saved = json.loads(report_path.read_text(encoding="utf-8"))
        self.assertEqual(saved["capture_incomplete"], "Снято 2 из 5 экранов.")
        self.assertEqual((saved["width"], saved["height"]), (1366, 768))

    def test_observations_refuse_non_image(self):
        text_id = self.add(self.write("note.md", "# Заметка\n\nТекст."))[1]["id"]
        (code, report, _), path = self.observe(source=text_id)
        self.assertEqual((code, report["status"]), (REFUSED, "refused"))
        self.assertTrue(path.exists())

    def test_observations_refuse_withdrawn_unknown_and_unreadable_images(self):
        over = self.add(self.write("wide.png", image_bytes.png_bytes(9000, 100)))[1]["id"]
        (code, _, _), path = self.observe(source=over)
        self.assertEqual(code, REFUSED)
        self.assertTrue(path.exists())
        self.assertEqual(self.run_tool("add-observations", self.case, "SRC-0099", "--text-file", path)[0], REFUSED)
        with mock.patch("sys.stdout"):
            evidence.main(["withdraw", str(self.case), self.id, "--reason", "Не тот экран"])
        (code, _, _), path = self.observe()
        self.assertEqual(code, REFUSED)
        self.assertTrue(path.exists())

    def test_bad_file_is_refused_and_kept(self):
        before = (self.case / "evidence" / "sources.json").read_bytes()
        (code, report, _), path = self.observe("## Экран\nкрасивый — x\n")
        self.assertEqual((code, report["status"]), (REFUSED, "refused"))
        self.assertTrue(path.exists())
        self.assertEqual((self.case / "evidence" / "sources.json").read_bytes(), before)
        outside = self.write("obs.md", VALID)
        code, _, _ = self.run_tool("add-observations", self.case, self.id, "--text-file", outside)
        self.assertEqual(code, REFUSED)
        self.assertTrue(outside.exists())

    def test_locked_incoming_file_is_reported(self):
        path = self.incoming(VALID)
        with mock.patch.object(Path, "unlink", side_effect=PermissionError):
            code, report, _ = self.run_tool("add-observations", self.case, self.id, "--text-file", path)
        self.assertEqual((code, report["incoming_left"]), (OK, True))


class ObservationJournal(Base):
    def setUp(self):
        super().setUp()
        self.id = self.add(self.write("screen.png", image_bytes.png_bytes(1366, 768)))[1]["id"]

    def incoming(self, text, name="obs.md"):
        folder = self.case / "sources" / "incoming"
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / name
        path.write_bytes(text.encode("utf-8"))
        return path

    def observe(self):
        return self.run_tool("add-observations", self.case, self.id, "--text-file", self.incoming(VALID))

    def lines(self):
        path = self.case / "state" / "actions.jsonl"
        if not path.exists():
            return []
        return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]

    def test_the_action_is_journaled_with_environment(self):
        path = self.incoming(VALID)
        code, _, _ = self.run_tool("add-observations", self.case, self.id, "--text-file", path,
                                   "--environment", "codex", "--model", "m1")
        self.assertEqual(code, OK)
        start, end = self.lines()
        self.assertEqual((start["kind"], start["tool"], start["environment"], end["outcome"], end["result_refs"]),
                         ("file_read", "read_material.py add-observations", "codex", "completed", [self.id]))

    def test_a_bad_file_ends_failed_and_no_flag_leaves_the_journal_alone(self):
        path = self.incoming("не по формату")
        code, _, _ = self.run_tool("add-observations", self.case, self.id, "--text-file", path, "--environment", "codex")
        self.assertEqual(code, REFUSED)
        self.assertEqual([line["phase"] for line in self.lines()], ["start", "end"])
        self.assertEqual(self.lines()[1]["outcome"], "failed")
        before = self.lines()
        self.observe()
        self.assertEqual(self.lines(), before)

    def test_a_screen_without_text_ends_failed_and_a_retry_is_linked(self):
        path = self.incoming(with_label("заслонён"))
        code, report, _ = self.run_tool("add-observations", self.case, self.id, "--text-file", path,
                                        "--environment", "codex")
        self.assertEqual((code, report["status"]), (NO_TEXT, "unavailable"))
        self.assertEqual([(line["id"], line.get("outcome"), line.get("result_refs")) for line in self.lines()],
                         [("ACT-0001", None, None), ("ACT-0001", "failed", [self.id])])
        code, _, _ = self.run_tool("add-observations", self.case, self.id, "--text-file", self.incoming(VALID),
                                   "--environment", "codex", "--retry-of", "ACT-0001", "--retry-reason", "Другой экран")
        self.assertEqual(code, OK)
        start, end = self.lines()[2:]
        self.assertEqual((start["retry_of"], end["outcome"]), ("ACT-0001", "completed"))

    def test_a_site_snapshot_part_is_not_recorded_this_way(self):
        registry = self.case / "evidence" / "sources.json"
        data = json.loads(registry.read_text(encoding="utf-8"))
        data["records"][0]["origin"] = "public_web"
        registry.write_text(json.dumps(data), encoding="utf-8")
        text = self.incoming(VALID)
        code, report, _ = self.run_tool("add-observations", self.case, self.id, "--text-file", text,
                                         "--environment", "codex")
        self.assertEqual((code, report["status"]), (REFUSED, "refused"))
        self.assertEqual(self.lines(), [])


if __name__ == "__main__":
    unittest.main()
