import contextlib
import io
import json
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))

import case_view  # noqa: E402
import case_view_md  # noqa: E402

DEMO = ROOT / "agent" / "contracts" / "examples" / "valid" / "demo-case"
BLOCKS = ["decision", "numbers", "team", "options", "texts", "basis", "revisions", "reviews", "accounting"]
EMPTY = "ещё нет"
TEAM_MD = """# Команда

Источник: слова пользователя, действие ACT-0004

| Имя | Роль |
|---|---|
| Анна К. | аналитик |
| Мария Р. | дизайн |
"""


def block_of(page: str, name: str) -> str:
    start = page.index(f'id="{name}"')
    later = [page.index(f'id="{other}"') for other in BLOCKS if other != name and page.find(f'id="{other}"') > start]
    return page[start:min(later)] if later else page[start:]


class CaseViewTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.case = Path(self.tmp.name) / "demo-case"
        shutil.copytree(DEMO, self.case)

    def write(self, rel: str, text: str) -> None:
        path = self.case / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(text.encode("utf-8"))

    def edit(self, rel: str, old: str, new: str) -> None:
        text = (self.case / rel).read_text(encoding="utf-8")
        self.assertIn(old, text)
        self.write(rel, text.replace(old, new))

    def edit_registry(self, rel: str, change) -> None:
        path = self.case / rel
        document = json.loads(path.read_text(encoding="utf-8"))
        change(document["records"])
        self.write(rel, json.dumps(document, ensure_ascii=False, indent=2) + "\n")

    def build(self) -> str:
        return case_view.build_page(self.case)[0]

    def run_cli(self, *args) -> tuple[int, str]:
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = case_view.main(list(args))
        return code, out.getvalue()


