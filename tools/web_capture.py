"""Take screenshots of one public page in a headless browser, with no model in between.

Used by `tools/read_material.py capture-web`. The browser is an optional dependency group
(`screenshots`): without it `available()` is false and nothing here installs or downloads
anything. The page is only opened and looked at: no clicks, no consent to cookies, no input,
no scrolling to load more. Only public http and https addresses are opened. The browser goes to
the network only through the local proxy of `capture_proxy.py` and never resolves a name itself:
the proxy resolves each name once with the address rule of `fetch-web` and connects to exactly
that address. So a redirect to an inner address gets no connection, and a name that changes its
answer between two lookups (DNS rebinding) cannot lead the browser elsewhere. The request guard
and the check of the redirect chain after the navigation stay as a second line.
"""
from dataclasses import dataclass
import math
from pathlib import Path
import socket
import subprocess
import sys
import threading
from urllib.parse import urlsplit

from capture_proxy import GuardProxy
from web_fetch import FetchError, check_public, normalized, public_address

DESKTOP = (1366, 768)
MOBILE = (390, 844)
MOBILE_AGENT = ("Mozilla/5.0 (Linux; Android 13; Pixel 7) AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Mobile Safari/537.36")
SETTLE_SECONDS = 5
WATCHDOG_MARGIN = 5  # seconds added to the capture timeout before the browser is stopped by force
LOCAL_SCHEMES = ("data", "blob", "about")
# The proxy carries TCP only: WebRTC must not open UDP to an address past it.
BROWSER_ARGS = ["--force-webrtc-ip-handling-policy=disable_non_proxied_udp"]
HEIGHT_SCRIPT = ("Math.max(document.documentElement ? document.documentElement.scrollHeight : 0, "
                 "document.body ? document.body.scrollHeight : 0)")
# A page is blank when it shows no text and no picture-like or embedded element that takes up room.
BLANK_SCRIPT = """() => {
  const text = ((document.body && document.body.innerText) || '').trim();
  if (text.length > 0) return false;
  for (const element of document.querySelectorAll('img, svg, canvas, video, iframe, embed, object')) {
    const box = element.getBoundingClientRect();
    if (box.width > 0 && box.height > 0) return false;
  }
  return true;
}"""


class CaptureError(Exception):
    """The page was not captured; `code` tells the procedure what to do next."""

    def __init__(self, code: str, http_status: int | None = None):
        super().__init__(code)
        self.code, self.http_status = code, http_status


@dataclass
class Capture:
    final_url: str
    parts: list[bytes]
    total_parts: int


def install_commands() -> list[str]:
    python = ".venv/Scripts/python.exe" if sys.platform == "win32" else ".venv/bin/python"
    return ["uv sync --locked --group screenshots", f"{python} -m playwright install chromium"]


def available() -> bool:
    """The package is importable and its browser is on disk. Nothing is installed or downloaded."""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return False
    try:
        with sync_playwright() as playwright:
            return Path(playwright.chromium.executable_path).exists()
    except Exception:
        return False


def verdict(url: str) -> str | None:
    """None when the address may be opened, else the code of the refusal."""
    parts = urlsplit(url)
    if parts.scheme in LOCAL_SCHEMES:
        return None
    if parts.scheme not in ("http", "https") or not parts.hostname:
        return "address_blocked"
    try:
        check_public(url)
    except (FetchError, ValueError) as error:
        return getattr(error, "code", "address_blocked")
    return None


def request_allowed(url: str) -> bool:
    return verdict(url) is None


def address_for(host: str, port: int, allow_private: bool = False) -> str:
    """The one address the proxy connects to for this name, or `FetchError` with the refusal code."""
    if not allow_private:
        return public_address(host, port)
    try:
        return socket.getaddrinfo(host, port, proto=socket.IPPROTO_TCP)[0][4][0]
    except (OSError, UnicodeError, IndexError):
        raise FetchError("network_unavailable") from None


def capture(url: str, mobile: bool, max_parts: int, timeout: float, allow_private: bool = False) -> Capture:
    """First screen and the following ones by the height of the window, at most `max_parts`.

    `allow_private` exists for tests against a local server: the command never passes it.
    """
    try:
        target = normalized(url)
    except FetchError as error:
        raise CaptureError(error.code) from None
    if not allow_private:
        problem = verdict(target)
        if problem:
            raise CaptureError(problem)
    try:
        from playwright.sync_api import Error as BrowserError
        from playwright.sync_api import TimeoutError as BrowserTimeout
        from playwright.sync_api import sync_playwright
    except ImportError:
        raise CaptureError("capture_unavailable") from None
    width, height = MOBILE if mobile else DESKTOP
    expired = threading.Event()
    # The name is looked up in this module at call time, so tests can give their own rule.
    proxy = GuardProxy(lambda host, port: address_for(host, port, allow_private), timeout).start()
    manager = sync_playwright()
    watchdog = threading.Timer(timeout + WATCHDOG_MARGIN, _expire, (expired, manager))
    watchdog.daemon = True
    watchdog.start()
    try:
        playwright = manager.start()
        try:
            # Playwright adds `<-loopback>` to the bypass list itself, so loopback goes through the proxy too.
            browser = playwright.chromium.launch(proxy={"server": proxy.server}, args=BROWSER_ARGS)
        except BrowserError:
            raise CaptureError("timeout" if expired.is_set() else "capture_unavailable") from None
        try:
            return _shoot(browser, target, mobile, (width, height), max_parts, timeout, allow_private,
                          proxy.refused, BrowserError, BrowserTimeout)
        finally:
            try:
                browser.close()
            except Exception:
                pass
    except CaptureError as error:
        if expired.is_set() and error.code == "network_unavailable":
            raise CaptureError("timeout") from None
        raise
    except Exception:
        if expired.is_set():
            raise CaptureError("timeout") from None
        raise
    finally:
        watchdog.cancel()
        proxy.stop()
        try:
            if expired.is_set():  # the killed driver leaves an error nobody asks for
                manager._client._transport.on_error_future.exception()
        except Exception:
            pass
        try:
            manager.__exit__(None, None, None)
        except Exception:
            pass


