import contextlib
import http.server
import io
import json
from pathlib import Path
import socket
import sys
import threading
import time
import types
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))
sys.path.insert(0, str(ROOT / "tests"))

import capture_proxy  # noqa: E402
import checkpoint  # noqa: E402
import image_bytes  # noqa: E402
import image_header  # noqa: E402
import material_extract  # noqa: E402
import read_material  # noqa: E402
import web_capture  # noqa: E402

from test_read_material import Base, OK, REFUSED  # noqa: E402

PART = image_bytes.png_bytes(1366, 768)
FINAL = "https://example.com/final"


def shot_of(parts, total=None):
    return web_capture.Capture(FINAL, [PART] * parts, total or parts)


class CaptureCli(Base):
    URL = "https://example.com/p"

    def setUp(self):
        super().setUp()
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(checkpoint.main(["action", "start", str(self.case), "--kind", "web_read",
                                              "--tool", "capture-web", "--environment", "claude_code"]), 0)

    def brief(self, text):
        (self.case / "brief.md").write_text(f"# Бриф\n\n## Бюджет исследования\n\n{text}\n", encoding="utf-8")

    def capture(self, shot=None, error=None, available=True, *extra):
        with mock.patch.object(web_capture, "available", return_value=available), \
                mock.patch.object(web_capture, "capture", return_value=shot, side_effect=error) as self.capturer:
            return self.run_tool("capture-web", self.case, "--url", self.URL, "--title", "Сайт", *extra)

    def capture_pages(self, total, *extra):
        """A page that needs `total` screens: the stub returns as many as the caller allows."""
        def stub(url, mobile, max_parts, timeout):
            return shot_of(min(total, max_parts), total)
        return self.capture(None, stub, True, *extra)

    def report_of(self, source_id):
        path = self.case / "sources" / "extracted" / f"{source_id}.report.json"
        return json.loads(path.read_text(encoding="utf-8"))

    def capture_files(self):
        return sorted(p.name for p in (self.case / "sources").rglob("*") if p.is_file())

    def test_capture_unavailable_lists_install_commands(self):
        code, report, _ = self.capture(shot_of(1), available=False)
        self.assertEqual((code, report["refused"]), (REFUSED, "capture_unavailable"))
        self.assertEqual(len(report["install"]), 2)
        self.assertIn("--group screenshots", report["install"][0])
        self.assertIn("playwright install chromium", report["install"][1])
        self.capturer.assert_not_called()
        self.assertFalse((self.case / "evidence" / "sources.json").exists())

    def test_capture_requires_open_action(self):
        (self.case / "state" / "actions.jsonl").unlink()
        code, report, _ = self.capture(shot_of(1))
        self.assertEqual((code, report["refused"]), (REFUSED, "action_missing"))
        self.capturer.assert_not_called()

    def test_capture_requires_a_web_address(self):
        code, _, _ = self.run_tool("capture-web", self.case, "--url", "ftp://example.com", "--title", "x")
        self.assertEqual(code, REFUSED)

    def test_capture_writes_parts_atomically(self):
        code, report, _ = self.capture(shot_of(3))
        self.assertEqual((code, report["parts"], report["total_parts"], report["final_url"]), (OK, 3, 3, FINAL))
        self.assertEqual(report["ids"], ["SRC-0001", "SRC-0002", "SRC-0003"])
        records = self.sources()
        self.assertEqual([r["title"] for r in records], [f"Сайт, экран {i} из 3" for i in (1, 2, 3)])
        self.assertEqual(len({r["retrieved_at"] for r in records}), 1)
        for record in records:
            self.assertEqual((record["type"], record["origin"], record["allowed"], record["source_url"]),
                             ("image", "public_web", True, FINAL))
            self.assertEqual(record["location"], {"path": f"sources/capture-{record['id']}.png"})
            self.assertEqual(record["extraction"]["status"], "partial")
            self.assertIn("PNG 1366×768", record["extraction"]["limits"])
            self.assertEqual(self.blocks(record["id"]), [])
        self.assertEqual(self.report_of("SRC-0003").get("capture_incomplete"), None)
        report = self.validate()
        self.assertEqual(report["errors"], [])
        # the open action of the test itself is the only standing warning
        self.assertEqual([w["code"] for w in report["warnings"]], ["ACTION_UNKNOWN"])

    def test_failure_on_the_third_part_registers_nothing(self):
        real = read_material.Case.write_extraction
        calls = []

        def failing(case, source_id, result):
            calls.append(source_id)
            if len(calls) == 3:
                raise OSError("disk full")
            return real(case, source_id, result)

        with mock.patch.object(read_material.Case, "write_extraction", failing):
            code, report, _ = self.capture(shot_of(3))
        self.assertEqual((code, report["status"]), (5, "internal_error"))
        self.assertEqual(self.sources(), [])
        self.assertEqual(self.capture_files(), [])
        code, report, _ = self.capture(shot_of(3))
        self.assertEqual((code, report["ids"]), (OK, ["SRC-0001", "SRC-0002", "SRC-0003"]))

    def test_capture_budget_trims_parts(self):
        self.brief("1 поиск, 1 чтение страницы, 2 источника")
        code, report, _ = self.capture_pages(5)
        self.assertEqual((code, report["parts"], report["total_parts"]), (OK, 2, 5))
        self.assertEqual(self.capturer.call_args.kwargs["max_parts"], 2)
        first, last = self.sources()
        self.assertEqual(last["title"], "Сайт, экран 2 из 5")
        self.assertTrue(last["extraction"]["limits"].startswith("Снято 2 из 5 экранов."), last["extraction"]["limits"])
        self.assertEqual((last["extraction"]["status"], last["extraction"]["complete"]), ("partial", False))
        self.assertEqual(self.report_of(last["id"])["capture_incomplete"], "Снято 2 из 5 экранов.")
        self.assertNotIn("capture_incomplete", self.report_of(first["id"]))
        self.assertEqual(self.validate()["errors"], [])

    def test_capture_part_limit_comes_from_input_limits(self):
        code, report, _ = self.capture_pages(9)
        self.assertEqual((code, report["parts"]), (OK, material_extract.load_limits()["max_capture_parts"]))
        self.assertEqual(self.capturer.call_args.kwargs["max_parts"], 4)
        self.assertEqual(self.capturer.call_args.kwargs["timeout"], 30)
        self.assertIn("экран 4 из 9", self.sources()[-1]["title"])

    def test_capture_budget_exhausted(self):
        self.brief("1 поиск, 1 чтение страницы, 0 источников")
        code, report, _ = self.capture(shot_of(1))
        self.assertEqual((code, report["refused"]), (REFUSED, "budget_exhausted"))
        self.capturer.assert_not_called()
        self.assertFalse((self.case / "evidence" / "sources.json").exists())

    def test_earlier_captures_spend_the_budget(self):
        self.brief("1 поиск, 1 чтение страницы, 3 источника")
        self.assertEqual(self.capture(shot_of(2))[0], OK)
        self.capture_pages(5)
        self.assertEqual(self.capturer.call_args.kwargs["max_parts"], 1)
        self.assertEqual(self.capture(shot_of(1))[1]["refused"], "budget_exhausted")

    def test_capture_error_codes_map(self):
        for code in ("network_unavailable", "address_blocked", "http_error", "timeout", "blank_page"):
            with self.subTest(code):
                status = 503 if code == "http_error" else None
                exit_code, report, _ = self.capture(error=web_capture.CaptureError(code, status))
                self.assertEqual((exit_code, report["fetch_error"], report["http_status"]), (REFUSED, code, status))
        self.assertFalse((self.case / "evidence" / "sources.json").exists())
        self.assertEqual(self.capture_files(), [])

    def test_unavailable_browser_found_late_is_the_same_refusal(self):
        code, report, _ = self.capture(error=web_capture.CaptureError("capture_unavailable"))
        self.assertEqual((code, report["refused"], len(report["install"])), (REFUSED, "capture_unavailable", 2))

    def test_repeat_capture_makes_new_sources(self):
        self.capture(shot_of(2))
        code, report, _ = self.capture(shot_of(2))
        self.assertEqual((code, report["ids"]), (OK, ["SRC-0003", "SRC-0004"]))
        self.assertEqual(len(self.sources()), 4)
        self.assertEqual(len({r["location"]["path"] for r in self.sources()}), 4)
        self.assertEqual(self.validate()["errors"], [])

    def test_mobile_flag_reaches_the_capture(self):
        self.capture(shot_of(1), None, True, "--mobile")
        self.assertIs(self.capturer.call_args.kwargs["mobile"], True)
        self.capture(shot_of(1))
        self.assertIs(self.capturer.call_args.kwargs["mobile"], False)

    def test_unreadable_picture_from_the_browser_registers_nothing(self):
        broken = web_capture.Capture(FINAL, [PART, b"not a picture"], 2)
        code, report, _ = self.capture(broken)
        self.assertEqual(code, REFUSED)
        self.assertEqual(self.sources(), [])
        self.assertEqual(self.capture_files(), [])


