import hashlib
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))

import validate_project as vp  # noqa: E402

CONTRACTS = ROOT / "agent" / "contracts"
EXAMPLES = CONTRACTS / "examples"
VALID = EXAMPLES / "valid" / "demo-case"
INVALID = EXAMPLES / "invalid"
SPEC = ROOT / "docs" / "specs" / "2026-11-05-b01-contracts.md"
VALIDATOR = ROOT / "tools" / "validate_project.py"

REGISTRIES = [
    "evidence/sources.json",
    "evidence/claims.json",
    "evidence/calculations.json",
    "research/questions.json",
    "decisions/options.json",
    "decisions/decisions.json",
]
JOURNAL = "state/actions.jsonl"


def read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def write_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def tree_hash(root):
    digest = hashlib.sha256()
    for path in sorted(Path(root).rglob("*")):
        digest.update(path.relative_to(root).as_posix().encode("utf-8"))
        if path.is_file():
            digest.update(path.read_bytes())
    return digest.hexdigest()


def codes(report):
    return sorted(f.code for f in report.errors), sorted(f.code for f in report.warnings)


def spec_codes():
    text = SPEC.read_text(encoding="utf-8")
    section = text.split("## Правила связей")[1].split("## Валидатор")[0]
    return set(re.findall(r"`([A-Z][A-Z_]+)`", section)) - {"ACT", "CKP"}


class CaseTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.case = Path(self.tmp.name) / "demo-case"
        shutil.copytree(VALID, self.case)

    def edit(self, rel, change):
        data = read_json(self.case / rel)
        change(data)
        write_json(self.case / rel, data)

    def run_case(self):
        return vp.validate_case(self.case)

    def cli(self, *args, env=None):
        return subprocess.run(
            [sys.executable, str(VALIDATOR), *map(str, args)],
            capture_output=True,
            env={**os.environ, **(env or {})},
        )


class ValidExample(CaseTestCase):
    def test_valid_case_passes(self):
        report = self.run_case()
        expected = read_json(EXAMPLES / "valid" / "expected-warnings.json")["warnings"]
        self.assertEqual(report.errors, [])
        self.assertEqual(sorted(f.code for f in report.warnings), sorted(expected))
        self.assertIsNotNone(report.current_checkpoint)

    def test_valid_example_covers_every_type(self):
        self.assertTrue((self.case / "case.json").is_file())
        records = {name: read_json(self.case / name)["records"] for name in REGISTRIES}
        for name, items in records.items():
            self.assertTrue(items, name)
        claims = records["evidence/claims.json"]
        self.assertTrue(any(c["status"] == "withdrawn" for c in claims))
        self.assertTrue(any(c["status"] == "contested" for c in claims))
        self.assertTrue(any(
            c["status"] == "confirmed" and c["contradicts"]
            and all(x.get("resolution") for x in c["contradicts"]) for c in claims))
        self.assertTrue(any(c["kind"] == "inference" for c in claims))
        self.assertTrue(any(r["record_status"] == "withdrawn" for r in records["evidence/sources.json"]))
        self.assertTrue(any(r["result"]["status"] == "not_computable" for r in records["evidence/calculations.json"]))
        decisions = records["decisions/decisions.json"]
        self.assertTrue(any(d["status"] == "superseded" for d in decisions))
        self.assertTrue(any(d["status"] == "chosen" and d.get("supersedes") for d in decisions))
        lines = [json.loads(x) for x in (self.case / JOURNAL).read_text(encoding="utf-8").splitlines()]
        ends = [x for x in lines if x["phase"] == "end"]
        self.assertTrue(any(x["cost"]["basis"] == "unknown" for x in ends))
        self.assertTrue(any(x["cost"]["basis"] == "estimated" for x in ends))
        self.assertTrue(any(x["cost"]["basis"] == "measured" for x in ends))
        self.assertTrue(any(x["phase"] == "start" and x.get("retry_of") for x in lines))
        self.assertTrue(any(x["phase"] == "start" and x["kind"] == "human_input" for x in lines))
        self.assertGreaterEqual(len(list((self.case / "state" / "checkpoints").glob("CKP-*.json"))), 2)
        self.assertTrue(list((self.case / "outputs").glob("*.md")))

    def test_case_runs_without_work_dir_or_chat(self):
        # The case lives outside the repository, there is no .work anywhere,
        # and the validator runs from an unrelated working directory.
        elsewhere = Path(self.tmp.name) / "elsewhere"
        elsewhere.mkdir()
        self.assertNotIn(ROOT, self.case.resolve().parents)
        self.assertFalse((elsewhere / ".work").exists())
        self.assertFalse(any(p.name == ".work" for p in self.case.rglob("*")))
        result = subprocess.run([sys.executable, str(VALIDATOR), str(self.case)],
                                capture_output=True, cwd=elsewhere)
        self.assertEqual(result.returncode, 0, result.stdout.decode("utf-8", "replace"))
        self.assertIn(b"current checkpoint: CKP-0002", result.stdout)

    def test_examples_have_no_absolute_paths_or_keys(self):
        patterns = [
            r"[A-Za-z]:\\", r"/Users/", r"/home/", r"\bsk-[A-Za-z0-9]{8,}",
            r"\bghp_[A-Za-z0-9]{8,}", r"BEGIN [A-Z ]*PRIVATE KEY",
        ]
        checked = 0
        for path in CONTRACTS.rglob("*"):
            if not path.is_file() or INVALID in path.parents:
                continue
            text = path.read_text(encoding="utf-8")
            for pattern in patterns:
                self.assertIsNone(re.search(pattern, text), f"{path.name}: {pattern}")
            checked += 1
        self.assertGreater(checked, 20)