def _expire(expired: threading.Event, manager) -> None:
    """The capture ran out of time: stop the browser by force so that the blocked call fails.

    A page that spins in a loop blocks `page.evaluate`, which has no timeout of its own, so the
    watchdog works from outside. The Playwright driver and the browser under it are killed; the
    pending call raises and `capture` reports `timeout`. The driver process is reached through
    private attributes of the pinned Playwright 1.65; without them the per-call timeouts remain.
    """
    expired.set()
    try:
        pid = manager._client._transport._proc.pid
        if sys.platform == "win32":
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(pid)], capture_output=True, timeout=15)
        else:
            import os
            import signal
            os.kill(pid, signal.SIGKILL)
    except Exception:
        pass


def chain_problem(response, final_url: str, allow_private: bool = False) -> str | None:
    """Code of the refusal when any hop of the redirect chain, or the final address, is not public."""
    if allow_private:
        return None
    request = response.request if response is not None else None
    urls = []
    while request is not None:
        urls.append(request.url)
        request = request.redirected_from
    for url in [*reversed(urls), final_url]:
        problem = verdict(url)
        if problem:
            return problem
    return None


def _refusal(blocked: list[str], refused: list[str]) -> str | None:
    """Why the navigation failed, if a guard refused something: an inner address wins over a lookup failure."""
    codes = [*blocked, *refused]
    if "address_blocked" in codes:
        return "address_blocked"
    return codes[0] if codes else None


def _shoot(browser, target, mobile, size, max_parts, timeout, allow_private, refused, BrowserError,
           BrowserTimeout) -> Capture:
    width, height = size
    options = {"viewport": {"width": width, "height": height}, "accept_downloads": False, "service_workers": "block"}
    if mobile:
        options |= {"user_agent": MOBILE_AGENT, "is_mobile": True, "has_touch": True}
    context = browser.new_context(**options)
    page = context.new_page()
    page.set_default_timeout(timeout * 1000)
    blocked: list[str] = []

    def guard(route):
        request = route.request
        try:
            own_page = request.frame.page == page
            main_navigation = request.is_navigation_request() and request.frame == page.main_frame
        except BrowserError:
            route.abort()
            return
        problem = None if allow_private else verdict(request.url)
        if own_page and problem is None:
            route.continue_()
            return
        if problem and main_navigation:
            blocked.append(problem)
        route.abort()

    # The route is set on the context, so that a window the page opens is covered too; such a window
    # is closed at once and its requests are refused.
    context.route("**/*", guard)
    context.on("page", lambda opened: opened.close() if opened != page else None)
    # A socket opened by the page is closed on the page side and never connected to a server.
    context.route_web_socket("**/*", lambda socket: socket.close())
    try:
        response = page.goto(target, wait_until="load")
        try:
            page.wait_for_load_state("networkidle", timeout=min(timeout, SETTLE_SECONDS) * 1000)
        except BrowserTimeout:
            pass
    except BrowserTimeout:
        raise CaptureError(_refusal(blocked, refused) or "timeout") from None
    except BrowserError:
        raise CaptureError(_refusal(blocked, refused) or "network_unavailable") from None
    if blocked:
        raise CaptureError(blocked[0])
    problem = chain_problem(response, page.url, allow_private)
    if problem:
        raise CaptureError(problem)
    if response is None:
        raise CaptureError("network_unavailable")
    if not 200 <= response.status < 300:
        raise CaptureError("http_error", response.status)
    if page.evaluate(BLANK_SCRIPT):
        raise CaptureError("blank_page")
    total_height = int(page.evaluate(HEIGHT_SCRIPT))
    total = max(1, math.ceil(total_height / height))
    parts = []
    try:
        for index in range(min(total, max_parts)):
            top = index * height
            clip = {"x": 0, "y": top, "width": width, "height": min(height, max(1, total_height - top))}
            parts.append(page.screenshot(full_page=True, clip=clip, type="png"))
    except BrowserTimeout:
        raise CaptureError("timeout") from None
    except BrowserError:
        raise CaptureError("network_unavailable") from None
    return Capture(page.url, parts, total)