class Guard(unittest.TestCase):
    def test_route_guard_blocks_private(self):
        blocked = ("http://127.0.0.1/", "http://10.0.0.1/", "http://192.168.1.1:8080/x", "http://[::1]/",
                   "http://169.254.169.254/latest", "http://localhost/", "file:///etc/passwd", "ftp://example.com/",
                   "http:///nohost")
        for url in blocked:
            with self.subTest(url):
                self.assertFalse(web_capture.request_allowed(url))
        for url in ("data:text/html,<p>x</p>", "blob:https://example.com/1", "about:blank", "http://93.184.216.34/",
                    "https://8.8.8.8/x"):
            with self.subTest(url):
                self.assertTrue(web_capture.request_allowed(url))

    def test_install_commands(self):
        first, second = web_capture.install_commands()
        self.assertEqual(first, "uv sync --locked --group screenshots")
        self.assertTrue(second.endswith("-m playwright install chromium"))
        self.assertIn(".venv", second)

    def test_unavailable_without_the_package(self):
        with mock.patch.dict(sys.modules, {"playwright": None, "playwright.sync_api": None}):
            self.assertFalse(web_capture.available())
            with self.assertRaises(web_capture.CaptureError) as caught:
                web_capture.capture("https://93.184.216.34/", False, 1, 5)
        self.assertEqual(caught.exception.code, "capture_unavailable")

    def test_private_address_is_refused_before_a_browser_starts(self):
        for url in ("http://127.0.0.1:9/", "http://10.1.2.3/"):
            with self.subTest(url):
                with mock.patch.dict(sys.modules, {"playwright": None, "playwright.sync_api": None}):
                    with self.assertRaises(web_capture.CaptureError) as caught:
                        web_capture.capture(url, False, 1, 5)
                self.assertEqual(caught.exception.code, "address_blocked")