class InvalidExamples(unittest.TestCase):
    def assemble(self, folder, target):
        shutil.copytree(VALID, target)
        expected = read_json(folder / "expected.json")
        for rel in expected.get("delete", []):
            (target / rel).unlink()
        for path in folder.rglob("*"):
            if path.is_file() and path.name != "expected.json":
                dest = target / path.relative_to(folder)
                dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(path, dest)
        return expected

    def test_invalid_cases(self):
        folders = sorted(p for p in INVALID.iterdir() if p.is_dir())
        self.assertGreater(len(folders), 30)
        for folder in folders:
            with self.subTest(case=folder.name), tempfile.TemporaryDirectory() as tmp:
                target = Path(tmp) / "demo-case"
                expected = self.assemble(folder, target)
                with mock.patch.object(
                    vp, "SUPPORTED_VERSIONS", frozenset(expected.get("supported_versions", [1]))
                ):
                    report = vp.validate_case(target)
                self.assertEqual(
                    (sorted(expected["errors"]), sorted(expected["warnings"])),
                    codes(report),
                    [f.message for f in report.errors + report.warnings],
                )

    def test_every_code_covered(self):
        covered = set()
        for path in INVALID.glob("*/expected.json"):
            data = read_json(path)
            covered.update(data["errors"], data["warnings"])
        self.assertEqual(covered, set(vp.ALL_CODES))
        self.assertEqual(set(vp.ALL_CODES), spec_codes())
        self.assertFalse(set(vp.ERROR_CODES) & set(vp.WARNING_CODES))


class ReportContract(CaseTestCase):
    def test_output_has_no_field_values(self):
        marker = "MARKER-secret-text-91"

        def change(data):
            claim = data["records"][0]
            claim["text"] = marker
            claim["kind"] = "not-a-kind"

        self.edit("evidence/claims.json", change)
        text = self.cli(self.case)
        as_json = self.cli(self.case, "--json")
        self.assertEqual(text.returncode, 1)
        self.assertEqual(as_json.returncode, 1)
        self.assertIn("SCHEMA", text.stdout.decode("utf-8"))
        self.assertNotIn(marker.encode(), text.stdout + text.stderr)
        self.assertNotIn(marker.encode(), as_json.stdout + as_json.stderr)

    def test_json_output_shape(self):
        result = self.cli(self.case, "--json")
        data = json.loads(result.stdout.decode("utf-8"))
        self.assertEqual(set(data), {"errors", "warnings", "current_checkpoint"})
        self.assertEqual(data["errors"], [])
        self.assertEqual(data["current_checkpoint"], vp.validate_case(self.case).current_checkpoint)

    def test_read_only(self):
        before = tree_hash(self.case)
        vp.validate_case(self.case)
        self.cli(self.case, "--json")
        self.assertEqual(before, tree_hash(self.case))

    def test_bad_invocation_exits_2(self):
        self.assertEqual(self.cli(self.case.parent / "missing").returncode, 2)
        self.assertEqual(self.cli().returncode, 2)

    def test_unknown_version_stops_before_records(self):
        self.edit("evidence/claims.json", lambda d: d.update(format_version=99))
        self.edit("decisions/options.json", lambda d: d["records"][0].pop("title"))
        report = self.run_case()
        self.assertEqual(codes(report), (["FORMAT_UNSUPPORTED"], []))
        self.assertEqual(report.errors[0].file, "evidence/claims.json")

    def test_unknown_version_in_journal_stops(self):
        path = self.case / JOURNAL
        lines = path.read_text(encoding="utf-8").splitlines()
        first = json.loads(lines[0])
        first["format_version"] = 7
        lines[0] = json.dumps(first)
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        self.assertEqual(codes(self.run_case()), (["FORMAT_UNSUPPORTED"], []))

    def test_unknown_or_differing_version_in_checkpoint_falls_back(self):
        for version, supported in ((7, {1}), (2, {1, 2})):
            with self.subTest(version=version):
                self.edit("state/checkpoints/CKP-0002.json", lambda d: d.update(format_version=version))
                with mock.patch.object(vp, "SUPPORTED_VERSIONS", frozenset(supported)):
                    report = self.run_case()
                self.assertEqual(codes(report), ([], ["CHECKPOINT_FALLBACK"]))
                self.assertEqual(report.current_checkpoint, "CKP-0001")