class DemoPageTest(CaseViewTest):
    def test_builds_and_has_blocks_in_order(self):
        page, warnings = case_view.build_page(self.case)
        self.assertEqual(warnings, [])
        positions = [page.index(f'id="{name}"') for name in BLOCKS]
        self.assertEqual(positions, sorted(positions))
        self.assertIn("Книжная полка (учебный кейс)", page)
        self.assertIn("CKP-0002", page)
        self.assertIn("Сохранение CKP-0002 от 5 ноября 2026 года, 12:30 (UTC+03:00)", page)
        # The accounting summary carries journal timestamps: they are shown the same way.
        self.assertIn("с 2 ноября 2026 года, 10:00 (UTC+03:00) по 5 ноября 2026 года, 12:30 (UTC+03:00)", page)
        self.assertIsNone(case_view.TIMESTAMP_RE.search(page))

    def test_human_time(self):
        self.assertEqual(case_view.human_time("2026-01-09T07:05:00Z"), "9 января 2026 года, 07:05 (UTC+00:00)")
        self.assertEqual(case_view.human_time("2026-03-01T23:59:00-05:30"), "1 марта 2026 года, 23:59 (UTC-05:30)")
        self.assertEqual(case_view.human_time("вчера"), "вчера")

    def test_no_script_no_external_resources(self):
        page = self.build()
        self.assertNotIn("<script", page.lower())
        self.assertNotRegex(page, r"\son[a-z]+\s*=")
        self.assertNotIn("<link", page)
        self.assertNotRegex(page, r'(?:src|href)="https?://(?!example\.invalid)')
        self.assertIn("prefers-color-scheme:dark", page)
        self.assertIn("@media print", page)

    def test_footnotes_have_targets(self):
        page = self.build()
        hrefs = set(re.findall(r'href="#(basis-[^"]+)"', page))
        self.assertTrue(hrefs)
        ids = set(re.findall(r'id="([^"]+)"', page))
        self.assertLessEqual(hrefs, ids)

    def test_decision_block(self):
        text = block_of(self.build(), "decision")
        self.assertIn("Оставить как есть", text)
        self.assertIn("Оставляем как есть, бюджет на развитие урезан", text)
        self.assertIn("выбор отличается от рекомендации агента", text)

    def test_numbers_block(self):
        text = block_of(self.build(), "numbers")
        self.assertIn("Среднее число заявок в месяц за 2025 год", text)
        self.assertIn("120", text)
        self.assertIn("заявок в месяц", text)

    def test_not_computable_number(self):
        self.edit("outputs/validation-plan.md", "Очереди в пунктах;", "Очереди в пунктах [CALC-0002];")
        self.assertIn("не вычислено", block_of(self.build(), "numbers"))

    def test_basis_labels(self):
        self.edit("outputs/product-strategy.md", "Окупаемость пункта", "Аренда [CLM-0008]. Окупаемость пункта")
        text = block_of(self.build(), "basis")
        for label in ("Факт", "Вывод", "Гипотеза", "не подтверждено", "спорно", "синтетика", "Не знаем"):
            self.assertIn(label, text)
        self.assertIn("Нет данных о доходах пункта", text)
        self.assertNotIn("Пункты работают ежедневно", text)

    def test_deterministic(self):
        first = (case_view.build_page(self.case)[0]).encode("utf-8")
        second = (case_view.build_page(self.case)[0]).encode("utf-8")
        self.assertEqual(first, second)

    def test_accounting_block(self):
        text = block_of(self.build(), "accounting")
        self.assertIn("Полное время", text)
        self.assertIn("неизвестно", text)

    def test_reviews_and_revisions(self):
        self.write("state/reviews/review-0001.md",
                   "# Проверка 0001 — 2026-11-20\n\n- Действие: ACT-0042\n- Итог: замечаний 4, принято 3, отклонено 1\n")
        page = self.build()
        reviews = block_of(page, "reviews")
        self.assertRegex(reviews, r"4.*3.*1")
        self.assertIn("Проверка 0001", reviews)
        revisions = block_of(page, "revisions")
        self.assertIn("Пересмотр 1", revisions)
        self.assertIn(".history/1/commitments.md", revisions)

    def test_withdrawn_claim_not_in_basis(self):
        self.edit("outputs/product-strategy.md", "Окупаемость пункта", "Пункты работают ежедневно [CLM-0005]. Окупаемость пункта")
        page = self.build()
        self.assertNotIn('id="basis-CLM-0005"', page)
        self.assertNotIn('href="#basis-CLM-0005"', page)

    def test_options_block(self):
        text = block_of(self.build(), "options")
        for title in ("Оставить как есть", "Закрыть направление", "Развивать сервис"):
            self.assertIn(title, text)
        self.assertIn("выбрано", text)
        self.assertIn("рекомендовал агент", text)
        self.assertIn("<details", text)

    def test_options_block_shows_cost_and_limit_check_when_recorded(self):
        self.assertNotIn("cost-line", block_of(self.build(), "options"))

        def change(records):
            records[0]["cost"] = {"status": "none", "note": "Затрат нет <b>совсем</b>"}
            records[0]["limit_check"] = {"brief_decision": None, "result": "no_limit", "note": "Бриф ограничений не называет"}
            records[1]["cost"] = {"status": "not_estimated", "reason": "Нет цены пункта", "question_id": "Q-0002"}
            records[1]["limit_check"] = {"brief_decision": None, "result": "unknown", "note": "Нечего сравнивать"}
            records[2]["cost"] = {"status": "estimated", "calc_ids": ["CALC-0001"], "note": "Учтён объём заявок"}
            records[2]["limit_check"] = {"brief_decision": None, "result": "exceeds", "calc_id": "CALC-0001"}
        self.edit_registry("decisions/options.json", change)
        text = block_of(self.build(), "options")
        for shown in ("Стоимость: затрат нет", "Стоимость: не оценена", "Стоимость: оценена",
                      "Ограничение брифа: не укладывается", "Ограничение брифа: в брифе нет ограничения",
                      "Ограничение брифа: неизвестно", "Нет цены пункта", "Учтён объём заявок",
                      "Бриф ограничений не называет"):
            self.assertIn(shown, text)
        self.assertNotIn("<b>совсем</b>", text)
        self.assertIn("&lt;b&gt;совсем&lt;/b&gt;", text)


class SafetyTest(CaseViewTest):
    def test_script_in_claim_is_text(self):
        def change(records):
            records[0]["text"] = "<script>alert(1)</script>"
        self.edit_registry("evidence/claims.json", change)
        self.edit("outputs/product-strategy.md", "Окупаемость пункта", "Правило [CLM-0001]. Окупаемость пункта")
        page = self.build()
        self.assertIn("&lt;script&gt;alert(1)&lt;/script&gt;", page)
        self.assertNotIn("<script", page.lower())

    def test_markdown_link_is_text(self):
        self.edit("outputs/product-strategy.md", "Окупаемость пункта", "[x](javascript:alert(1)) Окупаемость пункта")
        page = self.build()
        self.assertNotRegex(page, r'<a[^>]*javascript:')
        self.assertIn("[x](javascript:alert(1))", page)

    def test_raw_html_emphasis_and_code_are_text(self):
        text = case_view_md.render(["<b>x</b> **y**", "```", "code", "```", "[CLM-0001](javascript:1)"],
                                   lambda _id: None)
        self.assertNotIn("<b>", text)
        self.assertNotIn("<strong>", text)
        self.assertNotIn("<code>", text)
        self.assertNotIn("<a", text)
        self.assertIn("&lt;b&gt;x&lt;/b&gt; **y**", text)

    def test_member_name_is_escaped(self):
        self.write("team.md", TEAM_MD.replace("Анна К.", "<b>x</b>"))
        page = self.build()
        self.assertIn("&lt;b&gt;x&lt;/b&gt;", page)
        self.assertNotIn("<b>x</b>", page)

    def test_title_is_escaped(self):
        self.write("case.json", json.dumps({"format_version": 1, "case_id": "demo-case",
                                            "title": "<i>t</i>", "created_at": "2026-11-02T10:00:00+03:00"}))
        page = self.build()
        self.assertNotIn("<i>t</i>", page)
        self.assertIn("&lt;i&gt;t&lt;/i&gt;", page)

    def test_source_links(self):
        def change(records):
            records[0]["location"] = {"path": "sources/отчёт 1.pdf"}
            records[0]["type"] = "pdf"
        self.edit_registry("evidence/sources.json", change)
        self.write("sources/отчёт 1.pdf", "x")
        page = self.build()
        self.assertIn('href="../sources/%D0%BE%D1%82%D1%87%D1%91%D1%82%201.pdf"', page)
        self.assertIn('rel="noopener noreferrer"', page)  # the web source of the demo case

        def drop(records):
            records[0]["location"] = {"url": None}
        self.edit_registry("evidence/sources.json", drop)
        page = self.build()
        self.assertNotIn("%D0%BE%D1%82%D1%87", page)

    def test_only_http_urls_become_links(self):
        def change(records):
            records[2]["location"] = {"url": "javascript:alert(1)"}
        self.edit_registry("evidence/sources.json", change)
        page = self.build()
        self.assertNotRegex(page, r'href="[^"]*javascript')


