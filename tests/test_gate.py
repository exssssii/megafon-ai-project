import contextlib
import hashlib
import io
import json
from pathlib import Path
import shutil
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))

import gate  # noqa: E402

DEMO = ROOT / "agent" / "contracts" / "examples" / "valid" / "demo-case"
PROBLEMS, REFUSED = 1, 4
STATUS = "Статус: выбор сделан [DEC-0002], вариант [OPT-0001]"
DRAFT = "Статус: предварительно, выбор не сделан, предложен вариант [OPT-0003]"

OPTIONS_MD = """# Варианты

## Что известно

Заявок в среднем 120 в месяц [CALC-0001].

## Чего не хватает

Доходы неизвестны [Q-0002].

## Сравнение вариантов

| Вариант | Действие |
|---|---|
| [OPT-0001] | Не менять |
| [OPT-0002] | Закрыть |
| [OPT-0003] | Развивать |

## Рекомендация

Развивать [OPT-0003].

## Что изменит рекомендацию

Падение заявок.
"""

PRODUCT_MD = f"""# Продуктовая стратегия

{STATUS}

## Аудитория и задача

Жители района [CLM-0002].

## Текущие альтернативы

Покупка книг.

## Выбранное направление

Сохраняем сервис [OPT-0001].

## Отказы

Новых пунктов не открываем.

## Ценность

В среднем 120 заявок в месяц [CALC-0001], разброс от 100 до 140 [CLM-0003].

## Затраты и доходы

Окупаемость неизвестна [Q-0002].

## Допущения и ограничения

Аренда неизменна.
"""

COMMUNICATION_MD = f"""# Коммуникационная стратегия

{STATUS}

## Аудитория и ситуация

Жители района.

## Барьер

Не знают правил.

## Ключевое сообщение

Книги за визит бесплатно [CLM-0001].

## Подтверждение

Правила обмена [CLM-0001].

## Роль каналов

Объявления в пунктах.

## Границы обещаний

Не обещаем новых пунктов.

## Обещания и возможности

| Обещание | Возможность продукта | Основание |
|---|---|---|
| Бесплатный обмен | Обмен в пунктах | [CLM-0001] |

## Почему без изменений и условия пересмотра

Бюджет урезан [OPT-0001].
"""

PLAN_MD = f"""# План проверки

{STATUS}

## Ожидаемое изменение

Заявки не падают.

## Показатель и единица

Заявок в месяц.

## Исходное состояние

База 120 [CALC-0001].

## С чем сравниваем

С прошлым годом.

## Период наблюдения

Три месяца.

## Условие успеха и неуспеха

Не ниже 110 (допущение).

Пороги согласованы [DEC-0003].

## Нежелательные последствия

Очереди в пунктах.
"""