class MalformedCases(CaseTestCase):
    def test_registry_with_wrong_shape_is_schema_error(self):
        for body in ('[]', '"text"', 'null', '{"format_version": 1}', '{"format_version": 1, "records": {}}'):
            with self.subTest(body=body):
                (self.case / "evidence" / "claims.json").write_text(body, encoding="utf-8")
                errors, _ = codes(self.run_case())
                self.assertIn("SCHEMA", errors)
                self.assertEqual(set(errors), {"SCHEMA"})

    def test_non_utf8_json_is_file_corrupt(self):
        (self.case / "evidence" / "claims.json").write_bytes(b'{"format_version": 1, "records": []}\xff\xfe')
        self.assertEqual(codes(self.run_case()), (["FILE_CORRUPT"], []))

    def test_bom_prefixed_json_is_accepted(self):
        path = self.case / "research" / "questions.json"
        path.write_bytes(b"\xef\xbb\xbf" + path.read_bytes())
        self.assertEqual(self.run_case().errors, [])

    def test_corrupt_line_before_the_end_of_journal(self):
        path = self.case / JOURNAL
        lines = path.read_text(encoding="utf-8").splitlines()
        lines[1] = lines[1][:20]
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        errors, warnings = codes(self.run_case())
        self.assertIn("FILE_CORRUPT", errors)
        self.assertNotIn("JOURNAL_TORN", warnings)

    def test_cycle_in_derived_from_is_weak_derivation(self):
        def change(data):
            first, second = data["records"][0], data["records"][1]
            first.update(kind="inference", status="confirmed", derived_from=[second["id"]], supports=[], contradicts=[])
            second.update(kind="inference", status="confirmed", derived_from=[first["id"]], supports=[], contradicts=[])

        self.edit("evidence/claims.json", change)
        errors, _ = codes(self.run_case())
        self.assertEqual(errors.count("CLAIM_WEAK_DERIVATION"), 2)

    def test_mode_files(self):
        router = (ROOT / "AGENTS.md").read_text(encoding="utf-8")
        claude = (ROOT / "CLAUDE.md").read_text(encoding="utf-8")
        product = (ROOT / "agent/instructions/product-mode.md").read_text(encoding="utf-8")
        for target in ("agent/instructions/product-mode.md", "docs/development-rules.md", "agent/contracts/modes.md"):
            self.assertIn(target, router)
            self.assertTrue((ROOT / target).is_file(), target)
        self.assertIn("@AGENTS.md", claude)
        self.assertIn("`Режим: продукт.`", router)
        self.assertIn("`Режим: разработка.`", router)
        self.assertIn("`Режим: продукт.`", product)
        self.assertNotIn("@docs/development-rules.md", claude)
        self.assertNotRegex(product, r"git (add|commit|push|switch|checkout)")

    def test_cycle_in_supersedes_is_decision_supersede(self):
        def change(data):
            first, second = data["records"][0], data["records"][1]
            first["supersedes"] = second["id"]
            first["change_reason"] = "cycle"
            second["supersedes"] = first["id"]
            second["change_reason"] = "cycle"
            first["status"] = second["status"] = "superseded"

        self.edit("decisions/decisions.json", change)
        errors, _ = codes(self.run_case())
        self.assertEqual(errors.count("DECISION_SUPERSEDE"), 1)

    def test_windows_and_unsafe_paths(self):
        bad = [
            "C:\\x", "\\\\srv\\x", "a\\..\\b", "/x", "sources/../x", "sources/a/../../x",
            "sources//x", "sources/./x", "x:y", "other/file.md",
        ]
        original = read_json(self.case / "evidence" / "sources.json")
        original_ckp = read_json(self.case / "state" / "checkpoints" / "CKP-0002.json")
        for value in bad:
            with self.subTest(source_path=value):
                data = json.loads(json.dumps(original))
                data["records"][1]["location"]["path"] = value
                write_json(self.case / "evidence" / "sources.json", data)
                errors, _ = codes(self.run_case())
                self.assertEqual(errors, ["PATH_UNSAFE"])
        write_json(self.case / "evidence" / "sources.json", original)
        for value in bad[:-1]:
            with self.subTest(artifact_path=value):
                data = json.loads(json.dumps(original_ckp))
                data["artifacts"][0]["path"] = value
                write_json(self.case / "state" / "checkpoints" / "CKP-0002.json", data)
                errors, _ = codes(self.run_case())
                self.assertEqual(errors, ["PATH_UNSAFE"])

    def test_non_ascii_parent_folder_with_spaces_and_console_encoding(self):
        parent = Path(self.tmp.name) / "Каталог с пробелом"
        parent.mkdir()
        moved = parent / "demo-case"
        shutil.copytree(self.case, moved)
        (moved / "outputs" / "стратегия.md").write_text("Битая ссылка [CLM-0099].\n", encoding="utf-8")
        ok = self.cli(self.case, env={"PYTHONIOENCODING": "cp1251"})
        self.assertEqual(ok.returncode, 0)
        result = self.cli(moved, env={"PYTHONIOENCODING": "cp1251"})
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertIn("стратегия.md", result.stdout.decode("utf-8"))
        self.assertEqual(result.stderr, b"")
        report = vp.validate_case(moved)
        self.assertEqual([f.code for f in report.errors], ["OUTPUT_LINK"])


