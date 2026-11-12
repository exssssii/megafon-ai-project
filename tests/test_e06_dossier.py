"""Dossiers of the comparison E06: what the judge gets, what is hidden from it and what stays out."""
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import unittest
import zipfile

ROOT = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("e06_dossier", ROOT / "scripts" / "e06_dossier.py")
dossier = importlib.util.module_from_spec(spec)
sys.modules["e06_dossier"] = dossier
spec.loader.exec_module(dossier)

PROTOCOL = """# Протокол

## Сценарий реплик

Слова «бриф» и «пороги» — язык нашего агента.

| Подстановка | Значение |
|---|---|
| `{имя}` | про Окошко |

Файлы попадают в папку прямо перед репликой.

| Реплика | Текст |
|---|---|
| `c1-1` | Запрос |

## Что измеряем

### Существенные ошибки и пропуски

Определения ошибок.

### Пункты оценки

| Пункт | «Да», если |
|---|---|
| K1 | Постановка |

### Контрольные числа

Категории чисел.

### Время, расход и помощь человека

Не для судьи.

## Оценка моделью-судьёй

### Задание судьи

> Оцени X и Y.

## Подготовка до прогонов

Не для судьи.
"""


def lines(*events):
    return "\n".join(json.dumps(e, ensure_ascii=False) for e in events) + "\n"


def claude_reply(text):
    return lines({"type": "assistant", "message": {"content": [{"type": "text", "text": "Читаю материалы."}]}},
                 {"type": "result", "result": text, "session_id": "s"})


def codex_reply(*texts, completed=True):
    events = [{"type": "thread.started", "thread_id": "t"}]
    events += [{"type": "item.completed", "item": {"type": "agent_message", "text": t}} for t in texts]
    if completed:
        events.append({"type": "turn.completed", "usage": {}})
    return lines(*events)


