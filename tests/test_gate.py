import contextlib
import hashlib
import io
import json
from pathlib import Path
import shutil
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))

import gate  # noqa: E402
import revision  # noqa: E402
sys.path.insert(0, str(ROOT / "tests"))
from demo_plain import strip_interviews  # noqa: E402

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

COMMITMENTS_MD = f"""# Решение и обязательства

{STATUS}

## Решение

Сохраняем сервис [OPT-0001], основание [CLM-0002].

## Действия

| Действие | Ответственный | Срок | Показатель | Основание |
|---|---|---|---|---|
| Сохранить сервис | руководитель продукта | 2026-12-01 (допущение) | Заявок в месяц | [OPT-0001] |

## Обязательства

Бесплатный обмен сохраняется [CLM-0001].

## Условия пересмотра

Если подтвердится, что жителей района стало меньше [CLM-0002], пересматриваем.

## Согласование

Обязательства согласованы [DEC-0004].
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
        self.write("outputs/commitments.md", COMMITMENTS_MD)
        self.write("outputs/accounting.md", "# Учёт работы\n")
        self.change("Q", self.partial_question)
        self.drop_demo_revision()
        self.drop_demo_interviews()

    def drop_demo_interviews(self):
        """The example holds one synthetic conversation; the interview tests below add their own."""
        strip_interviews(self.case)

    def drop_demo_revision(self):
        """The example holds one recorded revision; the tests below add their own."""
        (self.case / "outputs" / "revisions.md").unlink()
        shutil.rmtree(self.case / "outputs" / ".history")
        journal = self.case / "state" / "actions.jsonl"
        lines = journal.read_text(encoding="utf-8").splitlines(keepends=True)
        journal.write_text("".join(line for line in lines if '"ACT-0011"' not in line), encoding="utf-8")
        self.change("DEC", lambda records: records.remove(next(r for r in records if r["id"] == "DEC-0005")))

    def write(self, name, text):
        (self.case / name).write_text(text, encoding="utf-8")

    def replace(self, name, old, new):
        text = (self.case / name).read_text(encoding="utf-8")
        self.assertIn(old, text)
        self.write(name, text.replace(old, new))

    def change(self, kind, edit):
        name = {"Q": "research/questions.json", "OPT": "decisions/options.json",
                "DEC": "decisions/decisions.json", "CLM": "evidence/claims.json",
                "CALC": "evidence/calculations.json",
                "SRC": "evidence/sources.json"}[kind]
        document = json.loads((self.case / name).read_text(encoding="utf-8"))
        edit(document["records"])
        self.write(name, json.dumps(document, ensure_ascii=False, indent=2))

    @staticmethod
    def partial_question(records):
        records[1]["status"] = "partial"

    def add_approval(self, topic, dec_id, act_id, at=None, supersedes=None):
        journal = self.case / "state" / "actions.jsonl"
        lines = [json.loads(line) for line in journal.read_text(encoding="utf-8").splitlines()]
        common = {"format_version": 1, "id": act_id}
        at = at or lines[-1]["at"]
        start = next(line for line in lines if line.get("kind") == "human_input" and line["phase"] == "start")
        end = next(line for line in lines if line["id"] == start["id"] and line["phase"] == "end")
        lines.append(start | common | {"at": at})
        lines.append(end | common | {"at": at, "result_refs": [dec_id]})
        journal.write_text("".join(json.dumps(line, ensure_ascii=False) + "\n" for line in lines), encoding="utf-8")
        record = {"id": dec_id, "created_at": at, "topic": topic, "status": "chosen", "proposal": None,
                  "choice": {"option_id": None, "user_statement": "Согласовано", "chosen_at": at,
                             "action_id": act_id}}
        if supersedes:
            record |= {"supersedes": supersedes, "change_reason": "новые данные"}

        def add(records):
            records.append(record)
            for old in records:
                if old["id"] == supersedes:
                    old["status"] = "superseded"
        self.change("DEC", add)

    @staticmethod
    def not_a_choice(line):
        """A human action left without a decision is no longer a choice, or the journal is invalid."""
        if "human" in line and not line["result_refs"]:
            line["human"]["help"] = "answer"

    def drop_threshold_link(self, dec_id="DEC-0003"):
        journal = self.case / "state" / "actions.jsonl"
        lines = [json.loads(line) for line in journal.read_text(encoding="utf-8").splitlines()]
        for line in lines:
            if line.get("result_refs") == [dec_id]:
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


TEAM_HEAD = "# Команда\n\nИсточник: слова пользователя, действие ACT-0004\n\n"
ROSTER = "| Имя | Роль |\n|---|---|\n| Анна К. | аналитик |\n| Игорь С. | разработка |\n"


class Team(Base):
    def team(self, text):
        self.write("team.md", text)

    def owner(self, name):
        text = (self.case / "outputs/commitments.md").read_text(encoding="utf-8")
        row = next(line for line in text.splitlines() if line.startswith("| Сохранить сервис |"))
        cells = row.split("|")
        cells[2] = f" {name} "
        self.write("outputs/commitments.md", text.replace(row, "|".join(cells)))

    def report(self, stage="validation_plan"):
        return self.check(stage)[1]

    def team_codes(self, stage="validation_plan"):
        report = self.report(stage)
        return ({p["code"] for p in report["problems"] if p["code"].startswith("TEAM_")},
                {w["code"] for w in report["warnings"] if w["code"].startswith("TEAM_")})

    def test_no_file_is_a_warning_only(self):
        before = self.codes("validation_plan")
        self.assertEqual(self.team_codes(), (set(), {"TEAM_NOT_ASKED"}))
        self.assertEqual(self.codes("validation_plan"), before)
        self.assertEqual(self.team_codes("done"), (set(), {"TEAM_NOT_ASKED"}))

    def test_no_warning_without_commitments(self):
        (self.case / "outputs/commitments.md").unlink()
        self.assertEqual(self.team_codes(), (set(), set()))

    def test_owners_from_the_roster(self):
        self.team(TEAM_HEAD + ROSTER)
        self.owner("Анна К.")
        self.assertEqual(self.team_codes(), (set(), set()))
        self.assertEqual(self.team_codes("done"), (set(), set()))

    def test_unknown_owner_has_its_line(self):
        self.team(TEAM_HEAD + ROSTER)
        report = self.report()
        found = [(p["code"], p["file"], p.get("line")) for p in report["problems"] if p["code"].startswith("TEAM_")]
        self.assertEqual(found, [("TEAM_UNKNOWN_OWNER", "outputs/commitments.md", 13)])

    def test_several_owners(self):
        self.team(TEAM_HEAD + ROSTER)
        self.owner("Анна К., Игорь С.")
        self.assertEqual(self.team_codes(), (set(), set()))
        self.owner("Анна К., Олег Р.")
        self.assertEqual(self.team_codes()[0], {"TEAM_UNKNOWN_OWNER"})

    def test_owner_must_be_exactly_a_name(self):
        self.team(TEAM_HEAD + ROSTER)
        for cell in ("Анна К. (аналитик)", "анна к.", "Анна"):
            with self.subTest(cell):
                self.owner(cell)
                self.assertEqual(self.team_codes()[0], {"TEAM_UNKNOWN_OWNER"})

    def test_inner_spaces_are_collapsed(self):
        self.team(TEAM_HEAD + ROSTER)
        self.owner("  Анна   К. ")
        self.assertEqual(self.team_codes(), (set(), set()))

    def test_not_named_lets_any_owner_pass(self):
        self.team(TEAM_HEAD + "Состав не назван: ответственные записаны ролями.\n")
        self.assertEqual(self.team_codes(), (set(), set()))

    def test_source_action_is_checked(self):
        for action in ("ACT-0999", "ACT-0001"):
            with self.subTest(action):
                self.team(TEAM_HEAD.replace("ACT-0004", action) + ROSTER)
                self.owner("Анна К.")
                self.assertEqual(self.team_codes()[0], {"TEAM_FORMAT"})

    def test_source_action_without_end(self):
        journal = self.case / "state" / "actions.jsonl"
        lines = [line for line in journal.read_text(encoding="utf-8").splitlines()
                 if not ('"ACT-0004"' in line and '"phase": "end"' in line)]
        journal.write_text("\n".join(lines) + "\n", encoding="utf-8")
        self.team(TEAM_HEAD + ROSTER)
        self.owner("Анна К.")
        self.assertIn("TEAM_FORMAT", self.team_codes()[0])

    def roster_changed(self, at):
        """A later human action names a new team, as after «вместо Анны Олег»."""
        journal = self.case / "state" / "actions.jsonl"
        lines = [json.loads(line) for line in journal.read_text(encoding="utf-8").splitlines()]
        start = next(line for line in lines if line["id"] == "ACT-0004" and line["phase"] == "start")
        end = next(line for line in lines if line["id"] == "ACT-0004" and line["phase"] == "end")
        lines.append(start | {"id": "ACT-0050", "at": at})
        lines.append(end | {"id": "ACT-0050", "at": at, "result_refs": []})
        self.not_a_choice(lines[-1])
        journal.write_text("".join(json.dumps(line, ensure_ascii=False) + "\n" for line in lines), encoding="utf-8")
        self.team(TEAM_HEAD.replace("ACT-0004", "ACT-0050") + ROSTER)
        self.owner("Анна К.")

    def test_roster_changed_after_consent_needs_new_consent(self):
        self.roster_changed("2026-11-04T09:00:00+03:00")
        self.assertIn("COMMITMENTS_PENDING", self.codes("done"))
        self.add_approval("commitments", "DEC-0009", "ACT-0051", at="2026-11-04T10:00:00+03:00",
                          supersedes="DEC-0004")
        self.replace("outputs/commitments.md", "[DEC-0004]", "[DEC-0009]")
        self.assertNotIn("COMMITMENTS_PENDING", self.codes("done"))

    def test_roster_named_before_consent_keeps_it(self):
        self.roster_changed("2026-11-03T11:00:00+03:00")
        self.assertNotIn("COMMITMENTS_PENDING", self.codes("done"))

    def test_malformed_table_is_only_a_format_problem(self):
        self.team(TEAM_HEAD + "| Имя | Должность |\n|---|---|\n| Анна К. | аналитик |\n")
        self.assertEqual(self.team_codes(), ({"TEAM_FORMAT"}, set()))


class ContractExample(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(folder.cleanup)
        self.case = Path(folder.name) / "demo-case"
        shutil.copytree(DEMO, self.case)

    def run_tool(self, module, *args):
        raw = io.BytesIO()
        out = io.TextIOWrapper(raw, encoding="utf-8", newline="\n")
        with contextlib.redirect_stdout(out):
            code = module.main([*args])
        out.flush()
        return code, json.loads(raw.getvalue().decode("utf-8").splitlines()[0])

    def test_contract_example_passes_done(self):
        code, report = self.run_tool(gate, "check", str(self.case), "done")
        self.assertEqual((code, report["problems"], [w["code"] for w in report["warnings"]]), (0, [], ["BASIS_UNCONFIRMED", "TEAM_NOT_ASKED"]))

    def test_contract_example_has_a_recorded_revision(self):
        code, report = self.run_tool(revision, "affected", str(self.case), "CLM-0001")
        self.assertEqual(code, 0)
        self.assertEqual(report["affected"], {"outputs/communication-strategy.md": ["Ключевое сообщение"]})
        self.assertEqual(report["reapproval"], [])


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
        for name in ("product-strategy", "communication-strategy", "validation-plan", "commitments"):
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
        for name in ("product-strategy", "communication-strategy", "validation-plan", "commitments"):
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
        for name in ("product-strategy", "communication-strategy", "validation-plan", "commitments"):
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
        for name in ("product-strategy", "communication-strategy", "validation-plan", "commitments"):
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
        self.change("DEC", lambda records: records.remove(next(r for r in records if r["id"] == "DEC-0003")))
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
            records.append({**old, "id": "DEC-0005", "supersedes": "DEC-0003", "change_reason": "новые пороги"})
            old["status"] = "superseded"
        self.change("DEC", replace)
        self.replace("state/actions.jsonl", '"result_refs": ["DEC-0003"]', '"result_refs": ["DEC-0003", "DEC-0005"]')
        self.assertEqual(self.codes(), {"CASE_INVALID", "WITHDRAWN_USED"})
        self.replace("outputs/validation-plan.md", "[DEC-0003]", "")
        self.assertEqual(self.codes(), {"THRESHOLDS_NOT_CITED"})
        self.replace("outputs/validation-plan.md", "согласованы .", "согласованы [DEC-0005].")
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
        codes = [item["code"] for item in report["problems"]]
        self.assertEqual(codes[0], "CASE_INVALID")
        self.assertGreater(len(codes), 1)

    def test_validator_errors_over_the_output_limit_are_counted_not_dropped(self):
        for name in ("decisions/options.json", "decisions/decisions.json", "evidence/claims.json",
                     "evidence/sources.json", "research/questions.json"):
            self.write(name, "{")
        _, full = self.check()
        errors = [p for p in full["problems"] if p["code"] != "CASE_INVALID"]
        self.assertGreater(len(errors), 1)
        with mock.patch.object(gate, "load_limits", return_value={"max_output_chars": 400}):
            code, report = self.check()
        problems = report["problems"]
        self.assertEqual((code, report["passed"], problems[0]["code"]), (PROBLEMS, False, "CASE_INVALID"))
        more = problems[-1]
        self.assertEqual(more["code"], "VALIDATOR_ERRORS_NOT_SHOWN")
        shown = len(problems) - 2
        self.assertLess(shown, len(errors))
        self.assertIn(str(len(errors) - shown), more["note"])
        self.assertIn("validate_project.py", more["note"])
        self.assertEqual([p["code"] for p in problems[1:-1]], [p["code"] for p in errors[:shown]])

    def test_validator_errors_come_as_separate_problems(self):
        self.replace("outputs/product-strategy.md", STATUS, STATUS.replace("DEC-0002", "DEC-0001"))
        _, report = self.check("strategies")
        first, *rest = report["problems"]
        self.assertEqual(first["code"], "CASE_INVALID")
        withdrawn = [p for p in rest if p["code"] == "WITHDRAWN_USED"]
        self.assertEqual(len(withdrawn), 1)
        self.assertEqual(withdrawn[0]["file"], "outputs/product-strategy.md")
        self.assertTrue(withdrawn[0]["note"])
        self.assertIn("id", withdrawn[0])


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


REVISION_AT = "2026-11-05T10:00:00+03:00"
LATER_AT = "2026-11-08T10:00:00+03:00"


def revisions_md(*entries):
    return "# Пересмотры\n" + "".join(entries)


def revision_entry(number=1, choice="[DEC-0005]", touched="validation-plan.md «Условие успеха и неуспеха»",
                   snapshot=None):
    snapshot = snapshot or f"outputs/.history/{number}/"
    return f"""