SOURCES = "evidence/sources.json"
CLAIMS = "evidence/claims.json"
CALCS = "evidence/calculations.json"
QUESTIONS = "research/questions.json"
OPTIONS = "decisions/options.json"
DECISIONS = "decisions/decisions.json"
CKP2 = "state/checkpoints/CKP-0002.json"


class Mutating(CaseTestCase):
    """Edit the valid case and check which findings appear."""

    def rec(self, rel, rid, change):
        self.edit(rel, lambda d: change(next(r for r in d["records"] if r["id"] == rid)))

    def journal(self, change):
        path = self.case / JOURNAL
        lines = [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines()]
        change(lines)
        path.write_text("".join(json.dumps(x, ensure_ascii=False) + "\n" for x in lines), encoding="utf-8")

    @staticmethod
    def line(lines, rid, phase):
        return next(x for x in lines if x["id"] == rid and x["phase"] == phase)

    def reset(self):
        shutil.rmtree(self.case)
        shutil.copytree(VALID, self.case)

    def expect(self, errors=(), warnings=()):
        report = self.run_case()
        got_errors, got_warnings = codes(report)
        for code in errors:
            self.assertIn(code, got_errors, [f.message for f in report.errors])
        for code in warnings:
            self.assertIn(code, got_warnings, [f.message for f in report.warnings])
        return report

    def each(self, cases):
        for label, mutate, errors, warnings in cases:
            with self.subTest(label):
                self.reset()
                mutate()
                self.expect(errors, warnings)


class Robustness(Mutating):
    def test_decision_option_of_another_type_is_wrong_type(self):
        for target in ("CLM-0001", "ACT-0001", "SRC-0001", "DEC-0001"):
            def mutate(d, target=target):
                d["records"][1]["choice"]["option_id"] = target
                d["records"][1]["proposal"]["option_id"] = target

            with self.subTest(target):
                self.reset()
                self.edit(DECISIONS, mutate)
                self.expect(["ID_WRONG_TYPE"])
                self.assertEqual(self.cli(self.case).returncode, 1)

    def test_duplicate_id_after_a_record_that_failed_its_schema(self):
        def change(data):
            copy_of = dict(data["records"][1], id="CLM-0001")
            data["records"][0].pop("text")
            data["records"].append(copy_of)

        self.edit(CLAIMS, change)
        self.expect(["SCHEMA", "ID_DUPLICATE"])

    def test_record_with_id_of_another_type(self):
        self.edit(CLAIMS, lambda d: d["records"].append(dict(d["records"][1], id="ACT-0001")))
        report = self.expect(["SCHEMA"])
        self.assertNotIn("ID_DUPLICATE", codes(report)[0])
        self.assertNotIn("ID_MISSING", codes(report)[0])

    def test_directory_in_place_of_a_file(self):
        for rel in (CLAIMS, JOURNAL, "case.json", CKP2):
            with self.subTest(rel):
                self.reset()
                (self.case / rel).unlink()
                (self.case / rel).mkdir()
                report = self.run_case()
                if rel == CKP2:
                    self.assertIn("CHECKPOINT_FALLBACK", codes(report)[1])
                else:
                    self.assertIn("FILE_CORRUPT", codes(report)[0])

    def test_unreadable_files_do_not_crash(self):
        original = Path.open

        def refuse(path, *args, **kwargs):
            if path.name in ("monthly-requests.csv", "validation-plan.md"):
                raise PermissionError("denied")
            return original(path, *args, **kwargs)

        with mock.patch.object(Path, "open", refuse):
            report = self.run_case()
        errors, warnings = codes(report)
        self.assertIn("SOURCE_MISSING", errors)
        self.assertIn("CHECKPOINT_FALLBACK", warnings)

    def test_internal_failure_exits_3_without_text(self):
        marker = "MARKER-exception-text"
        for extra in ([], ["--json"]):
            with self.subTest(extra), mock.patch.object(vp, "validate_case", side_effect=RuntimeError(marker)), \
                    mock.patch("sys.stdout", new_callable=io.StringIO) as out, \
                    mock.patch("sys.stderr", new_callable=io.StringIO) as err:
                self.assertEqual(vp.main([str(self.case), *extra]), 3)
            self.assertNotIn(marker, out.getvalue() + err.getvalue())
            if extra:
                self.assertTrue(json.loads(out.getvalue())["internal_error"])

    def test_case_json_null_is_case_invalid(self):
        (self.case / "case.json").write_text("null", encoding="utf-8")
        self.assertEqual(codes(self.run_case())[0], ["CASE_INVALID"])

    def test_format_version_must_be_an_integer(self):
        for value in (99.0, 2.0, 1.0, True, "1", None):
            with self.subTest(value=value):
                self.reset()
                self.edit(CLAIMS, lambda d: d.update(format_version=value))
                self.assertIn("SCHEMA", codes(self.run_case())[0])
        self.reset()
        self.edit("case.json", lambda d: d.update(format_version=99.0))
        self.assertIn("SCHEMA", codes(self.run_case())[0])

    def test_values_with_stray_characters_fail_the_schema(self):
        cases = [
            ("trailing newline in id", OPTIONS, lambda d: d["records"][0]["basis"].append("CLM-0001\n")),
            ("non-ASCII digits", OPTIONS, lambda d: d["records"][0]["basis"].append("CLM-\u0660\u0660\u0660\u0661")),
            ("topic with newline", DECISIONS, lambda d: d["records"][1].update(topic="direction\n")),
            ("case id with newline", "case.json", lambda d: d.update(case_id="demo-case\n")),
            ("record id with newline", CLAIMS, lambda d: d["records"][0].update(id="CLM-0001\n")),
        ]
        for label, rel, change in cases:
            with self.subTest(label):
                self.reset()
                self.edit(rel, change)
                self.assertIn("SCHEMA", codes(self.run_case())[0])

    def test_invalid_dates_fail_the_schema(self):
        bad = ("2026-02-30T10:00:00+03:00", "2026-11-05T10:00:00", "2026-11-05 10:00:00+03:00",
               "2026-13-01T10:00:00Z", "2026-11-05T25:00:00Z", "05.11.2026", "2026-11-05T10:00:00+99:00")
        for value in bad:
            with self.subTest(value):
                self.reset()
                self.rec(CLAIMS, "CLM-0002", lambda r: r.update(created_at=value))
                self.assertIn("SCHEMA", codes(self.run_case())[0])
        for value in ("2026-11-05T10:00:00Z", "2026-11-05T10:00:00.250+03:00"):
            with self.subTest(value):
                self.reset()
                self.rec(CLAIMS, "CLM-0002", lambda r: r.update(created_at=value))
                self.assertEqual(self.run_case().errors, [])