class RedirectChain(unittest.TestCase):
    def response(self, *urls):
        """A response to the last url, reached through the earlier ones."""
        request = None
        for url in urls:
            request = types.SimpleNamespace(url=url, redirected_from=request)
        return types.SimpleNamespace(request=request)

    def test_every_hop_and_the_final_address_are_checked(self):
        chain = ("https://93.184.216.34/a", "https://93.184.216.34/b", "https://93.184.216.34/c")
        self.assertIsNone(web_capture.chain_problem(self.response(*chain), chain[-1]))
        for hop in range(3):
            urls = list(chain)
            urls[hop] = "http://10.0.0.5/inner"
            with self.subTest(hop=hop):
                self.assertEqual(web_capture.chain_problem(self.response(*urls), chain[-1]), "address_blocked")
        self.assertEqual(web_capture.chain_problem(self.response(*chain), "http://127.0.0.1/"), "address_blocked")

    def test_private_hosts_are_not_checked_for_local_tests(self):
        self.assertIsNone(web_capture.chain_problem(self.response("http://127.0.0.1/a"), "http://127.0.0.1/a", True))


class Pages(http.server.BaseHTTPRequestHandler):
    hits: list[str] = []

    def pages(self):
        port = self.server.server_address[1]
        return {
            "/long": "<html><body style='margin:0'><div style='height:3000px'>Тестовая страница с текстом</div></body></html>",
            "/blank": "<html><body style='margin:0'></body></html>",
            "/framed": "<html><body style='margin:0'><iframe src='/long' style='width:300px;height:200px'></iframe></body></html>",
            "/hidden-frame": "<html><body><iframe src='/long' style='display:none'></iframe></body></html>",
            "/embedded": "<html><body style='margin:0'><object data='/long' style='width:300px;height:200px'></object></body></html>",
            "/secret": "<html><body>Скрытая страница</body></html>",
            "/with-image": "<html><body>Страница с картинкой<img src='/secret.png'></body></html>",
            "/opener": "<html><body>Страница с окном<script>window.open('/popup-target')</script></body></html>",
            "/with-socket": ("<html><body>Страница с сокетом<script>try { new WebSocket('ws://127.0.0.1:%d/socket') }"
                             " catch (error) {}</script></body></html>" % port),
            "/hang": ("<html><body>Страница зависает<script>setTimeout(function () { for (;;) {} }, 300)"
                      "</script></body></html>"),
        }

    def do_GET(self):
        self.hits.append(self.path)
        if self.path == "/to-inner":
            self.send_response(302)
            self.send_header("Location", "http://inner.example:%d/secret" % self.server.server_address[1])
            self.end_headers()
            return
        body = self.pages().get(self.path)
        self.send_response(200 if body else 404)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write((body or "<html><body>Нет такой страницы</body></html>").encode("utf-8"))

    def log_message(self, *args):
        pass