## Пересмотр {number} — 2026-11-05

- Новые данные: [SRC-0001]
- Изменилось: [CLM-0001]
- Условия пересмотра: не сработало ни одно
- Затронуто: {touched}
- Не затронуто: product-strategy.md
- Выбор: {choice}
- Прежние версии: {snapshot}
"""


class Commitments(Base):
    def test_required_at_done_only(self):
        (self.case / "outputs/commitments.md").unlink()
        self.assertEqual(self.codes(), {"OUTPUT_MISSING"})
        self.assertEqual(self.codes("validation_plan"), set())

    def test_old_finished_case(self):
        (self.case / "outputs/commitments.md").unlink()
        self.drop_threshold_link("DEC-0004")
        self.change("DEC", lambda records: records.remove(next(r for r in records if r["id"] == "DEC-0004")))
        self.assertEqual(self.codes(), {"OUTPUT_MISSING", "COMMITMENTS_PENDING"})

    def test_section_missing(self):
        self.replace("outputs/commitments.md", "## Обязательства", "## Другое")
        self.assert_code("OUTPUT_SECTION")

    def test_table_header(self):
        self.replace("outputs/commitments.md", "| Срок |", "| Дата |")
        self.assert_code("COMMITMENTS_TABLE")

    def test_table_without_rows(self):
        self.replace("outputs/commitments.md", "| Сохранить сервис | руководитель продукта | 2026-12-01 (допущение) |"
                     " Заявок в месяц | [OPT-0001] |\n", "")
        self.assert_code("COMMITMENTS_TABLE")

    def test_row_without_basis(self):
        self.replace("outputs/commitments.md", "| Заявок в месяц | [OPT-0001] |", "| Заявок в месяц | |")
        _, report = self.check()
        self.assertEqual([(p["code"], p.get("line")) for p in report["problems"]], [("COMMITMENTS_TABLE", 13)])

    def test_row_with_an_empty_cell(self):
        self.replace("outputs/commitments.md", "| руководитель продукта |", "| |")
        self.assert_code("COMMITMENTS_TABLE")

    def test_review_without_link(self):
        self.replace("outputs/commitments.md", "стало меньше [CLM-0002],", "стало меньше (допущение),")
        _, report = self.check()
        self.assertEqual([(p["code"], p.get("line")) for p in report["problems"]], [("REVIEW_UNLINKED", 21)])

    def test_number_without_basis(self):
        self.replace("outputs/commitments.md", "сохраняется [CLM-0001].", "сохраняется 5 лет.")
        self.assert_code("NUMBER_UNLINKED")

    def test_decision_before_direction(self):
        def move(records):
            next(r for r in records if r["id"] == "DEC-0004")["choice"]["chosen_at"] = "2026-11-01T10:00:00+03:00"
        self.change("DEC", move)
        self.assert_code("COMMITMENTS_PENDING")

    def test_not_cited(self):
        self.replace("outputs/commitments.md", "согласованы [DEC-0004].", "согласованы.")
        self.assertEqual(self.codes(), {"COMMITMENTS_NOT_CITED"})


class Revisions(Base):
    def add_revision(self, at=REVISION_AT):
        self.add_approval("revision", "DEC-0005", "ACT-0102", at=at)

    def snapshot(self, number=1):
        folder = self.case / "outputs" / ".history" / str(number)
        folder.mkdir(parents=True)
        (folder / "validation-plan.md").write_text("# Копия\n", encoding="utf-8")

    def log(self, *entries):
        self.write("outputs/revisions.md", revisions_md(*entries))

    def recorded(self, **fields):
        self.add_revision()
        self.snapshot()
        self.log(revision_entry(**fields))

    def reapprove(self, topic, dec_id, act_id, old):
        self.add_approval(topic, dec_id, act_id, at=LATER_AT, supersedes=old)

    def test_no_revision_no_file_needed(self):
        self.assertEqual(self.codes(), set())

    def test_revision_without_entry(self):
        self.add_revision()
        self.assertEqual(self.codes(), {"REVISION_UNRECORDED"})
        self.log(revision_entry(choice="изменений нет", snapshot="нет"))
        self.assertEqual(self.codes(), {"REVISION_UNRECORDED"})

    def test_entry_without_snapshot(self):
        self.add_revision()
        self.log(revision_entry(touched="product-strategy.md"))
        self.assertEqual(self.codes(), {"REVISION_NO_SNAPSHOT"})
        (self.case / "outputs/.history/1").mkdir(parents=True)
        self.assertEqual(self.codes(), {"REVISION_NO_SNAPSHOT"})
        (self.case / "outputs/.history/1/product-strategy.md").write_text("# Копия\n", encoding="utf-8")
        self.assertEqual(self.codes(), set())

    def test_unchanged_entry_needs_no_snapshot(self):
        self.log(revision_entry(choice="изменений нет", snapshot="нет"))
        self.assertEqual(self.codes(), set())
        self.add_revision()
        self.snapshot(2)
        self.log(revision_entry(choice="изменений нет", snapshot="нет"),
                 revision_entry(2, touched="product-strategy.md"))
        self.assertEqual(self.codes(), set())

    def test_duplicate_choice(self):
        self.add_revision()
        self.snapshot()
        self.snapshot(2)
        self.log(revision_entry(touched="product-strategy.md"), revision_entry(2, touched="product-strategy.md"))
        self.assertEqual(self.codes(), {"REVISION_UNRECORDED"})

    def test_log_problems_surface(self):
        self.recorded(touched="product-strategy.md")
        self.replace("outputs/revisions.md", "## Пересмотр 1 —", "## Пересмотр 3 —")
        _, report = self.check()
        self.assertIn(("REVISION_NUMBER", "outputs/revisions.md", 3),
                      [(p["code"], p["file"], p.get("line")) for p in report["problems"]])

    def test_reapproval_after_plan_touched(self):
        self.recorded()
        _, report = self.check()
        self.assertEqual([(p["code"], p["id"]) for p in report["problems"]], [("REAPPROVAL_PENDING", "DEC-0003")])
        self.reapprove("thresholds", "DEC-0006", "ACT-0103", "DEC-0003")
        self.replace("outputs/validation-plan.md", "[DEC-0003]", "[DEC-0006]")
        self.assertEqual(self.codes(), set())

    def test_reapproval_after_commitments_touched(self):
        self.recorded(touched="commitments.md «Действия»")
        _, report = self.check()
        self.assertEqual([(p["code"], p["id"]) for p in report["problems"]], [("REAPPROVAL_PENDING", "DEC-0004")])
        self.reapprove("commitments", "DEC-0006", "ACT-0103", "DEC-0004")
        self.replace("outputs/commitments.md", "[DEC-0004]", "[DEC-0006]")
        self.assertEqual(self.codes(), set())

    def test_second_revision_supersedes_the_first(self):
        self.add_revision()
        self.snapshot()
        self.snapshot(2)
        self.add_approval("revision", "DEC-0006", "ACT-0103", at=LATER_AT, supersedes="DEC-0005")
        self.log(revision_entry(touched="product-strategy.md"),
                 revision_entry(2, choice="[DEC-0006]", touched="product-strategy.md"))
        _, report = self.check()
        self.assertEqual(report["problems"], [])

    def test_log_may_name_withdrawn_records(self):
        self.recorded(touched="product-strategy.md")
        self.replace("outputs/revisions.md", "- Изменилось: [CLM-0001]",
                     "- Изменилось: [CLM-0001] [CLM-0005] [OPT-0002]")
        self.assertEqual(self.codes(), set())

    def test_log_links_must_still_exist(self):
        self.recorded(touched="product-strategy.md")
        self.replace("outputs/revisions.md", "- Изменилось: [CLM-0001]", "- Изменилось: [CLM-0999]")
        self.assertIn("CASE_INVALID", self.codes())

    def test_log_is_checked_without_revision_decisions(self):
        self.write("outputs/revisions.md", "# Не то")
        self.assertIn("REVISION_FORMAT", self.codes())

    def test_untouched_plan_needs_no_reapproval(self):
        self.recorded(touched="product-strategy.md «Ценность»")
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

    def test_revision_procedure_is_connected(self):
        run_case = (ROOT / "agent/workflows/run-case.md").read_text(encoding="utf-8")
        self.assertIn("revision.md", run_case)
        path = ROOT / "agent/workflows/revision.md"
        self.assertTrue(path.is_file())
        text = path.read_text(encoding="utf-8")
        for word in ("affected", "snapshot", "revisions.md", ".history"):
            self.assertIn(word, text)

    def test_revision_procedure_holds_no_ready_answers(self):
        names = ("мегафон", "яндекс", "сбер", "постомат", "озон", "wildberries")
        for name in ("workflows/revision.md", "workflows/strategies.md", "workflows/checkpoint.md"):
            text = (ROOT / "agent" / name).read_text(encoding="utf-8").casefold()
            for company in names:
                self.assertNotIn(company, text)

    def test_sections_of_the_contract_match_the_tool(self):
        text = (ROOT / "agent/workflows/strategies.md").read_text(encoding="utf-8")
        text += (ROOT / "agent/workflows/options.md").read_text(encoding="utf-8")
        for sections in gate.SECTIONS.values():
            for name in sections:
                self.assertIn(f"`{name}`", text)
        for name in gate.KIND_SECTIONS.values():
            self.assertIn(f"`{name}`", text)



FORM = """
## Форма результата
Источник: SRC-0001, с. 1–2
0. Резюме
   - главный вывод в начале
