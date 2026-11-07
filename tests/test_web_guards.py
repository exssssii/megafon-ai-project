import email.message
import http.client
import io
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import tempfile
import unittest
from unittest import mock
from urllib.error import HTTPError, URLError

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))

import checkpoint as ck  # noqa: E402
import web_fetch as wf  # noqa: E402
import web_guard as guard  # noqa: E402

LONG = "Текст страницы про тарифы и условия. " * 10


class Response(io.BytesIO):
    def __init__(self, body, kind="text/html; charset=utf-8", url="https://example.com/p"):
        super().__init__(body)
        self.headers = email.message.Message()
        self.headers["Content-Type"] = kind
        self.url = url

    def geturl(self):
        return self.url


class Opener:
    def __init__(self, response=None, error=None):
        self.response, self.error = response, error

    def open(self, request, timeout):
        if self.error:
            raise self.error
        return self.response


def public(*_, **__):
    return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443))]


@mock.patch.object(wf.socket, "getaddrinfo", public)
class Fetch(unittest.TestCase):
    def page(self, body, **extra):
        return wf.fetch_page("https://example.com/p", Opener(Response(body, **extra)))

    def code(self, opener, url="https://example.com/p"):
        with self.assertRaises(wf.FetchError) as caught:
            wf.fetch_page(url, opener)
        return caught.exception

    def test_markup_scripts_and_styles_are_removed(self):
        body = (f"<html><head><title>Заголовок</title><style>p{{}}</style></head><body><h2>Цены</h2>"
                f"<p>{LONG}</p><script>alert(1)</script><p>Второй &amp; абзац</p></body></html>").encode("utf-8")
        page = self.page(body)
        self.assertIn("## Цены", page.text)
        self.assertIn("Второй & абзац", page.text)
        for hidden in ("alert", "Заголовок", "p{}"):
            self.assertNotIn(hidden, page.text)
        self.assertTrue(page.complete)
        self.assertEqual(len(page.sha256), 64)

    def test_page_on_scripts_is_too_short(self):
        self.assertEqual(self.code(Opener(Response(b"<div id=app></div><script>run()</script>"))).code, "too_little_text")

    def test_only_text_is_taken(self):
        self.assertEqual(self.code(Opener(Response(b"%PDF", kind="application/pdf"))).code, "unsupported_type")

    def test_page_in_cp1251_without_a_charset(self):
        page = self.page(f"<p>{LONG}</p>".encode("cp1251"), kind="text/html")
        self.assertIn("тарифы", page.text)

    def test_oversized_page_is_cut_and_marked(self):
        with mock.patch.object(wf, "MAX_PAGE_BYTES", 500):
            page = self.page(("x " * 2000).encode("utf-8"), kind="text/plain")
        self.assertFalse(page.complete)

    def test_table_row_stays_one_line(self):
        page = self.page(f"<table><tr><td>Тариф</td><td>100</td></tr></table><p>{LONG}</p>".encode("utf-8"))
        self.assertIn("Тариф | 100\n", page.text)

    def test_page_cut_inside_a_letter_stays_readable(self):
        body = ("Тариф и цена. " * 200).encode("utf-8")
        for size in (1001, 1002):
            with mock.patch.object(wf, "MAX_PAGE_BYTES", size):
                page = self.page(body, kind="text/plain")
            self.assertTrue(page.text.startswith("Тариф и цена."))
            self.assertFalse(page.complete)

    def test_single_broken_byte_does_not_change_the_charset(self):
        body = ("Тариф и цена. " * 200).encode("utf-8") + b"\xff"
        self.assertIn("Тариф", self.page(body).text)

    def test_wrong_declared_charset_loses_to_utf8(self):
        page = self.page(f"<p>{LONG}</p>".encode("utf-8"), kind="text/html; charset=iso-8859-1")
        self.assertIn("тарифы", page.text)

    def test_charset_of_the_meta_tag(self):
        body = f'<meta charset="koi8-r"><p>{LONG}</p>'.encode("koi8-r")
        self.assertIn("тарифы", self.page(body, kind="text/html").text)

    def test_unknown_charset_does_not_break_the_tool(self):
        self.assertIn("тарифы", self.page(f"<p>{LONG}</p>".encode("utf-8"), kind="text/html; charset=undefined").text)

    def test_head_without_a_closing_tag(self):
        page = self.page(f"<head><title>T</title><body><p>{LONG}</p>".encode("utf-8"))
        self.assertIn("тарифы", page.text)
        self.assertTrue(page.complete)

    def test_open_tag_that_hides_text_is_not_called_complete(self):
        page = self.page(f"<p>{LONG}</p><svg><p>Скрытый текст</p>".encode("utf-8"))
        self.assertFalse(page.complete)

    def test_short_body_against_its_length_is_not_called_complete(self):
        response = Response(f"<p>{LONG}</p>".encode("utf-8"))
        response.headers["Content-Length"] = "100000"
        self.assertFalse(wf.fetch_page("https://example.com/p", Opener(response)).complete)

    def test_answer_without_a_type_is_read_as_html(self):
        response = Response(f"<b>x</b><p>{LONG}</p>".encode("utf-8"))
        del response.headers["Content-Type"]
        self.assertNotIn("<b>", wf.fetch_page("https://example.com/p", Opener(response)).text)

    def test_broken_answer_is_a_network_failure(self):
        for error in (http.client.BadStatusLine("x"), ValueError("x")):
            self.assertEqual(self.code(Opener(error=error)).code, "network_unavailable")

    def test_failures_are_named(self):
        self.assertEqual(self.code(Opener(error=URLError("tls"))).code, "network_unavailable")
        failure = self.code(Opener(error=HTTPError("https://example.com/p", 404, "x", email.message.Message(), None)))
        self.assertEqual((failure.code, failure.status), ("http_error", 404))