def blocks_secret(url):
    return "address_blocked" if "/secret" in url or "/popup-target" in url else None


def names(**known):
    """An address rule for the proxy: listed names go to their address, any other is refused.

    Names under `.example` have no answer in real DNS, so a page reached by such a name proves the
    browser asked the proxy and did not resolve the name itself.
    """
    def rule(host, port, allow_private=False):
        address = known.get(host.replace(".", "_"))
        if address is None:
            raise capture_proxy.FetchError("address_blocked")
        return address
    return mock.patch.object(web_capture, "address_for", rule)


class LocalServer:
    @classmethod
    def setUpClass(cls):
        cls.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Pages)
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()
        cls.port = cls.server.server_address[1]
        cls.base = f"http://127.0.0.1:{cls.port}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def setUp(self):
        Pages.hits.clear()


class Proxy(LocalServer, unittest.TestCase):
    """The proxy alone, without a browser: these run in CI."""

    def proxy(self, resolve):
        proxy = capture_proxy.GuardProxy(resolve, 5).start()
        self.addCleanup(proxy.stop)
        host, port = proxy.server.removeprefix("http://").split(":")
        return proxy, (host, int(port))

    def exchange(self, address, data):
        with socket.create_connection(address, 5) as client:
            client.sendall(data)
            received, tunnel_used = b"", False
            while chunk := client.recv(65536):
                received += chunk
                if data.startswith(b"CONNECT") and not tunnel_used and received.endswith(b"\r\n\r\n"):
                    tunnel_used = True
                    client.sendall(b"GET /secret HTTP/1.1\r\nHost: x\r\nConnection: close\r\n\r\n")
        return received

    def only_local(self, host, port):
        if host != "site.example":
            raise capture_proxy.FetchError("address_blocked")
        return "127.0.0.1"

    def test_tunnel_goes_to_the_address_the_rule_returned(self):
        proxy, address = self.proxy(self.only_local)
        received = self.exchange(address, b"CONNECT site.example:%d HTTP/1.1\r\n\r\n" % self.port)
        self.assertIn("Скрытая страница".encode(), received)
        self.assertEqual((Pages.hits, proxy.refused), (["/secret"], []))

    def test_refused_tunnel_gets_no_connection(self):
        proxy, address = self.proxy(self.only_local)
        received = self.exchange(address, b"CONNECT inner.example:%d HTTP/1.1\r\n\r\n" % self.port)
        self.assertEqual((received, Pages.hits, proxy.refused), (b"", [], ["address_blocked"]))

    def test_plain_request_goes_on_in_origin_form(self):
        proxy, address = self.proxy(self.only_local)
        received = self.exchange(address, b"GET http://site.example:%d/secret HTTP/1.1\r\nHost: site.example\r\n"
                                          b"Proxy-Connection: keep-alive\r\n\r\n" % self.port)
        self.assertTrue(received.startswith(b"HTTP/1.0 200"))
        self.assertEqual(Pages.hits, ["/secret"])

    def test_refused_plain_request_gets_no_connection(self):
        proxy, address = self.proxy(self.only_local)
        for request in (b"GET http://inner.example:%d/secret HTTP/1.1\r\n\r\n" % self.port,
                        b"GET ftp://site.example/secret HTTP/1.1\r\n\r\n"):
            with self.subTest(request):
                self.assertEqual(self.exchange(address, request), b"")
        self.assertEqual((Pages.hits, proxy.refused), ([], ["address_blocked", "address_blocked"]))

    def test_name_that_now_answers_an_inner_address_is_refused(self):
        """DNS rebinding: whatever the name answered before, the proxy checks the answer it connects to."""
        real = socket.getaddrinfo

        def lookup(host, port, *args, **kwargs):
            if host == "rebind.example":
                return [(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", ("127.0.0.1", port))]
            return real(host, port, *args, **kwargs)

        proxy, address = self.proxy(lambda host, port: web_capture.address_for(host, port))
        with mock.patch.object(socket, "getaddrinfo", lookup):
            received = self.exchange(address, b"CONNECT rebind.example:%d HTTP/1.1\r\n\r\n" % self.port)
        self.assertEqual((received, Pages.hits, proxy.refused), (b"", [], ["address_blocked"]))

    def test_stop_closes_open_tunnels(self):
        proxy, address = self.proxy(self.only_local)
        client = socket.create_connection(address, 5)
        self.addCleanup(client.close)
        client.sendall(b"CONNECT site.example:%d HTTP/1.1\r\n\r\n" % self.port)
        self.assertIn(b"200", client.recv(1024))
        proxy.stop()
        self.assertEqual(client.recv(1024), b"")


@unittest.skipUnless(web_capture.available(), "Playwright and its browser are not installed")
class RealCapture(LocalServer, unittest.TestCase):
    def shoot(self, path, mobile=False, max_parts=10):
        return web_capture.capture(self.base + path, mobile, max_parts, 30, allow_private=True)

    def test_real_capture_local_page(self):
        shot = self.shoot("/long")
        self.assertEqual((len(shot.parts), shot.total_parts), (4, 4))
        sizes = [image_header.read_header(part) for part in shot.parts]
        self.assertEqual([(i.format, i.width) for i in sizes], [("png", 1366)] * 4)
        self.assertEqual([i.height for i in sizes], [768, 768, 768, 696])

    def test_real_capture_respects_the_part_limit_and_mobile_size(self):
        shot = self.shoot("/long", mobile=True, max_parts=2)
        self.assertEqual((len(shot.parts), shot.total_parts), (2, 4))
        info = image_header.read_header(shot.parts[0])
        self.assertEqual((info.width, info.height), (390, 844))

    def test_real_capture_errors(self):
        with self.assertRaises(web_capture.CaptureError) as caught:
            self.shoot("/missing")
        self.assertEqual((caught.exception.code, caught.exception.http_status), ("http_error", 404))
        with self.assertRaises(web_capture.CaptureError) as caught:
            self.shoot("/blank")
        self.assertEqual(caught.exception.code, "blank_page")

    def test_blank_detection_counts_visible_frames_and_objects(self):
        for path in ("/framed", "/embedded"):
            with self.subTest(path):
                self.assertEqual(len(self.shoot(path).parts), 1)
        with self.assertRaises(web_capture.CaptureError) as caught:
            self.shoot("/hidden-frame")
        self.assertEqual(caught.exception.code, "blank_page")

    def test_browser_resolves_names_only_through_the_proxy(self):
        with names(site_example="127.0.0.1"), mock.patch.object(web_capture, "verdict", lambda url: None):
            shot = web_capture.capture(f"http://site.example:{self.port}/long", False, 10, 30)
        self.assertEqual(len(shot.parts), 4)

    def test_redirect_to_an_inner_name_never_connects(self):
        with names(site_example="127.0.0.1"), mock.patch.object(web_capture, "verdict", lambda url: None):
            with self.assertRaises(web_capture.CaptureError) as caught:
                web_capture.capture(f"http://site.example:{self.port}/to-inner", False, 1, 30)
        self.assertEqual(caught.exception.code, "address_blocked")
        self.assertEqual(Pages.hits, ["/to-inner"])

    def test_loopback_does_not_bypass_the_proxy(self):
        with names(), mock.patch.object(web_capture, "verdict", lambda url: None):
            with self.assertRaises(web_capture.CaptureError) as caught:
                web_capture.capture(self.base + "/long", False, 1, 30)
        self.assertEqual(caught.exception.code, "address_blocked")
        self.assertEqual(Pages.hits, [])

    def test_blocked_subresource_is_aborted_but_the_page_is_captured(self):
        with names(**{"127_0_0_1": "127.0.0.1"}), mock.patch.object(web_capture, "verdict", blocks_secret):
            shot = web_capture.capture(self.base + "/with-image", False, 1, 30)
        self.assertEqual(len(shot.parts), 1)
        self.assertNotIn("/secret.png", Pages.hits)

    def test_popup_does_not_load(self):
        with names(**{"127_0_0_1": "127.0.0.1"}), mock.patch.object(web_capture, "verdict", lambda url: None):
            shot = web_capture.capture(self.base + "/opener", False, 1, 30)
        self.assertEqual(len(shot.parts), 1)
        self.assertNotIn("/popup-target", Pages.hits)

    def test_web_socket_is_not_connected(self):
        shot = self.shoot("/with-socket")
        self.assertEqual(len(shot.parts), 1)
        self.assertNotIn("/socket", Pages.hits)

    def test_watchdog_stops_a_page_that_hangs_the_renderer(self):
        started = time.monotonic()
        with mock.patch.object(web_capture, "WATCHDOG_MARGIN", 1):
            with self.assertRaises(web_capture.CaptureError) as caught:
                web_capture.capture(self.base + "/hang", False, 1, 3, allow_private=True)
        self.assertEqual(caught.exception.code, "timeout")
        self.assertLess(time.monotonic() - started, 30)


    def test_real_capture_refuses_a_private_address(self):
        with self.assertRaises(web_capture.CaptureError) as caught:
            web_capture.capture(self.base + "/long", False, 1, 30)
        self.assertEqual(caught.exception.code, "address_blocked")


if __name__ == "__main__":
    unittest.main()