1. Клиент и его проблема
   - один восстановленный случай
   - сравнение с альтернативой
2. Предложение и путь к клиенту
   - таблица прогноза

"""
BRIEF_HEAD = "# Бриф\n\n## Вопрос\n\nЧто делать?\n\n"
DELIVERABLE_MD = f"""# Документ для заказчика

{STATUS}

## Резюме

Сервис сохраняем [OPT-0001].

## Клиент и его проблема

Заявок в среднем 120 в месяц [CALC-0001].

## Предложение и путь к клиенту

Бесплатный обмен в пунктах [CLM-0001].
"""


class Deliverable(Base):
    """The form of the result: checked only when the brief sets one."""

    def set_brief(self, text):
        self.write("brief.md", BRIEF_HEAD + text)

    def setUp(self):
        super().setUp()
        self.set_brief(FORM)
        self.write("outputs/deliverable.md", DELIVERABLE_MD)

    def test_complete_document_passes(self):
        self.assertEqual(self.codes(), set())

    def test_case_without_a_form_is_checked_as_before(self):
        self.set_brief("")
        (self.case / "outputs/deliverable.md").unlink()
        self.assertEqual(self.codes(), set())

    def test_document_is_not_required_before_done(self):
        (self.case / "outputs/deliverable.md").unlink()
        for stage in gate.STAGES[:-1]:
            self.assertEqual(self.codes(stage), set(), stage)

    def test_unreadable_form_is_caught_from_research_on(self):
        self.set_brief("## Форма результата\n1) Клиент\n")
        for stage in gate.STAGES:
            with self.subTest(stage):
                _, report = self.check(stage)
                self.assertEqual([p["code"] for p in report["problems"]], ["FORM_UNREADABLE"])

    def test_two_form_sections_are_unreadable(self):
        self.set_brief(FORM + "## Форма результата\n1. Клиент и его проблема\n")
        _, report = self.check("research")
        self.assertEqual([p["code"] for p in report["problems"]], ["FORM_UNREADABLE"])
        self.assertIn("twice", report["problems"][0]["note"])

    def test_repeated_part_in_the_document(self):
        self.write("outputs/deliverable.md", DELIVERABLE_MD + "\n## Резюме\n\nЕщё раз.\n")
        _, report = self.check()
        self.assertEqual([(p["code"], "more than once" in p["note"]) for p in report["problems"]],
                         [("DELIVERABLE_PART_ORDER", True)])

    def test_document_is_required(self):
        (self.case / "outputs/deliverable.md").unlink()
        self.assertEqual(self.codes(), {"DELIVERABLE_MISSING"})

    def test_missing_part_is_named(self):
        self.replace("outputs/deliverable.md", "## Клиент и его проблема", "## Другое")
        _, report = self.check()
        self.assertEqual([(p["code"], p["note"]) for p in report["problems"]],
                         [("DELIVERABLE_PART_MISSING", "no section «Клиент и его проблема»")])

    def test_names_match_exactly_after_trimming(self):
        self.replace("outputs/deliverable.md", "## Резюме", "## резюме")
        self.assertEqual(self.codes(), {"DELIVERABLE_PART_MISSING"})
        self.replace("outputs/deliverable.md", "## резюме", "## Резюме   ")
        self.assertEqual(self.codes(), set())

    def test_parts_out_of_order(self):
        self.write("outputs/deliverable.md", DELIVERABLE_MD.replace("## Резюме", "## Временно")
                   .replace("## Клиент и его проблема", "## Резюме")
                   .replace("## Временно", "## Клиент и его проблема"))
        self.assertEqual(self.codes(), {"DELIVERABLE_PART_ORDER"})

    def test_extra_sections_and_subheadings_are_free(self):
        self.replace("outputs/deliverable.md", "## Клиент и его проблема\n\n",
                     "## Приложение\n\nСправка.\n\n## Клиент и его проблема\n\n### Случай\n\n")
        self.assertEqual(self.codes(), set())

    def test_empty_part(self):
        self.replace("outputs/deliverable.md", "Заявок в среднем 120 в месяц [CALC-0001].", "### Только заголовок")
        self.assertEqual(self.codes(), {"DELIVERABLE_PART_EMPTY"})

    def test_status_line_is_required(self):
        self.replace("outputs/deliverable.md", STATUS + "\n", "")
        self.assertEqual(self.codes(), {"STATUS_LINE"})

    def test_wrong_number_is_caught(self):
        self.replace("outputs/deliverable.md", "120 в месяц", "130 в месяц")
        self.assertEqual(self.codes(), {"NUMBER_MISMATCH"})

    def test_false_precision_is_caught(self):
        self.replace("outputs/deliverable.md", "120 в месяц", "120,0000 в месяц")
        self.assertEqual(self.codes(), {"NUMBER_PRECISION"})

    def test_false_precision_of_an_assumption_is_caught(self):
        self.write("outputs/deliverable.md", DELIVERABLE_MD + "\nДоля отклика 12,3456 % (допущение).\n")
        self.assertEqual(self.codes(), {"NUMBER_PRECISION"})

    def test_three_digits_after_the_comma_pass(self):
        self.write("outputs/deliverable.md", DELIVERABLE_MD + "\nДоля отклика 12,35 % (допущение), сумма 0,648 млн руб. (допущение).\n")
        self.assertEqual(self.codes(), set())

    def test_number_needs_a_link(self):
        self.replace("outputs/deliverable.md", "120 в месяц [CALC-0001]", "120 в месяц")
        self.assertEqual(self.codes(), {"NUMBER_UNLINKED"})

    def test_unknown_link_is_caught(self):
        self.replace("outputs/deliverable.md", "[CLM-0001]", "[CLM-0099]")
        self.assertIn("OUTPUT_LINK", self.codes())

    def test_unreadable_form(self):
        cases = {
            "bracket numbering": "1) Клиент\n2) Предложение\n",
            "number out of order": "1. Клиент\n3. Предложение\n",
            "zero is not the summary": "0. Вводная часть\n1. Клиент\n",
            "starts from two": "2. Клиент\n3. Предложение\n",
            "requirement before a part": "   - главное\n1. Клиент\n",
            "no parts": "Источник: SRC-0001\n",
            "empty section": "",
            "name on the next line": "1. Клиент\nи его проблема\n",
            "sub-numbering": "1. Клиент\n1.1 Случай\n",
            "source not first": "1. Клиент\nИсточник: SRC-0001\n",
            "requirement without indent": "1. Клиент\n- главное\n",
            "part without a name": "1. \n",
            "part name repeats": "1. Клиент\n2. Клиент\n",
        }
        for name, body in cases.items():
            with self.subTest(name):
                self.set_brief("## Форма результата\n" + body)
                _, report = self.check()
                self.assertEqual({p["code"] for p in report["problems"]}, {"FORM_UNREADABLE"})
                self.assertTrue(all(p["file"] == "brief.md" for p in report["problems"]))

    def test_unreadable_form_points_at_the_line(self):
        self.write("brief.md", "# Бриф\n\n## Форма результата\n1. Клиент\n3. Предложение\n")
        _, report = self.check()
        self.assertEqual([p["line"] for p in report["problems"]], [5])

    def test_unreadable_form_does_not_ask_for_the_document(self):
        self.set_brief("## Форма результата\n1) Клиент\n")
        (self.case / "outputs/deliverable.md").unlink()
        self.assertEqual(self.codes(), {"FORM_UNREADABLE"})

    def test_form_without_the_summary_may_start_from_one(self):
        self.set_brief("## Форма результата\n1. Клиент и его проблема\n2. Предложение и путь к клиенту\n")
        self.replace("outputs/deliverable.md", "## Резюме\n\nСервис сохраняем [OPT-0001].\n\n", "")
        self.assertEqual(self.codes(), set())

    def test_the_form_ends_at_the_next_section(self):
        self.set_brief(FORM + "## Допущения агента\n\nКакой-то текст без частей.\n")
        self.assertEqual(self.codes(), set())


METHOD_AT = "2026-11-08T10:00:00+03:00"
LATER_AT = "2026-11-08T12:00:00+03:00"
GUIDE_MD = """# Гайд интервью