class Addresses(unittest.TestCase):
    def test_national_letters_and_spaces_are_encoded(self):
        self.assertEqual(wf.normalized("https://ru.wikipedia.org/wiki/Тариф плана?q=цена#x"),
                         "https://ru.wikipedia.org/wiki/%D0%A2%D0%B0%D1%80%D0%B8%D1%84%20%D0%BF%D0%BB%D0%B0%D0%BD%D0%B0"
                         "?q=%D1%86%D0%B5%D0%BD%D0%B0")
        self.assertEqual(wf.normalized("https://example.com/a%20b"), "https://example.com/a%20b")
        self.assertEqual(wf.normalized("https://сайт.рф/"), "https://xn--80aswg.xn--p1ai/")

    def test_broken_address_is_refused(self):
        for url in ("https://example.com:99999/", "https://[/", "ftp://example.com/"):
            with self.assertRaises(wf.FetchError) as caught:
                wf.normalized(url)
            self.assertEqual(caught.exception.code, "address_blocked")

    def test_embedded_and_multicast_addresses_are_not_public(self):
        for address in ("::127.0.0.1", "64:ff9b::7f00:1", "224.0.0.1", "::ffff:127.0.0.1", "::1"):
            self.assertFalse(wf.is_public(address), address)
        self.assertTrue(wf.is_public("93.184.216.34"))

    def test_connection_checks_the_answer_it_uses(self):
        found = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 443))]
        with mock.patch.object(wf.socket, "getaddrinfo", return_value=found), \
                mock.patch.object(wf.socket, "create_connection") as connect:
            with self.assertRaises(wf.FetchError):
                wf.PublicHTTPS("rebound.example")._create_connection(("rebound.example", 443))
        connect.assert_not_called()

    def test_certificates_are_checked(self):
        with mock.patch.object(wf.PublicHTTPSHandler, "do_open") as opened:
            wf.PublicHTTPSHandler().https_open(wf.Request("https://example.com/"))
        context = opened.call_args.kwargs["context"]
        self.assertEqual(context.verify_mode, wf.ssl.CERT_REQUIRED)
        self.assertTrue(context.check_hostname)

    def test_connection_goes_to_the_checked_address(self):
        with mock.patch.object(wf.socket, "getaddrinfo", public), \
                mock.patch.object(wf.socket, "create_connection") as connect:
            wf.connect_public(("example.com", 443))
        self.assertEqual(connect.call_args.args[0], ("93.184.216.34", 443))

    def blocked(self, url, address):
        found = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (address, 443))]
        with mock.patch.object(wf.socket, "getaddrinfo", return_value=found):
            with self.assertRaises(wf.FetchError) as caught:
                wf.check_public(url)
        return caught.exception.code

    def test_local_and_private_addresses_are_refused(self):
        for address in ("127.0.0.1", "10.0.0.5", "192.168.1.1", "169.254.169.254"):
            self.assertEqual(self.blocked("https://inner.example/", address), "address_blocked")

    def test_address_with_a_password_is_refused(self):
        with self.assertRaises(wf.FetchError) as caught:
            wf.normalized("https://user:pass@example.com/")
        self.assertEqual(caught.exception.code, "address_blocked")

    def test_redirect_to_a_private_address_is_refused(self):
        handler = wf.CheckedRedirects()
        request = wf.Request("https://example.com/")
        with mock.patch.object(wf.socket, "getaddrinfo",
                               return_value=[(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 80))]):
            with self.assertRaises(wf.FetchError):
                handler.redirect_request(request, None, 302, "", {}, "http://localhost/admin")