class TeamBlockTest(CaseViewTest):
    def test_roster_cards(self):
        self.write("team.md", TEAM_MD)
        self.edit("outputs/commitments.md", "руководитель продукта", "Анна К.")
        text = block_of(self.build(), "team")
        self.assertIn("Анна К.", text)
        self.assertIn("Мария Р.", text)
        self.assertIn("Действий не назначено", text)
        self.assertEqual(text.count("Действий не назначено"), 1)
        self.assertIn(">АК<", text)
        self.assertIn(">МР<", text)
        self.assertIn("Сохранить четыре пункта и правила обмена", text)
        self.assertIn("допущение", text)

    def test_action_with_two_owners_appears_under_each(self):
        self.write("team.md", TEAM_MD)
        self.edit("outputs/commitments.md", "| руководитель продукта | 2026-12-01",
                  "| Анна К., Мария Р. | 2026-12-01")
        self.edit("outputs/commitments.md", "руководитель продукта", "Анна К.")
        text = block_of(self.build(), "team")
        self.assertEqual(text.count("Сохранить четыре пункта и правила обмена"), 2)

    def test_no_team_file_cards_by_owner_text(self):
        self.edit("outputs/commitments.md", "| руководитель продукта | 2027", "| аналитик | 2027")
        text = block_of(self.build(), "team")
        self.assertEqual(text.count('class="person'), 2)
        self.assertIn("руководитель продукта", text)
        self.assertIn("аналитик", text)

    def test_not_named_roster(self):
        self.write("team.md", "# Команда\n\nИсточник: слова пользователя, действие ACT-0004\n\n"
                              "Состав не назван: ответственные записаны ролями.\n")
        text = block_of(self.build(), "team")
        self.assertEqual(text.count('class="person'), 1)
        self.assertIn("руководитель продукта", text)

    def test_owner_outside_roster_is_shown(self):
        self.write("team.md", TEAM_MD)
        text = block_of(self.build(), "team")
        self.assertIn("руководитель продукта", text)
        self.assertIn("нет в составе", text)

    def test_avatar_class_is_stable(self):
        self.write("team.md", TEAM_MD)
        text = block_of(self.build(), "team")
        name = "Анна К."
        self.assertIn(f"avatar-{sum(map(ord, name)) % 6}", text)

    def test_bom_and_crlf_roster(self):
        (self.case / "team.md").write_bytes(b"\xef\xbb\xbf" + TEAM_MD.replace("\n", "\r\n").encode("utf-8"))
        text = block_of(self.build(), "team")
        self.assertIn("Анна К.", text)