Статус: способ выбран [DEC-0006]

## Кого спрашивать

Жители района [CLM-0002].

## Q-0002. Окупаемость

Что нужно узнать: будут ли платить [Q-0002].

## Согласие и данные

Перед интервью получить согласие, имён не хранить.
"""


class Interviews(Base):
    """Interview method, guide, session limits and marks of synthetic support."""

    def add_synthetic(self, number=1, created_at=LATER_AT, questions=2, status="active"):
        src_id = f"SRC-{4 + number:04d}"
        path = f"sources/interviews/synthetic-r{number}-1.md"
        text = "# Беседа\n" + "".join(f"\n## Вопрос {n}\n\nОтвет персонажа.\n" for n in range(1, questions + 1))
        data = text.encode("utf-8")
        (self.case / path).parent.mkdir(parents=True, exist_ok=True)
        (self.case / path).write_bytes(data)
        record = {
            "id": src_id, "created_at": created_at, "record_status": status, "type": "md",
            "title": "Беседа с персонажем", "location": {"path": path}, "origin": "synthetic", "allowed": True,
            "retrieved_at": created_at, "sha256": hashlib.sha256(data).hexdigest(),
            "extraction": {"status": "ok", "complete": True, "limits": "Прочитан целиком."},
        }
        if status == "withdrawn":
            record["status_reason"] = "Заменена повтором."
        self.change("SRC", lambda records: records.append(record))
        block = {"n": 1, "locator": {"kind": "paragraph", "index": 1}, "text": "## Вопрос 1", "heading": "Вопрос 1"}
        (self.case / "sources" / "extracted" / f"{src_id}.jsonl").write_text(
            json.dumps(block, ensure_ascii=False) + "\n", encoding="utf-8")
        return src_id

    def assert_invalid_with(self, code, stage):
        """The gate stops on validator errors and names their codes in one CASE_INVALID note."""
        _, report = self.check(stage)
        notes = " ".join(item["note"] for item in report["problems"] if item["code"] == "CASE_INVALID")
        self.assertIn(code, notes)

    def add_method(self, at=METHOD_AT):
        self.add_approval("interview_method", "DEC-0006", "ACT-0100", at=at)

    def add_budget(self, at="2026-11-08T13:00:00+03:00"):
        self.add_approval("budget", "DEC-0007", "ACT-0101", at=at)

    def add_hypothesis(self, src_id="SRC-0005", claim_id="CLM-0100", derived_from=()):
        claim = {
            "id": claim_id, "created_at": LATER_AT, "text": "Клиенты сочли бы это дорогим.",
            "kind": "inference" if derived_from else "hypothesis", "status": "unconfirmed",
            "supports": [] if derived_from else [
                {"source_id": src_id, "locator": {"kind": "section", "heading": "Вопрос 1"}, "note": "Реплика"}],
            "contradicts": [], "derived_from": list(derived_from), "limits": "Синтетическая беседа.",
        }
        self.change("CLM", lambda records: records.append(claim))

    def with_synthetic_claim(self):
        self.add_method()
        self.add_synthetic()
        self.add_hypothesis()

    def product_with(self, line):
        self.replace("outputs/product-strategy.md", "Покупка книг.", f"Покупка книг. {line}")

    def write_guide(self, text=GUIDE_MD):
        self.write("outputs/interview-guide.md", text)

    def test_no_interviews_no_new_problems(self):
        for stage in gate.STAGES:
            with self.subTest(stage):
                self.assertEqual(self.codes(stage), set())

    def test_synthetic_source_without_method(self):
        self.add_synthetic()
        self.assert_code("INTERVIEW_METHOD_MISSING", "research")

    def test_guide_without_method(self):
        self.write_guide(GUIDE_MD.replace("[DEC-0006]", "[DEC-0002]"))
        self.assert_code("INTERVIEW_METHOD_MISSING", "research")

    def test_synthetic_source_before_method(self):
        self.add_method()
        self.add_synthetic(created_at="2026-11-08T09:00:00+03:00")
        self.assert_code("INTERVIEW_METHOD_MISSING", "research")

    def test_synthetic_source_after_method_ok(self):
        self.add_method()
        self.add_synthetic()
        self.assertEqual(self.codes("research"), set())

    def test_guide_status_not_method(self):
        self.add_method()
        self.write_guide(GUIDE_MD.replace("[DEC-0006]", "[DEC-0002]"))
        self.assert_code("INTERVIEW_GUIDE", "research")

    def test_guide_status_missing(self):
        self.add_method()
        self.write_guide(GUIDE_MD.replace("Статус: способ выбран [DEC-0006]", "Черновик."))
        self.assert_code("INTERVIEW_GUIDE", "research")

    def test_guide_unknown_question(self):
        self.add_method()
        self.write_guide(GUIDE_MD.replace("Q-0002. Окупаемость", "Q-0099. Что-то"))
        self.assert_code("INTERVIEW_GUIDE", "research")

    def test_guide_question_answered(self):
        self.add_method()
        self.write_guide(GUIDE_MD.replace("Q-0002. Окупаемость", "Q-0001. Правила"))
        self.assert_code("INTERVIEW_GUIDE", "research")

    def test_guide_missing_section(self):
        self.add_method()
        self.write_guide(GUIDE_MD.replace("Согласие и данные", "Прочее"))
        self.assert_code("OUTPUT_SECTION", "research")

    def test_guide_number_needs_link(self):
        self.add_method()
        self.write_guide(GUIDE_MD.replace("будут ли платить", "будут ли платить 500 рублей"))
        self.assert_code("NUMBER_UNLINKED", "research")

    def test_guide_ok(self):
        self.add_method()
        self.write_guide()
        self.assertEqual(self.codes("research"), set())

    def test_fourth_session_needs_budget(self):
        self.add_method()
        for number in range(1, 5):
            self.add_synthetic(number)
        self.assert_code("INTERVIEW_LIMIT", "research")

    def test_fourth_session_with_budget_ok(self):
        self.add_method()
        for number in range(1, 5):
            self.add_synthetic(number)
        self.add_budget()
        self.assertEqual(self.codes("research"), set())

    def test_budget_before_method_does_not_count(self):
        self.add_approval("budget", "DEC-0007", "ACT-0101", at="2026-11-08T09:00:00+03:00")
        self.add_method()
        for number in range(1, 5):
            self.add_synthetic(number)
        self.assert_code("INTERVIEW_LIMIT", "research")

    def test_withdrawn_session_not_counted(self):
        self.add_method()
        for number in range(1, 4):
            self.add_synthetic(number)
        self.add_synthetic(4, status="withdrawn")
        self.assertEqual(self.codes("research"), set())

    def test_eleven_questions(self):
        self.add_method()
        self.add_synthetic(questions=11)
        self.assert_code("INTERVIEW_LIMIT", "research")

    def test_ten_questions_ok(self):
        self.add_method()
        self.add_synthetic(questions=10)
        self.assertEqual(self.codes("research"), set())

    def test_unmarked_synthetic_reference(self):
        self.with_synthetic_claim()
        self.product_with("Возражение: дорого [CLM-0100].")
        self.assert_code("SYNTHETIC_UNMARKED", "strategies")

    def test_marked_reference_ok(self):
        self.with_synthetic_claim()
        self.product_with("Возражение: дорого [CLM-0100] (синтетика).")
        self.assertEqual(self.codes("strategies"), set())

    def test_mark_required_in_table_cell(self):
        self.with_synthetic_claim()
        self.replace("outputs/communication-strategy.md", "| Бесплатный обмен | Обмен в пунктах | [CLM-0001] |",
                     "| Бесплатный обмен | Обмен в пунктах | [CLM-0001], [CLM-0100] |")
        self.assert_code("SYNTHETIC_UNMARKED", "strategies")

    def test_mark_in_table_cell_ok(self):
        self.with_synthetic_claim()
        self.replace("outputs/communication-strategy.md", "| Бесплатный обмен | Обмен в пунктах | [CLM-0001] |",
                     "| Бесплатный обмен | Обмен в пунктах | [CLM-0001] [CLM-0100] (синтетика) |")
        self.assertNotIn("SYNTHETIC_UNMARKED", self.codes("strategies"))

    def test_transitive_inference_needs_mark(self):
        self.with_synthetic_claim()
        self.add_hypothesis(claim_id="CLM-0101", derived_from=["CLM-0100"])
        self.product_with("Вывод: дорого [CLM-0101].")
        self.assert_code("SYNTHETIC_UNMARKED", "strategies")
        self.replace("outputs/product-strategy.md", "[CLM-0101].", "[CLM-0101] (синтетика).")
        self.assertEqual(self.codes("strategies"), set())

    def test_mark_required_in_commitments(self):
        self.with_synthetic_claim()
        self.replace("outputs/commitments.md", "Бесплатный обмен сохраняется [CLM-0001].",
                     "Бесплатный обмен сохраняется [CLM-0001]. Риск: дорого [CLM-0100].")
        self.assert_code("SYNTHETIC_UNMARKED", "done")

    def test_option_reference_needs_no_mark(self):
        self.with_synthetic_claim()
        self.product_with("Вариант [OPT-0001].")
        self.assertEqual(self.codes("strategies"), set())

    def test_non_synthetic_reference_needs_no_mark(self):
        self.with_synthetic_claim()
        self.product_with("Обмен бесплатный [CLM-0001].")
        self.assertEqual(self.codes("strategies"), set())

    def withdraw_synthetic(self, src_id="SRC-0005"):
        def mark(records):
            for record in records:
                if record["id"] == src_id:
                    record["record_status"] = "withdrawn"
                    record["status_reason"] = "Заменена повтором."
        self.change("SRC", mark)

    def test_withdrawn_synthetic_still_needs_mark(self):
        self.with_synthetic_claim()
        self.withdraw_synthetic()
        self.product_with("Возражение: дорого [CLM-0100].")
        self.assert_code("SYNTHETIC_UNMARKED", "strategies")
        self.replace("outputs/product-strategy.md", "[CLM-0100].", "[CLM-0100] (синтетика).")
        self.assertNotIn("SYNTHETIC_UNMARKED", self.codes("strategies"))

    def test_withdrawn_synthetic_marks_transitive(self):
        self.with_synthetic_claim()
        self.add_hypothesis(claim_id="CLM-0101", derived_from=["CLM-0100"])
        self.withdraw_synthetic()
        self.product_with("Вывод: дорого [CLM-0101].")
        self.assert_code("SYNTHETIC_UNMARKED", "strategies")

    def test_withdrawn_synthetic_fact_rejected(self):
        self.with_synthetic_claim()
        self.withdraw_synthetic()

        def make_fact(records):
            for record in records:
                if record["id"] == "CLM-0100":
                    record["kind"] = "fact"
        self.change("CLM", make_fact)
        self.assert_invalid_with("SYNTHETIC_NOT_EVIDENCE", "research")

    def test_withdrawn_synthetic_calculation_input_rejected(self):
        self.with_synthetic_claim()
        self.withdraw_synthetic()

        def use_input(records):
            item = records[0]["inputs"][0]
            item["source_id"], item["locator"] = "SRC-0005", {"kind": "section", "heading": "Вопрос 1"}
            item.pop("column", None)
            item.pop("aggregate", None)
        self.change("CALC", use_input)
        self.assert_invalid_with("SYNTHETIC_NOT_EVIDENCE", "research")

    def test_synthetic_source_cannot_contradict(self):
        self.with_synthetic_claim()

        def contradict(records):
            for record in records:
                if record["id"] == "CLM-0001":
                    record["contradicts"].append({"source_id": "SRC-0005",
                                                  "locator": {"kind": "section", "heading": "Вопрос 1"},
                                                  "note": "Персонаж не согласен", "resolution": "Модельная реплика."})
        self.change("CLM", contradict)
        self.assert_invalid_with("SYNTHETIC_NOT_EVIDENCE", "research")

    def test_rechosen_method_keeps_earlier_sessions(self):
        self.add_method()
        self.add_synthetic()
        self.add_approval("interview_method", "DEC-0008", "ACT-0102", at="2026-11-08T14:00:00+03:00",
                          supersedes="DEC-0006")
        self.assertEqual(self.codes("research"), set())

    def test_rechosen_method_keeps_earlier_budget(self):
        self.add_method()
        self.add_budget(at="2026-11-08T11:00:00+03:00")
        for number in range(1, 5):
            self.add_synthetic(number)
        self.add_approval("interview_method", "DEC-0008", "ACT-0102", at="2026-11-08T14:00:00+03:00",
                          supersedes="DEC-0006")
        self.assertEqual(self.codes("research"), set())

    def test_session_before_first_method_still_rejected_after_rechoice(self):
        self.add_method()
        self.add_synthetic(created_at="2026-11-08T09:00:00+03:00")
        self.add_approval("interview_method", "DEC-0008", "ACT-0102", at="2026-11-08T14:00:00+03:00",
                          supersedes="DEC-0006")
        self.assert_code("INTERVIEW_METHOD_MISSING", "research")

    def test_guide_without_question_sections(self):
        self.add_method()
        self.write_guide(GUIDE_MD.replace("Q-0002. Окупаемость", "Окупаемость"))
        self.assert_code("INTERVIEW_GUIDE", "research")

    def test_options_reference_needs_mark(self):
        self.with_synthetic_claim()
        self.replace("outputs/options.md", "Развивать [OPT-0003].", "Развивать [OPT-0003]. Риск [CLM-0100].")
        self.assert_code("SYNTHETIC_UNMARKED", "options")

    def test_synthetic_based_set(self):
        self.with_synthetic_claim()
        self.add_hypothesis(claim_id="CLM-0101", derived_from=["CLM-0100"])
        validator = gate.Validator(self.case)
        validator.run()
        found = gate.Gate(self.case, validator, "done", False, gate.Result()).synthetic_based()
        self.assertEqual(found, {"CLM-0100", "CLM-0101"})


if __name__ == "__main__":
    unittest.main()