class Guard(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.projects = self.tmp / "projects"
        self.case = self.projects / "case-one"
        (self.case / "state").mkdir(parents=True)
        (self.case / "case.json").write_text(json.dumps({
            "format_version": 1, "case_id": "case-one", "title": "Кейс", "created_at": "2026-11-06T10:00:00+03:00"}),
            encoding="utf-8")

    def journal(self, *lines):
        (self.case / "state" / "actions.jsonl").write_text(
            "".join(json.dumps(line) + "\n" for line in lines), encoding="utf-8")

    @staticmethod
    def start(kind="search", number=1):
        return {"format_version": 1, "id": f"ACT-{number:04d}", "phase": "start", "kind": kind, "tool": "t",
                "environment": "claude_code", "model": None, "input_refs": [], "at": "2026-11-06T10:00:00+03:00"}

    @staticmethod
    def end(number=1):
        return {"format_version": 1, "id": f"ACT-{number:04d}", "phase": "end", "outcome": "completed",
                "result_refs": [], "cost": {"basis": "unknown"}, "at": "2026-11-06T10:01:00+03:00"}

    def denied(self, tool="WebSearch"):
        return guard.denied({"tool_name": tool}, self.projects)

    def test_no_case_allows_the_call(self):
        shutil.rmtree(self.projects)
        self.assertIsNone(self.denied())

    def test_call_without_a_record_is_denied(self):
        self.journal(self.start("file_read"), self.end())
        self.assertIn("--kind search", self.denied())

    def test_open_action_of_the_same_kind_allows_the_call(self):
        self.journal(self.start("search"))
        self.assertIsNone(self.denied())
        self.assertIn("--kind web_read", self.denied("WebFetch"))

    def test_finished_action_does_not_allow_another_call(self):
        self.journal(self.start("search"), self.end())
        self.assertIsNotNone(self.denied())

    def test_open_action_in_any_fresh_case_allows_the_call(self):
        self.journal(self.start("web_read"))
        other = self.projects / "case-two"
        shutil.copytree(self.case, other)
        (other / "state" / "actions.jsonl").write_text(
            json.dumps(self.start("file_read")) + "\n" + json.dumps(self.end()) + "\n", encoding="utf-8")
        self.assertIsNone(self.denied("WebFetch"))
        self.assertIn("projects/<case-id>", self.denied("WebSearch"))

    def test_answer_gives_a_command_that_runs(self):
        self.journal(self.start("file_read"), self.end())
        reason = self.denied("WebFetch")
        self.assertIn("projects/case-one --kind web_read --tool WebFetch --environment claude_code", reason)

    def test_old_case_does_not_stop_other_work(self):
        self.journal(self.start("file_read"), self.end())
        old = guard.time.time() - guard.ACTIVE_SECONDS - 60
        os.utime(self.case / "state" / "actions.jsonl", (old, old))
        self.assertIsNone(self.denied())

    def test_finished_case_allows_the_call(self):
        self.journal(self.start("file_read"), self.end())
        with mock.patch.object(guard, "is_finished", return_value=True):
            self.assertIsNone(self.denied())

    def test_real_end_of_case_is_read_from_the_checkpoint(self):
        folder = self.case / "state" / "checkpoints"
        folder.mkdir()
        (folder / "CKP-0001.json").write_text("{}", encoding="utf-8")
        with mock.patch.object(guard, "read_checkpoint", return_value={"stage": "done"}):
            self.assertTrue(guard.is_finished(self.case))
        self.assertFalse(guard.is_finished(self.projects / "absent"))

    def test_switch_for_development(self):
        self.journal(self.start("file_read"), self.end())
        with mock.patch.dict(os.environ, {"CASE_WEB_GUARD": "off"}):
            self.assertIsNone(self.denied())

    def test_other_tools_pass(self):
        self.journal(self.start("file_read"), self.end())
        self.assertIsNone(self.denied("Read"))

    def run_hook(self, stdin):
        return subprocess.run([sys.executable, str(ROOT / "tools" / "web_guard.py")], input=stdin,
                              capture_output=True, cwd=self.tmp)

    def test_broken_input_lets_the_call_through(self):
        result = self.run_hook(b"not json")
        self.assertEqual((result.returncode, result.stdout), (0, b""))


if __name__ == "__main__":
    unittest.main()