class JournalAndCheckpoints(Mutating):
    def test_tear_cut_append_passes(self):
        path = self.case / JOURNAL
        path.write_bytes(path.read_bytes() + b'{"format_version": 1, "id": "ACT-0010", "ph')
        self.assertEqual(codes(self.run_case()), ([], ["JOURNAL_TORN"]))
        data = path.read_bytes()
        path.write_bytes(data[: data.rindex(b"\n") + 1])
        start = {"format_version": 1, "id": "ACT-0010", "phase": "start", "kind": "save", "tool": "checkpoint",
                 "environment": "codex", "model": None, "input_refs": [], "at": "2026-11-03T10:00:00+03:00"}
        end = {"format_version": 1, "id": "ACT-0010", "phase": "end", "outcome": "completed", "result_refs": [],
               "cost": {"basis": "unknown"}, "at": "2026-11-03T10:00:01+03:00"}
        with path.open("ab") as handle:
            for line in (start, end):
                handle.write(json.dumps(line).encode("utf-8") + b"\n")
        self.assertEqual(codes(self.run_case()), ([], []))

    def test_checkpoint_claims(self):
        cases = [
            ("action that did not complete", lambda: self.edit(CKP2, lambda d: d["completed_actions"].append("ACT-0005")), ["CHECKPOINT_CLAIM"], []),
            ("action without end", lambda: self.journal(lambda ls: ls.remove(self.line(ls, "ACT-0006", "end"))), ["CHECKPOINT_CLAIM"], []),
            ("decision without choice", lambda: self.rec(DECISIONS, "DEC-0002", lambda r: r.pop("choice")), ["CHECKPOINT_CLAIM"], []),
        ]
        self.each(cases)

    def test_older_checkpoint_is_not_judged_for_claims(self):
        self.edit("state/checkpoints/CKP-0001.json", lambda d: d["completed_actions"].append("ACT-0005"))
        self.assertEqual(codes(self.run_case()), ([], []))

    def newer_checkpoint(self, change, raw=None, name="CKP-0003.json"):
        path = self.case / "state" / "checkpoints" / name
        if raw is not None:
            path.write_bytes(raw)
            return
        data = read_json(self.case / CKP2)
        data.update(id=name.removesuffix(".json"), seq=3, previous="CKP-0002")
        change(data)
        write_json(path, data)

    def test_unfit_newest_checkpoint_gives_fallback(self):
        cases = [
            ("broken JSON", None, b'{"format_version": 1,'),
            ("not UTF-8", None, b"\xff\xfe not json"),
            ("schema", lambda d: d.pop("goal"), None),
            ("unknown version", lambda d: d.update(format_version=9), None),
            ("seq does not follow previous", lambda d: d.update(seq=5), None),
            ("previous missing", lambda d: d.update(previous="CKP-0042"), None),
            ("id differs from file name", lambda d: d.update(id="CKP-0009"), None),
            ("foreign case", lambda d: d.update(case_id="other-case"), None),
            ("artifact missing", lambda d: d["artifacts"].append({"path": "outputs/none.md", "sha256": "0" * 64}), None),
            ("missing time field", lambda d: d["totals"].pop("time_to_first_result_seconds"), None),
        ]
        for label, change, raw in cases:
            with self.subTest(label):
                self.reset()
                self.newer_checkpoint(change, raw)
                report = self.run_case()
                self.assertEqual(codes(report), ([], ["CHECKPOINT_FALLBACK"]))
                self.assertEqual(report.current_checkpoint, "CKP-0002")
                self.assertIn("CKP-0003", report.warnings[0].message)

    def test_stray_file_in_checkpoint_folder_is_reported(self):
        for name in ("CKP-3.json", "ckp-0003.json", "CKP-0003.json.tmp", "notes.txt"):
            with self.subTest(name):
                self.reset()
                (self.case / "state" / "checkpoints" / name).write_text("{}", encoding="utf-8")
                self.assertEqual(codes(self.run_case()), ([], ["CHECKPOINT_FALLBACK"]))
        self.reset()
        (self.case / "state" / "checkpoints" / ".keep").write_text("", encoding="utf-8")
        self.assertEqual(codes(self.run_case()), ([], []))

    def test_only_a_stray_file_means_no_valid_checkpoint(self):
        for name in ("CKP-0001.json", "CKP-0002.json"):
            (self.case / "state" / "checkpoints" / name).unlink()
        (self.case / "state" / "checkpoints" / "latest.json").write_text("{}", encoding="utf-8")
        self.assertIn("CHECKPOINT_NONE_VALID", codes(self.run_case())[0])