def write(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def agent_results(root, case="retention-smb"):
    results = root / "agent"
    write(results / "summary.txt", f"commit aaa\nmodel claude-sonnet-5-5\ncase {case}\nrun_counts True\n")
    write(results / "c1-1.jsonl", claude_reply("Режим: продукт.\n\nКейс создан, бриф в projects/retention-smb/brief.md, запись DEC-0002."))
    write(results / "c2.jsonl", lines({"type": "assistant", "message": {"content": [{"type": "text", "text": "Пишу стратегии."}]}}))
    write(results / "c4.jsonl", claude_reply("Утилита сломана."))
    final = results / "after-final" / case
    write(final / "brief.md", "# Бриф\n\nПроверено tools/gate.py, см. evidence/claims.json и SRC-0003.\n")
    write(final / "outputs/options.md", "# Варианты\n\nОставить, закрыть, развивать.\n")
    write(final / "outputs/accounting.md", "# Учёт\n")
    write(final / "outputs/.history/1/options.md", "# Старые варианты\n")
    write(final / "state/notes.md", "# Состояние\n")
    write(final / "sources/page.md", "# Страница\n")
    write(final / "evidence/claims.json", "{}")
    return results


def plain_results(root, case="retention-smb"):
    results = root / "plain"
    write(results / "summary.txt", f"commit aaa\nmodel claude-sonnet-5-5\ncase {case}\nplain_chat True\nrun_counts True\n")
    write(results / "c1-1.jsonl", codex_reply("Смотрю файлы.", "Вот разбор: отток 6,2 %."))
    write(results / "c2.jsonl", codex_reply("Начинаю стратегию.", completed=False))
    write(results / "written/projects/notes.md", "# Заметки чата\n\nВыбран вариант развития.\n")
    write(results / "written/projects/data.bin", "x")
    return results


class Anonymise(unittest.TestCase):
    def test_service_traces_go(self):
        text = ("См. projects/retention-smb/outputs/options.md, C:\\b12\\clone\\inbox\\notes.md, tools/gate.py, "
                "decisions.json, actions.jsonl, DEC-0002 и SRC-0003.")
        self.assertEqual(dossier.anonymise(text), "См. options.md, notes.md, [утилита], [служебный файл], "
                                                   "[служебный файл], [запись] и [запись].")

    def test_wording_urls_and_ratios_stay(self):
        text = "Цены: https://www.yclients.ru/info/pricing, доли 50/50, 120 тыс./мес., сайт example.com/a/b.json"
        self.assertEqual(dossier.anonymise(text), text.replace("example.com/a/b.json", "example.com/a/[служебный файл]"))

    def test_url_with_a_script_name_is_kept_whole(self):
        url = "https://example.com/tools/gate.py"
        self.assertEqual(dossier.anonymise(f"Адрес {url} недоступен"), f"Адрес {url} недоступен")

    def test_mode_line_goes(self):
        self.assertEqual(dossier.anonymise("Режим: продукт.\n\nОтвет."), "\nОтвет.")


class FinalText(unittest.TestCase):
    def check(self, text):
        with tempfile.TemporaryDirectory() as tmp:
            log = Path(tmp) / "r.jsonl"
            log.write_text(text, encoding="utf-8")
            return dossier.final_text(log)

    def test_claude_result(self):
        self.assertEqual(self.check(claude_reply("Итог.")), ("Итог.", True))

    def test_codex_last_message_of_a_completed_turn(self):
        self.assertEqual(self.check(codex_reply("Читаю.", "Итог.")), ("Итог.", True))

    def test_broken_reply_gives_all_it_said(self):
        self.assertEqual(self.check(codex_reply("Читаю.", "Пишу.", completed=False)), ("Читаю.\n\nПишу.", False))
        broken = lines({"type": "assistant", "message": {"content": [{"type": "text", "text": "Пишу."}]}}, "not json")
        self.assertEqual(self.check(broken), ("Пишу.", False))


class Build(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.protocol = self.root / "protocol.md"
        self.protocol.write_text(PROTOCOL, encoding="utf-8")

    def tearDown(self):
        self.tmp.cleanup()

    def build(self, first, second, swap):
        out = self.root / "out"
        return out, dossier.build(first, second, out, self.protocol, swap=swap)

    def test_labels_follow_the_coin_and_the_key_stays_out_of_the_judge_folder(self):
        agent, plain = agent_results(self.root), plain_results(self.root)
        out, key = self.build(agent, plain, swap=True)
        self.assertEqual(key["labels"]["X"]["plain_chat"], True)
        self.assertEqual(key["labels"]["Y"]["plain_chat"], False)
        self.assertEqual(sorted(p.name for p in (out / "judge").iterdir()),
                         ["control.json", "dossier-X.md", "dossier-Y.md", "expectations.md", "task.md"])
        self.assertTrue((out / "key.json").exists())
        for name in ("dossier-X.md", "dossier-Y.md", "task.md"):
            text = (out / "judge" / name).read_text(encoding="utf-8")
            self.assertNotIn("plain", text)
            self.assertNotIn(str(agent), text)

    def test_agent_dossier_holds_the_segment_and_its_result_only(self):
        out, _ = self.build(agent_results(self.root), plain_results(self.root), swap=False)
        text = (out / "judge" / "dossier-X.md").read_text(encoding="utf-8")
        self.assertIn("### c1-1", text)
        self.assertIn("Кейс создан, бриф в brief.md, запись [запись].", text)
        self.assertNotIn("Режим:", text)
        self.assertIn("### c2\n\n_Реплика оборвана", text)
        self.assertNotIn("Утилита сломана", text)
        self.assertIn("### options.md", text)
        self.assertIn("### brief.md", text)
        self.assertIn("Проверено [утилита], см. [служебный файл] и [запись].", text)
        for hidden in ("Учёт", "Старые варианты", "Состояние", "Страница"):
            self.assertNotIn(hidden, text)

    def test_plain_dossier_holds_what_the_chat_wrote(self):
        out, _ = self.build(agent_results(self.root), plain_results(self.root), swap=False)
        text = (out / "judge" / "dossier-Y.md").read_text(encoding="utf-8")
        self.assertIn("Вот разбор: отток 6,2 %.", text)
        self.assertNotIn("Смотрю файлы.", text)
        self.assertIn("_Реплика оборвана, ниже всё, что исполнитель успел сказать до обрыва._\n\nНачинаю стратегию.", text)
        self.assertIn("### notes.md", text)
        self.assertNotIn("data.bin", text)

    def test_task_has_the_judge_sections_without_the_notes(self):
        out, _ = self.build(agent_results(self.root), plain_results(self.root), swap=False)
        task = (out / "judge" / "task.md").read_text(encoding="utf-8")
        self.assertTrue(task.startswith("# Задание судьи\n\n> Оцени X и Y."))
        for present in ("| `c1-1` | Запрос |", "Определения ошибок.", "| K1 | Постановка |", "Категории чисел."):
            self.assertIn(present, task)
        for absent in ("язык нашего агента", "Не для судьи", "Файлы попадают"):
            self.assertNotIn(absent, task)
        # Two tables stay two tables.
        self.assertIn("| `{имя}` | про Окошко |\n\n| Реплика | Текст |", task)

    def test_zip_archives_are_read(self):
        archives = []
        for results in (agent_results(self.root), plain_results(self.root)):
            archive = self.root / f"{results.name}.zip"
            with zipfile.ZipFile(archive, "w") as packed:
                for path in results.rglob("*"):
                    if path.is_file():
                        # Compress-Archive of Windows PowerShell writes backslashes.
                        packed.write(path, str(path.relative_to(results)).replace("/", "\\"))
            archives.append(archive)
        out, key = self.build(*archives, swap=False)
        self.assertIn("Вот разбор", (out / "judge" / "dossier-Y.md").read_text(encoding="utf-8"))
        self.assertEqual(key["labels"]["X"]["source"], str(archives[0]))

    def test_unsafe_archive_path_is_refused(self):
        archive = self.root / "bad.zip"
        with zipfile.ZipFile(archive, "w") as packed:
            packed.writestr("../escape.txt", "x")
        with self.assertRaises(SystemExit):
            self.build(archive, plain_results(self.root), swap=False)

    def test_runs_of_different_cases_are_refused(self):
        with self.assertRaises(SystemExit):
            self.build(agent_results(self.root), plain_results(self.root, case="direction-b2c"), swap=False)

    def test_a_filled_judge_folder_is_not_overwritten(self):
        write(self.root / "out" / "judge" / "dossier-X.md", "прежняя оценка")
        with self.assertRaises(SystemExit):
            self.build(agent_results(self.root), plain_results(self.root), swap=False)

    def test_protocol_without_a_section_is_refused(self):
        self.protocol.write_text(PROTOCOL.replace("### Задание судьи", "### Другое"), encoding="utf-8")
        with self.assertRaises(SystemExit):
            self.build(agent_results(self.root), plain_results(self.root), swap=False)


if __name__ == "__main__":
    unittest.main()