class MinimalCaseTest(CaseViewTest):
    def minimal(self) -> Path:
        small = Path(self.tmp.name) / "small"
        small.mkdir()
        shutil.copy(DEMO / "case.json", small / "case.json")
        shutil.copy(DEMO / "brief.md", small / "brief.md")
        return small

    def test_only_case_json_and_brief(self):
        small = self.minimal()
        code, out = self.run_cli("build", str(small))
        self.assertEqual(code, 0)
        page = (small / "outputs" / "case-page.html").read_text(encoding="utf-8")
        for name in BLOCKS:
            self.assertIn(EMPTY, block_of(page, name), name)
        self.assertIn("сохранений нет", page)

    def test_unreadable_case_json(self):
        small = self.minimal()
        (small / "case.json").write_text("{not json", encoding="utf-8")
        code, out = self.run_cli("build", str(small))
        self.assertEqual(code, 4)
        self.assertFalse((small / "outputs" / "case-page.html").exists())
        self.assertEqual(json.loads(out.splitlines()[0])["status"], "refused")

    def test_missing_folder(self):
        code, _ = self.run_cli("build", str(Path(self.tmp.name) / "nope"))
        self.assertEqual(code, 4)

    def test_broken_registry_is_a_warning(self):
        self.write("decisions/decisions.json", "{broken")
        page, warnings = case_view.build_page(self.case)
        self.assertTrue(any(w["file"] == "decisions/decisions.json" for w in warnings))
        self.assertIn(EMPTY, block_of(page, "decision"))
        self.assertEqual(set(warnings[0]), {"code", "file", "note"})

    def test_preliminary_marker(self):
        self.edit("outputs/product-strategy.md", "Статус: выбор сделан [DEC-0002], вариант [OPT-0001]",
                  "Статус: предварительно, выбор не сделан, предложен вариант [OPT-0003]")
        self.assertIn("предварительно", block_of(self.build(), "texts"))


class SyntheticTest(CaseViewTest):
    def test_claim_from_synthetic_source_is_marked(self):
        text = block_of(self.build(), "basis")
        item = text[text.index('id="basis-CLM-0009"'):]
        item = item[:item.index("</article>")]
        self.assertIn("синтетика", item)
        self.assertIn("Гипотеза", item)


class CliTest(CaseViewTest):
    def test_cli_first_line_and_file(self):
        code, out = self.run_cli("build", str(self.case))
        self.assertEqual(code, 0)
        first = json.loads(out.splitlines()[0])
        self.assertEqual(first["status"], "ok")
        self.assertEqual(first["path"], "outputs/case-page.html")
        self.assertEqual(first["warnings"], [])
        data = (self.case / "outputs" / "case-page.html").read_bytes()
        self.assertFalse(data.startswith(b"\xef\xbb\xbf"))
        self.assertNotIn(b"\r", data)

    def test_cli_as_subprocess(self):
        done = subprocess.run([sys.executable, str(ROOT / "tools" / "case_view.py"), "build", str(self.case)],
                              capture_output=True, check=False)
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(json.loads(done.stdout.decode("utf-8").splitlines()[0])["status"], "ok")


class DoesNotDisturbTest(CaseViewTest):
    def snapshot(self):
        import gate
        import validate_project
        report = validate_project.validate_case(self.case)
        result = gate.run_check(self.case, "done", False)
        return ([(f.code, f.file) for f in report.errors], [(f.code, f.file) for f in report.warnings],
                result.problems, result.warnings)

    def test_validator_and_gate_unchanged(self):
        before = self.snapshot()
        self.run_cli("build", str(self.case))
        self.assertEqual(before, self.snapshot())

    def test_checkpoint_does_not_pin_the_page(self):
        import checkpoint
        self.run_cli("build", str(self.case))
        self.assertNotIn("outputs/case-page.html", checkpoint.collect_artifacts(self.case))

    def test_rebuild_replaces_the_page(self):
        self.run_cli("build", str(self.case))
        first = (self.case / "outputs" / "case-page.html").read_bytes()
        self.run_cli("build", str(self.case))
        self.assertEqual(first, (self.case / "outputs" / "case-page.html").read_bytes())
        self.assertEqual([p.name for p in (self.case / "outputs").glob("*.tmp")], [])


class MarkdownTest(unittest.TestCase):
    def test_structures(self):
        lines = ["# Title", "", "para one", "para two", "", "- a", "- b", "", "1. x", "2. y", "",
                 "| A | B |", "|---|---|", "| 1 | 2 |"]
        text = case_view_md.render(lines, lambda _id: None)
        self.assertIn("<h3>Title</h3>", text)
        self.assertIn("<p>para one para two</p>", text)
        self.assertIn("<ul><li>a</li><li>b</li></ul>", text)
        self.assertIn("<ol><li>x</li><li>y</li></ol>", text)
        self.assertIn("<th>A</th>", text)
        self.assertIn("<td", text)

    def test_links_and_marks(self):
        def footnote(record_id):
            return f'<a href="#{record_id}">1</a>' if record_id == "CLM-0001" else None
        text = case_view_md.render(["Текст [CLM-0001, CALC-0002] (допущение) (синтетика) [бриф] [CLM-0099]"], footnote)
        self.assertIn('<a href="#CLM-0001">1</a>', text)
        self.assertIn('<span class="ref">CALC-0002</span>', text)
        self.assertIn("допущение</span>", text)
        self.assertIn("синтетика</span>", text)
        self.assertIn("слова руководителя", text)
        self.assertIn("[CLM-0099]", text)


if __name__ == "__main__":
    unittest.main()