def tree_hash(folder: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(folder.rglob("*")):
        if path.is_file():
            digest.update(path.relative_to(folder).as_posix().encode())
            digest.update(path.read_bytes())
    return digest.hexdigest()


class Base(unittest.TestCase):
    """A finished case on top of the contract example; each test breaks one thing."""

    def setUp(self):
        folder = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(folder.cleanup)
        self.case = Path(folder.name) / "demo-case"
        shutil.copytree(DEMO, self.case)
        shutil.rmtree(self.case / "state" / "checkpoints")
        self.replace("state/actions.jsonl", '"result_refs": ["CKP-0002"]', '"result_refs": []')
        self.write("outputs/options.md", OPTIONS_MD)
        self.write("outputs/product-strategy.md", PRODUCT_MD)
        self.write("outputs/communication-strategy.md", COMMUNICATION_MD)
        self.write("outputs/validation-plan.md", PLAN_MD)
        self.write("outputs/accounting.md", "# Учёт работы\n")
        self.change("Q", self.partial_question)
        self.add_thresholds()

    def write(self, name, text):
        (self.case / name).write_text(text, encoding="utf-8")

    def replace(self, name, old, new):
        text = (self.case / name).read_text(encoding="utf-8")
        self.assertIn(old, text)
        self.write(name, text.replace(old, new))

    def change(self, kind, edit):
        name = {"Q": "research/questions.json", "OPT": "decisions/options.json",
                "DEC": "decisions/decisions.json", "CLM": "evidence/claims.json",
                "CALC": "evidence/calculations.json"}[kind]
        document = json.loads((self.case / name).read_text(encoding="utf-8"))
        edit(document["records"])
        self.write(name, json.dumps(document, ensure_ascii=False, indent=2))

    @staticmethod
    def partial_question(records):
        records[1]["status"] = "partial"

    def add_thresholds(self):
        journal = self.case / "state" / "actions.jsonl"
        lines = [json.loads(line) for line in journal.read_text(encoding="utf-8").splitlines()]
        common = {"format_version": 1, "id": "ACT-0100"}
        at = lines[-1]["at"]
        start = next(line for line in lines if line.get("kind") == "human_input" and line["phase"] == "start")
        end = next(line for line in lines if line["id"] == start["id"] and line["phase"] == "end")
        lines.append(start | common | {"at": at})
        lines.append(end | common | {"at": at, "result_refs": ["DEC-0003"]})
        journal.write_text("".join(json.dumps(line, ensure_ascii=False) + "\n" for line in lines), encoding="utf-8")
        self.change("DEC", lambda records: records.append({
            "id": "DEC-0003", "created_at": at, "topic": "thresholds", "status": "chosen", "proposal": None,
            "choice": {"option_id": None, "user_statement": "Пороги подходят", "chosen_at": at,
                       "action_id": "ACT-0100"}}))

    @staticmethod
    def not_a_choice(line):
        """A human action left without a decision is no longer a choice, or the journal is invalid."""
        if "human" in line and not line["result_refs"]:
            line["human"]["help"] = "answer"

    def drop_threshold_link(self):
        journal = self.case / "state" / "actions.jsonl"
        lines = [json.loads(line) for line in journal.read_text(encoding="utf-8").splitlines()]
        for line in lines:
            if line.get("result_refs") == ["DEC-0003"]:
                line["result_refs"] = []
                self.not_a_choice(line)
        journal.write_text("".join(json.dumps(line, ensure_ascii=False) + "\n" for line in lines), encoding="utf-8")

    def check(self, stage="done", *flags):
        raw = io.BytesIO()
        out = io.TextIOWrapper(raw, encoding="utf-8", newline="\n")
        with contextlib.redirect_stdout(out):
            code = gate.main(["check", str(self.case), stage, *flags])
        out.flush()
        return code, json.loads(raw.getvalue().decode("utf-8").splitlines()[0])

    def codes(self, stage="done", *flags):
        _, report = self.check(stage, *flags)
        return {item["code"] for item in report["problems"]}

    def assert_code(self, code, stage="done", *flags):
        self.assertIn(code, self.codes(stage, *flags))


class Passing(Base):
    def test_finished_case_passes_every_stage(self):
        for stage in gate.STAGES:
            with self.subTest(stage):
                code, report = self.check(stage)
                self.assertEqual((code, report["problems"]), (0, []))

    def test_check_changes_nothing(self):
        before = tree_hash(self.case)
        self.check()
        self.assertEqual(tree_hash(self.case), before)

    def test_report_holds_no_case_text(self):
        self.replace("outputs/product-strategy.md", "Новых пунктов не открываем.", "Секретная фраза 777.")
        _, report = self.check()
        self.assertNotIn("Секретная", json.dumps(report, ensure_ascii=False))

    def test_close_requires_its_own_section(self):
        def choose_close(records):
            records[1]["choice"]["option_id"] = "OPT-0002"
        self.change("DEC", choose_close)
        for name in ("product-strategy", "communication-strategy", "validation-plan"):
            self.replace(f"outputs/{name}.md", "вариант [OPT-0001]", "вариант [OPT-0002]")
        self.replace("outputs/product-strategy.md", "Сохраняем сервис [OPT-0001]", "Закрываем сервис [OPT-0002]")
        self.replace("outputs/communication-strategy.md", "## Почему без изменений и условия пересмотра",
                     "## Другое")
        self.assert_code("OUTPUT_SECTION")
        self.replace("outputs/communication-strategy.md", "## Другое", "## Завершение и последствия")
        self.assertEqual(self.codes(), set())

    def test_develop_needs_no_kind_section(self):
        def choose_develop(records):
            records[1]["choice"]["option_id"] = "OPT-0003"
        self.change("DEC", choose_develop)
        for name in ("product-strategy", "communication-strategy", "validation-plan"):
            self.replace(f"outputs/{name}.md", "вариант [OPT-0001]", "вариант [OPT-0003]")
        self.replace("outputs/product-strategy.md", "Сохраняем сервис [OPT-0001]", "Развиваем [OPT-0003]")
        self.replace("outputs/communication-strategy.md", "## Почему без изменений и условия пересмотра\n\n"
                     "Бюджет урезан [OPT-0001].\n", "")
        self.assertEqual(self.codes(), set())

    def test_not_computable_gap_passes_without_numbers(self):
        self.assertEqual(self.codes("options"), set())


class Research(Base):
    def edit_question(self, **fields):
        self.change("Q", lambda records: records[1].update(fields))

    def test_no_questions(self):
        self.change("Q", lambda records: records.clear())
        self.replace("outputs/options.md", " [Q-0002]", "")
        self.replace("outputs/product-strategy.md", " [Q-0002]", "")
        self.assert_code("RESEARCH_EMPTY", "research")

    def test_open_question(self):
        self.edit_question(status="open")
        self.assert_code("RESEARCH_OPEN", "research")

    def test_answered_without_findings(self):
        self.edit_question(status="answered", findings=[])
        self.assert_code("RESEARCH_NO_FINDING", "research")

    def test_partial_without_gap(self):
        self.edit_question(gap=None)
        self.assert_code("RESEARCH_NO_GAP", "research")

    def test_blank_gap_is_no_gap(self):
        self.edit_question(gap="   ")
        self.assert_code("RESEARCH_NO_GAP", "research")

    def test_contested_finding_needs_a_gap(self):
        self.change("Q", lambda records: records[0].update(findings=["CLM-0008"]))
        self.assert_code("RESEARCH_HIDDEN_CONFLICT", "research")


class Options(Base):
    def edit_option(self, index, **fields):
        self.change("OPT", lambda records: records[index].update(fields))

    def test_too_few(self):
        self.edit_option(1, record_status="withdrawn", status_reason="Снят")
        self.replace("outputs/options.md", "| [OPT-0002] | Закрыть |\n", "")
        self.assertLessEqual({"OPTIONS_TOO_FEW", "OPTIONS_NO_CLOSE"}, self.codes("options"))

    def test_each_kind_is_required(self):
        for index, kind, code in ((0, "develop", "OPTIONS_NO_KEEP"), (1, "develop", "OPTIONS_NO_CLOSE"),
                                  (2, "keep", "OPTIONS_NO_DEVELOP")):
            with self.subTest(code):
                self.edit_option(index, strategy_kind=kind)
                self.assert_code(code, "options")
                self.edit_option(index, strategy_kind=("keep", "close", "develop")[index])

    def test_renamed_action_is_a_duplicate(self):
        self.edit_option(1, action=" не  менять ПУНКТЫ и правила.")
        self.assert_code("OPTIONS_DUPLICATE", "options")

    def test_empty_fields(self):
        for name, empty in (("rejected_actions", []), ("risks", []), ("preconditions", []), ("verification", "  ")):
            with self.subTest(name):
                saved = json.loads((self.case / "decisions/options.json").read_text(encoding="utf-8"))
                self.edit_option(0, **{name: empty})
                self.assert_code("OPTION_FIELD_EMPTY", "options")
                self.write("decisions/options.json", json.dumps(saved, ensure_ascii=False))

    def test_no_proposal(self):
        self.change("DEC", lambda records: records.clear())
        for name in ("product-strategy", "communication-strategy", "validation-plan"):
            (self.case / f"outputs/{name}.md").unlink()
        journal = self.case / "state/actions.jsonl"
        lines = [json.loads(line) for line in journal.read_text(encoding="utf-8").splitlines()]
        for line in lines:
            for name in ("result_refs", "input_refs"):
                if name in line:
                    line[name] = [ref for ref in line[name] if not ref.startswith("DEC")]
            self.not_a_choice(line)
        journal.write_text("".join(json.dumps(line, ensure_ascii=False) + "\n" for line in lines), encoding="utf-8")
        self.assertEqual(self.codes("options"), {"PROPOSAL_MISSING"})

    def test_missing_file_and_section(self):
        self.replace("outputs/options.md", "## Рекомендация", "## Другое")
        self.assert_code("OUTPUT_SECTION", "options")
        (self.case / "outputs/options.md").unlink()
        self.assert_code("OUTPUT_MISSING", "options")

    def test_empty_section(self):
        self.replace("outputs/options.md", "Падение заявок.\n", "")
        self.assert_code("OUTPUT_SECTION", "options")

    def test_option_missing_from_table(self):
        self.replace("outputs/options.md", "| [OPT-0003] | Развивать |\n", "")
        self.assert_code("OPTIONS_TABLE", "options")

    def test_invented_economics(self):
        self.edit_option(0, economics="Выручка 999999 в год.")
        self.assert_code("NUMBER_MISMATCH", "options")

    def test_economics_without_basis(self):
        self.edit_option(0, economics="Выручка 999999 в год.", basis=[])
        self.assert_code("NUMBER_UNLINKED", "options")


class Decision(Base):
    def make_proposed(self, records):
        del records[1]["choice"]
        records[1]["status"] = "proposed"

    def test_recommendation_is_not_a_choice(self):
        self.change("DEC", self.make_proposed)
        self.assert_code("DECISION_PENDING", "decision")

    def test_preliminary_flag_is_refused_on_other_stages(self):
        code, report = self.check("decision", "--preliminary")
        self.assertEqual((code, report["reason"]), (REFUSED, "preliminary_stage"))

    def test_preliminary_passes_without_a_choice_and_never_on_done(self):
        self.change("DEC", self.make_proposed)
        for name in ("product-strategy", "communication-strategy", "validation-plan"):
            self.replace(f"outputs/{name}.md", STATUS, DRAFT)
        self.replace("outputs/product-strategy.md", "Сохраняем сервис [OPT-0001]", "Развиваем [OPT-0003]")
        self.replace("outputs/communication-strategy.md", "## Почему без изменений и условия пересмотра\n\n"
                     "Бюджет урезан [OPT-0001].\n", "")
        self.assertEqual(self.codes("validation_plan", "--preliminary"), set())
        self.assertEqual(self.codes("strategies"), {"DECISION_PENDING", "STATUS_LINE"})
        self.assertLessEqual({"DECISION_PENDING", "PRELIMINARY_LEFT"}, self.codes())

    def test_preliminary_flag_after_a_choice(self):
        self.assert_code("STATUS_LINE", "strategies", "--preliminary")


class Strategies(Base):
    def test_status_line(self):
        self.replace("outputs/product-strategy.md", STATUS, "Статус: готово")
        self.assert_code("STATUS_LINE", "strategies")

    def test_another_choice_in_the_same_decision(self):
        def choose_other(records):
            records[1]["choice"]["option_id"] = "OPT-0003"
        self.change("DEC", choose_other)
        self.assert_code("WRONG_OPTION", "strategies")

    def test_replaced_decision(self):
        self.replace("outputs/product-strategy.md", STATUS, STATUS.replace("DEC-0002", "DEC-0001"))
        _, report = self.check("strategies")
        self.assertEqual(report["problems"][0]["code"], "CASE_INVALID")
        self.assertIn("WITHDRAWN_USED", report["problems"][0]["note"])

    def test_chosen_section_must_name_the_choice(self):
        self.replace("outputs/product-strategy.md", "Сохраняем сервис [OPT-0001]", "Развиваем [OPT-0003]")
        self.assert_code("WRONG_OPTION", "strategies")

    def test_section_without_link(self):
        self.replace("outputs/product-strategy.md", "Окупаемость неизвестна [Q-0002].", "Окупаемость неизвестна.")
        self.assert_code("SECTION_UNLINKED", "strategies")

    def test_assumption_marker_replaces_a_link(self):
        self.replace("outputs/product-strategy.md", "Окупаемость неизвестна [Q-0002].",
                     "Окупаемость неизвестна (допущение).")
        self.assertEqual(self.codes("strategies"), set())

    def test_refusals_need_no_link(self):
        self.assertNotIn("SECTION_UNLINKED", self.codes("strategies"))

    def test_keep_section_required(self):
        self.replace("outputs/communication-strategy.md", "## Почему без изменений и условия пересмотра", "## Иное")
        self.assert_code("OUTPUT_SECTION", "strategies")

    def test_promise_table_is_required(self):
        self.replace("outputs/communication-strategy.md", "| Обещание | Возможность продукта | Основание |",
                     "| Обещание | Основание |")
        self.assert_code("PROMISE_TABLE", "strategies")

    def test_promise_without_basis(self):
        self.replace("outputs/communication-strategy.md", "| [CLM-0001] |", "| нет |")
        self.assert_code("PROMISE_TABLE", "strategies")

    def test_promise_with_no_rows(self):
        self.replace("outputs/communication-strategy.md", "| Бесплатный обмен | Обмен в пунктах | [CLM-0001] |\n", "")
        self.assert_code("PROMISE_TABLE", "strategies")

    def test_promise_on_unconfirmed_claim(self):
        self.replace("outputs/communication-strategy.md", "| [CLM-0001] |", "| [CLM-0004] |")
        self.assert_code("PROMISE_WEAK", "strategies")

    def test_promise_on_another_option(self):
        self.replace("outputs/communication-strategy.md", "| [CLM-0001] |", "| [OPT-0003] |")
        self.assert_code("PROMISE_WEAK", "strategies")

    def test_promise_on_the_plan_only_is_a_warning(self):
        self.replace("outputs/communication-strategy.md", "| [CLM-0001] |", "| [OPT-0001] |")
        code, report = self.check("strategies")
        self.assertEqual(code, 0)
        self.assertIn("PROMISE_PLANNED", {item["code"] for item in report["warnings"]})


class Numbers(Base):
    FILE = "outputs/product-strategy.md"
    LINE = "В среднем 120 заявок в месяц [CALC-0001], разброс от 100 до 140 [CLM-0003]."

    def line(self, text):
        self.replace(self.FILE, self.LINE, text)
        return self.codes("strategies")

    def test_wrong_number(self):
        self.assertIn("NUMBER_MISMATCH", self.line("Заявок 1540 в месяц [CALC-0001]."))

    def test_number_without_link(self):
        self.assertIn("NUMBER_UNLINKED", self.line("Заявок 120 в месяц."))

    def brief(self, words):
        self.write("brief.md", f"# Бриф\n\n## Ограничения\n\nБюджет 900 тысяч\n\n## Слова пользователя\n\n{words}\n")

    def test_number_of_the_user_needs_the_brief_link(self):
        self.brief("«Бюджет на квартал — 600 тысяч рублей»")
        self.assertIn("NUMBER_UNLINKED", self.line("Бюджет 600 тыс. рублей."))
        for text, previous in (("Бюджет 600 тыс. рублей [бриф].", "Бюджет 600 тыс. рублей."),
                               ("Бюджет 600 000 рублей [бриф].", "Бюджет 600 тыс. рублей [бриф].")):
            self.replace(self.FILE, previous, text)
            self.assertFalse({"NUMBER_UNLINKED", "NUMBER_MISMATCH", "SECTION_UNLINKED"} & self.codes("strategies"), text)

    def test_brief_link_backs_only_the_words_of_the_user(self):
        self.brief("«Бюджет на квартал — 600 тысяч рублей»")
        self.assertIn("NUMBER_MISMATCH", self.line("Бюджет 900 тыс. рублей [бриф]."))

    def test_brief_link_compares_the_unit_too(self):
        self.brief("«Хочу 3 варианта. Бюджет — 600 тысяч рублей, команда 5 человек»")
        self.assertIn("NUMBER_MISMATCH", self.line("Конверсия вырастет на 3% [бриф]."))
        self.replace(self.FILE, "Конверсия вырастет на 3% [бриф].", "Выручка 5 млн рублей [бриф].")
        self.assertIn("NUMBER_MISMATCH", self.codes("strategies"))

    def test_brief_link_does_not_back_a_number_next_to_a_record(self):
        self.brief("«Бюджет 600 тысяч»")
        self.assertIn("NUMBER_MISMATCH", self.line("Бюджет 700 тыс. на 120 заявок [бриф] [CALC-0001]."))

    def test_link_to_option_backs_no_number(self):
        self.assertIn("NUMBER_UNLINKED", self.line("Заявок 120 в месяц [OPT-0001]."))

    def test_rounding_scale_and_format(self):
        cases = {"19 %": 19.35, "1 440": 1440, "1,4 млн": 1400000, "35 %": 0.35, "12,5": 12.5, "2 тыс.": 2000}
        for text, value in cases.items():
            with self.subTest(text):
                found = gate.quantities(f"Итог {text} [CALC-0001]")
                self.assertTrue(gate.matches_record(found[0], value))

    def test_rounding_keeps_digits_of_the_text(self):
        self.assertFalse(gate.matches_record(gate.quantities("19,5 %")[0], 19.35))

    def test_sign_comes_from_words(self):
        self.assertTrue(gate.matches_record(gate.quantities("падение на 3")[0], -3))

    def test_assumption_frees_only_its_own_number(self):
        self.assertEqual([q["value"] for q in gate.quantities("с 1440 [CALC-0001] до 1700 (допущение)")], [1440])
        self.assertEqual([q["value"] for q in gate.quantities("до 1700 (допущение) при базе 1440")], [1440])

    def test_ids_years_dates_and_list_numbers_are_ignored(self):
        for text in ("В 2025 году [CLM-0001]", "1. Первый шаг", "К 2026-11-22", "К 22.11.2026", "Сегмент B2B",
                     "Квартал Q3"):
            with self.subTest(text):
                self.assertEqual(gate.quantities(text), [])

    def test_ambiguous_sign(self):
        self.assertIn("NUMBER_AMBIGUOUS", self.line("Итог –5 в месяц [CALC-0001]."))

    def test_headings_are_ignored(self):
        self.replace("outputs/validation-plan.md", "# План проверки", "# План проверки на 777 дней")
        self.assertEqual(self.codes("validation_plan"), set())

    def test_table_row_is_one_line(self):
        self.assertEqual(self.line("| Заявки | 120 | [CALC-0001] |"), set())

    def test_quote_of_a_claim_backs_a_number(self):
        def add_number_to_quote(records):
            records[0]["text"] = "Лимит 7 книг."
        self.change("CLM", add_number_to_quote)
        self.assertEqual(self.line("Лимит 7 книг [CLM-0001]."), set())


class Done(Base):
    def test_accounting_report_is_required(self):
        (self.case / "outputs/accounting.md").unlink()
        self.assertEqual(self.codes(), {"ACCOUNTING_MISSING"})
        self.assertEqual(self.codes("validation_plan"), set())

    def test_thresholds_are_required(self):
        self.drop_threshold_link()
        self.change("DEC", lambda records: records.pop())
        self.replace("outputs/validation-plan.md", "Пороги согласованы [DEC-0003].", "Пороги согласованы.")
        self.assertEqual(self.codes("validation_plan"), set())
        self.assertEqual(self.codes(), {"THRESHOLDS_PENDING"})

    def test_thresholds_without_a_person(self):
        self.drop_threshold_link()
        _, report = self.check()
        self.assertIn("DECISION_NOT_HUMAN", report["problems"][0]["note"])

    def test_plan_must_cite_the_thresholds(self):
        self.replace("outputs/validation-plan.md", "Пороги согласованы [DEC-0003].", "Пороги согласованы.")
        _, report = self.check()
        self.assertEqual([(p["code"], p["file"], p["id"]) for p in report["problems"]],
                         [("THRESHOLDS_NOT_CITED", "outputs/validation-plan.md", "DEC-0003")])
        self.assertEqual(self.codes("validation_plan"), set())

    def test_replaced_thresholds_need_a_new_citation(self):
        def replace(records):
            old = records[2]
            records.append({**old, "id": "DEC-0004", "supersedes": "DEC-0003", "change_reason": "новые пороги"})
            old["status"] = "superseded"
        self.change("DEC", replace)
        self.replace("state/actions.jsonl", '"result_refs": ["DEC-0003"]', '"result_refs": ["DEC-0003", "DEC-0004"]')
        self.assertEqual(self.codes(), {"CASE_INVALID"})
        self.replace("outputs/validation-plan.md", "[DEC-0003]", "")
        self.assertEqual(self.codes(), {"THRESHOLDS_NOT_CITED"})
        self.replace("outputs/validation-plan.md", "согласованы .", "согласованы [DEC-0004].")
        self.assertEqual(self.codes(), set())

    def test_unfinished_action(self):
        journal = self.case / "state/actions.jsonl"
        lines = journal.read_text(encoding="utf-8").splitlines()
        first = json.loads(lines[0]) | {"id": "ACT-0200"}
        lines.append(json.dumps(first, ensure_ascii=False))
        journal.write_text("\n".join(lines) + "\n", encoding="utf-8")
        _, report = self.check()
        self.assertIn("ACTIONS_OPEN", {item["code"] for item in report["problems"]})


class Invocation(Base):
    def test_missing_folder(self):
        self.case = self.case / "нет"
        code, report = self.check()
        self.assertEqual((code, report["reason"]), (REFUSED, "case_missing"))

    def test_unknown_stage(self):
        code, report = self.check("intake")
        self.assertEqual((code, report["reason"]), (REFUSED, "usage"))

    def test_invalid_case_stops_at_the_validator(self):
        self.write("decisions/options.json", "{")
        code, report = self.check()
        self.assertEqual(code, PROBLEMS)
        self.assertEqual([item["code"] for item in report["problems"]], ["CASE_INVALID"])


class ReviewedNumbers(Base):
    def matches(self, text, value):
        return gate.matches_record(gate.quantities(text)[0], value)

    def test_record_may_hold_the_scale_of_the_text(self):
        self.assertTrue(self.matches("1,4 млн", 1.4))

    def test_number_shaped_like_a_year_is_still_a_number(self):
        self.assertEqual(len(gate.quantities("Заявок 2000 в месяц")), 1)
        self.assertEqual(gate.quantities("В 2025 году, 2024–2025 гг. и в 2026 г."), [])

    def test_ranges_share_the_unit_and_the_marker(self):
        self.assertTrue(self.matches("10–15 %", 0.1))
        self.assertTrue(self.matches("1–2 млн", 1000000))
        self.assertTrue(self.matches("от 1 до 2 млн", 1000000))
        self.assertEqual(gate.quantities("5–10 % (допущение)"), [])
        self.assertEqual([q["value"] for q in gate.quantities("10 %–15 %")], [10, 15])

    def test_dates_and_marks_are_not_quantities(self):
        for text in ("до 15 марта", "в 11.2026", "поддержка 24/7", "топ-3 клиента", "решение DEC-0002"):
            with self.subTest(text):
                self.assertEqual(gate.quantities(text), [])

    def test_shares_are_scaled_exactly(self):
        self.assertTrue(self.matches("29 %", 0.285))
        self.assertTrue(self.matches("15 %", 0.145))
        self.assertFalse(self.matches("14 %", 0.145))

    def test_glued_units_and_word_scales(self):
        self.assertEqual(gate.quantities("1540шт")[0]["value"], 1540)
        self.assertTrue(self.matches("15млн", 15000000))
        self.assertTrue(self.matches("1,4 тысячи", 1400))
        self.assertTrue(self.matches("5 миллионов", 5000000))

    def test_written_sign_is_compared(self):
        self.assertFalse(self.matches("Итог -120", 120))
        self.assertTrue(self.matches("рост на 120", -120))

    def test_unreadable_sign_in_a_claim_keeps_its_numbers(self):
        self.change("CLM", lambda records: records[0].update(text="Было –5, стало 7 книг."))
        self.assertIn(7, gate.record_numbers("CLM-0001", {"CLM-0001": {
            "text": "Было –5, стало 7 книг.", "supports": [], "contradicts": []}}))

    def test_only_a_real_status_line_is_skipped(self):
        self.replace("outputs/validation-plan.md", "Заявки не падают.", "Статус: 1540 заявок.")
        self.assert_code("NUMBER_UNLINKED", "validation_plan")

    def test_section_with_only_a_subheading_is_empty(self):
        self.replace("outputs/options.md", "Падение заявок.", "### Подраздел")
        self.assert_code("OUTPUT_SECTION", "options")

    def test_heading_in_a_code_block_is_not_a_section(self):
        self.replace("outputs/options.md", "## Рекомендация", "```\n## Рекомендация\n```")
        self.assert_code("OUTPUT_SECTION", "options")


class ReviewedChoices(Base):
    def test_promise_needs_all_three_cells(self):
        self.replace("outputs/communication-strategy.md", "| Бесплатный обмен | Обмен в пунктах |", "|  |  |")
        self.assert_code("PROMISE_TABLE", "strategies")

    def test_plan_with_a_weak_claim_is_weak(self):
        self.replace("outputs/communication-strategy.md", "| [CLM-0001] |", "| [OPT-0001], [CLM-0004] |")
        self.assert_code("PROMISE_WEAK", "strategies")

    def test_thresholds_before_the_current_choice_do_not_count(self):
        def move(records):
            records[2]["choice"]["chosen_at"] = "2026-11-01T10:00:00+03:00"
        self.change("DEC", move)
        self.assert_code("THRESHOLDS_PENDING")

    def test_invalid_case_names_the_file(self):
        self.replace("outputs/product-strategy.md", STATUS, STATUS.replace("DEC-0002", "DEC-0001"))
        _, report = self.check("strategies")
        self.assertIn("outputs/product-strategy.md", report["problems"][0]["note"])

    def test_gap_of_a_not_computable_finding_passes_to_the_end(self):
        def link(records):
            self.assertIn("CALC-0002", records[1]["findings"])
            self.assertEqual(records[1]["status"], "partial")
        self.change("Q", link)
        self.assertEqual(self.codes(), set())


class Instructions(unittest.TestCase):
    def test_instructions_hold_no_ready_answers(self):
        names = ("мегафон", "яндекс", "сбер", "постомат", "озон", "wildberries")
        for name in ("workflows/research.md", "workflows/options.md", "workflows/strategies.md",
                     "knowledge/research.md", "knowledge/strategy.md"):
            text = (ROOT / "agent" / name).read_text(encoding="utf-8").casefold()
            for company in names:
                self.assertNotIn(company, text)

    def test_stage_procedures_are_connected(self):
        text = (ROOT / "agent/workflows/run-case.md").read_text(encoding="utf-8")
        self.assertNotIn("пока не подключены", text)
        for name in ("research.md", "options.md", "strategies.md"):
            self.assertIn(name, text)
            self.assertTrue((ROOT / "agent/workflows" / name).is_file())

    def test_sections_of_the_contract_match_the_tool(self):
        text = (ROOT / "agent/workflows/strategies.md").read_text(encoding="utf-8")
        text += (ROOT / "agent/workflows/options.md").read_text(encoding="utf-8")
        for sections in gate.SECTIONS.values():
            for name in sections:
                self.assertIn(f"`{name}`", text)
        for name in gate.KIND_SECTIONS.values():
            self.assertIn(f"`{name}`", text)


if __name__ == "__main__":
    unittest.main()