class BranchCoverage(Mutating):
    def test_error_branches(self):
        def decision(change):
            return lambda: self.rec(DECISIONS, "DEC-0002", change)

        def output(text):
            return lambda: (self.case / "outputs" / "product-strategy.md").write_text(text, encoding="utf-8")

        def no_reason(rel, rid):
            return lambda: self.rec(rel, rid, lambda r: r.update(record_status="withdrawn"))

        def pop_end(rid):
            return lambda: self.journal(lambda ls: ls.remove(self.line(ls, rid, "end")))

        def set_end(rid, **fields):
            return lambda: self.journal(lambda ls: self.line(ls, rid, "end").update(fields))

        def withdrawn_calc_input():
            self.rec(CALCS, "CALC-0001", lambda r: r.update(record_status="withdrawn", status_reason="x"))
            self.rec(CALCS, "CALC-0002", lambda r: r["inputs"].append(
                {"name": "n", "calculation_id": "CALC-0001", "value": None, "unit": None, "period": None}))

        cases = [
            ("case id differs from folder", lambda: self.edit("case.json", lambda d: d.update(case_id="other-case")), ["CASE_INVALID"], []),
            ("replaced decision not superseded", lambda: self.rec(DECISIONS, "DEC-0001", lambda r: r.update(status="chosen")), ["DECISION_SUPERSEDE"], []),
            ("superseded without replacement", decision(lambda r: (r.pop("supersedes"), r.pop("change_reason"))), ["DECISION_SUPERSEDE"], []),
            ("proposal made by a human action", decision(lambda r: r["proposal"].update(action_id="ACT-0008")), ["DECISION_NOT_HUMAN"], []),
            ("unfinished human_input", pop_end("ACT-0008"), ["DECISION_NOT_HUMAN"], []),
            ("failed human_input", set_end("ACT-0008", outcome="failed"), ["DECISION_NOT_HUMAN"], []),
            ("decision missing in result_refs", set_end("ACT-0008", result_refs=[]), ["DECISION_NOT_HUMAN"], []),
            ("proposed with choice", decision(lambda r: r.update(status="proposed")), ["DECISION_CHOICE_STATE"], []),
            ("withdrawn source as basis", lambda: self.rec(SOURCES, "SRC-0001", lambda r: r.update(record_status="withdrawn", status_reason="x")), ["CLAIM_WEAK_SOURCE"], []),
            ("unreadable source as basis", lambda: self.rec(SOURCES, "SRC-0001", lambda r: r["extraction"].update(status="unreadable")), ["CLAIM_WEAK_SOURCE"], []),
            ("uncomputed calculation as basis", lambda: self.rec(CALCS, "CALC-0001", lambda r: r["result"].update(status="not_computable", value=None)), ["CLAIM_WEAK_DERIVATION"], []),
            ("decision uses withdrawn option", lambda: self.rec(OPTIONS, "OPT-0001", lambda r: r.update(record_status="withdrawn", status_reason="x")), ["WITHDRAWN_USED"], []),
            ("question uses withdrawn claim", lambda: self.rec(QUESTIONS, "Q-0001", lambda r: r["findings"].append("CLM-0005")), ["WITHDRAWN_USED"], []),
            ("calculation uses withdrawn source", lambda: self.rec(CALCS, "CALC-0001", lambda r: r["inputs"][0].update(source_id="SRC-0004")), ["WITHDRAWN_USED"], []),
            ("calculation uses withdrawn calculation", withdrawn_calc_input, ["WITHDRAWN_USED"], []),
            ("outputs text uses withdrawn claim", output("Текст [CLM-0005]\n"), ["WITHDRAWN_USED"], []),
            ("outputs text uses superseded decision", output("Текст [DEC-0001]\n"), ["WITHDRAWN_USED"], []),
            ("outputs link to action", output("Текст [ACT-0001]\n"), ["OUTPUT_LINK"], []),
            ("outputs link to checkpoint", output("Текст [CKP-0001]\n"), ["OUTPUT_LINK"], []),
            ("estimated cost without source", set_end("ACT-0003", cost={"basis": "estimated", "amounts": {"input_tokens": 1}}), ["ACTION_COST"], []),
            ("two end lines", lambda: self.journal(lambda ls: ls.append(dict(self.line(ls, "ACT-0001", "end")))), ["ACTION_ORDER"], []),
            ("source without reason", lambda: self.rec(SOURCES, "SRC-0004", lambda r: r.pop("status_reason")), ["STATUS_NO_REASON"], []),
            ("calculation without reason", no_reason(CALCS, "CALC-0002"), ["STATUS_NO_REASON"], []),
            ("question without reason", no_reason(QUESTIONS, "Q-0002"), ["STATUS_NO_REASON"], []),
            ("option without reason", no_reason(OPTIONS, "OPT-0002"), ["STATUS_NO_REASON"], []),
        ]
        self.each(cases)

    def test_lower_severity_branches(self):
        def clone_decision():
            def add(data):
                other = json.loads(json.dumps(data["records"][1]))
                other.update(id="DEC-0003", supersedes="DEC-0001")
                data["records"].append(other)

            self.edit(DECISIONS, add)

        cases = [
            ("two decisions replace one", clone_decision, ["DECISION_SUPERSEDE"], []),
            ("topic changes in the chain", lambda: self.rec(DECISIONS, "DEC-0002", lambda r: r.update(topic="budget")), ["DECISION_SUPERSEDE"], []),
            ("empty option basis", lambda: self.rec(OPTIONS, "OPT-0001", lambda r: r.update(basis=[])), [], ["BASIS_UNCONFIRMED"]),
            ("retry of itself", lambda: self.journal(lambda ls: self.line(ls, "ACT-0006", "start").update(retry_of="ACT-0006")), ["ACTION_RETRY"], []),
            ("retry_reason without retry_of", lambda: self.journal(lambda ls: self.line(ls, "ACT-0001", "start").update(retry_reason="x")), ["ACTION_RETRY"], []),
            ("blank resolution", lambda: self.rec(CLAIMS, "CLM-0001", lambda r: r["contradicts"][0].update(resolution="   ")), ["CLAIM_CONFLICT_HIDDEN"], []),
            ("human_input with input_refs", lambda: self.journal(lambda ls: self.line(ls, "ACT-0004", "start").update(input_refs=["SRC-0001"])), ["SCHEMA"], []),
        ]
        self.each(cases)

    def test_weak_derivation_through_calculations(self):
        def chain():
            def add(data):
                data["records"].append({
                    "id": "CALC-0003", "created_at": "2026-11-02T10:00:00+03:00", "record_status": "active",
                    "inputs": [{"name": "n", "calculation_id": "CALC-0001", "value": 1, "unit": None, "period": None}],
                    "method": {"kind": "formula", "description": "d"},
                    "result": {"status": "computed", "value": 1, "unit": None, "period": None},
                    "gaps": [], "assumptions": [], "reproduce": "1"})

            self.edit(CALCS, add)
            self.rec(CLAIMS, "CLM-0006", lambda r: r.update(derived_from=["CLM-0002", "CALC-0003"]))

        def source(change):
            return lambda: self.rec(SOURCES, "SRC-0002", change)

        cases = [
            ("input source not allowed", source(lambda r: r.update(allowed=False)), ["CLAIM_WEAK_DERIVATION"], []),
            ("input source unreadable", source(lambda r: r["extraction"].update(status="empty")), ["CLAIM_WEAK_DERIVATION"], []),
            ("input source withdrawn", source(lambda r: r.update(record_status="withdrawn", status_reason="x")), ["CLAIM_WEAK_DERIVATION"], []),
            ("input source partial", source(lambda r: r["extraction"].update(status="partial", complete=False)), [], ["SOURCE_PARTIAL"]),
        ]
        self.each(cases)
        for label, mutate, errors, warnings in cases:
            with self.subTest("through another calculation: " + label):
                self.reset()
                chain()
                mutate()
                self.expect(errors, warnings)
        self.reset()
        chain()
        self.assertEqual(codes(self.run_case()), ([], []))

    def test_output_references(self):
        def output(text, name="outputs/product-strategy.md"):
            def write():
                path = self.case / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(text, encoding="utf-8")

            return write

        cases = [
            ("unknown ID in a comma list", output("См. [CLM-0001, CLM-0099]\n"), ["OUTPUT_LINK"], []),
            ("lower-case ID", output("См. [clm-0001]\n"), ["OUTPUT_LINK"], []),
            ("too short ID", output("См. [CLM-1]\n"), ["OUTPUT_LINK"], []),
            ("withdrawn ID in a comma list", output("См. [CLM-0001, CLM-0005]\n"), ["WITHDRAWN_USED"], []),
            ("nested folder", output("См. [CLM-0099]\n", "outputs/extra/note.md"), ["OUTPUT_LINK"], []),
        ]
        self.each(cases)
        self.reset()
        (self.case / "outputs" / "product-strategy.md").write_text(
            "См. [CLM-0001, CLM-0002] и [обычный текст] и [x](https://example.invalid).\n", encoding="utf-8")
        self.assertEqual(self.run_case().errors, [])

    def test_locations_and_commands(self):
        for url in ("ftp://example.invalid/x", "file:///etc/passwd", "javascript:alert(1)", "example.invalid", "https://"):
            with self.subTest(url):
                self.reset()
                self.rec(SOURCES, "SRC-0003", lambda r: r["location"].update(url=url))
                self.expect(["PATH_UNSAFE"])
        self.reset()
        self.rec(SOURCES, "SRC-0003", lambda r: r.update(source_url="ftp://example.invalid/x"))
        self.expect(["PATH_UNSAFE"])
        for command in ("python /home/user/run.py", "python C:\\work\\run.py", "cat \\\\srv\\share\\x", "run ~/x.py"):
            with self.subTest(command):
                self.reset()
                self.rec(CALCS, "CALC-0001", lambda r: r.update(reproduce=command))
                self.expect(["PATH_UNSAFE"])
        for fine in ("1440 / 12", "a /2", ".venv/Scripts/python.exe tools/x.py"):
            with self.subTest(fine):
                self.reset()
                self.rec(CALCS, "CALC-0001", lambda r: r.update(reproduce=fine))
                self.assertEqual(self.run_case().errors, [])

    def test_schema_extensions(self):
        def source_url():
            self.rec(SOURCES, "SRC-0002", lambda r: r.update(source_url="https://example.invalid/data.csv"))

        def web_locators():
            self.rec(CLAIMS, "CLM-0007", lambda r: r["supports"][0].update(locator={"kind": "paragraph", "index": 2}))
            self.rec(CLAIMS, "CLM-0004", lambda r: r["supports"][0].update(locator={"kind": "doc_table", "table": 1, "row": 2}))

        def range_estimate():
            self.journal(lambda ls: self.line(ls, "ACT-0003", "end")["cost"]["amounts"].update(input_tokens={"min": 4000, "max": 6000}))

        def totals_time():
            self.edit(CKP2, lambda d: d["totals"].update(time_to_first_result_seconds=None))

        def direct_choice():
            self.rec(DECISIONS, "DEC-0002", lambda r: r.update(proposal=None))

        def calculation_input():
            self.rec(CALCS, "CALC-0002", lambda r: r["inputs"].append(
                {"name": "n", "calculation_id": "CALC-0001", "value": None, "unit": None, "period": None}))

        for mutate in (source_url, web_locators, range_estimate, totals_time, direct_choice, calculation_input):
            with self.subTest(mutate.__name__):
                self.reset()
                mutate()
                self.assertEqual(codes(self.run_case()), ([], []))

        def calc_input(target):
            return lambda: self.rec(CALCS, "CALC-0002", lambda r: r["inputs"].append(
                {"name": "n", "calculation_id": target, "value": None, "unit": None, "period": None}))

        def proposed_without_proposal(record):
            record.update(proposal=None, status="proposed")
            record.pop("choice")

        cases = [
            ("range above its maximum", lambda: self.journal(lambda ls: self.line(ls, "ACT-0003", "end")["cost"]["amounts"].update(input_tokens={"min": 9, "max": 1})), ["ACTION_COST"], []),
            ("range in a measured cost", lambda: self.journal(lambda ls: self.line(ls, "ACT-0001", "end")["cost"]["amounts"].update(input_tokens={"min": 1, "max": 2})), ["ACTION_COST"], []),
            ("calculation input of a missing calculation", calc_input("CALC-0099"), ["ID_MISSING"], []),
            ("calculation input of another type", calc_input("SRC-0001"), ["ID_WRONG_TYPE"], []),
            ("proposed decision without proposal", lambda: self.rec(DECISIONS, "DEC-0002", proposed_without_proposal), ["DECISION_CHOICE_STATE"], []),
        ]
        self.each(cases)


if __name__ == "__main__":
    unittest.main()
