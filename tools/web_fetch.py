"""Download one public page and turn it into plain text, with no model in between.

Used by `tools/read_material.py fetch-web`. Only public http and https addresses are
opened: the address is checked when the connection is made, so a name that changes
its answer and a redirect are both covered. Proxies from the environment are not used.
Size and time are limited. Page text is data: nothing here follows it, and no scripts
run, so a page that needs them comes back too short and is reported as such.
"""
import codecs
from dataclasses import dataclass
from html.parser import HTMLParser
import hashlib
import http.client
import ipaddress
import re
import socket
import ssl
import string
import time
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urljoin, urlsplit, urlunsplit
from urllib.request import HTTPHandler, HTTPRedirectHandler, HTTPSHandler, ProxyHandler, Request, build_opener

import truststore

MAX_PAGE_BYTES = 5 * 1024 * 1024
CHUNK_BYTES = 64 * 1024
TIMEOUT_SECONDS = 20
TOTAL_SECONDS = 60
MIN_TEXT_CHARS = 200
MAX_DAMAGED_SHARE = 0.001
TEXT_TYPES = ("text/html", "application/xhtml+xml", "text/plain")
HEADERS = {"User-Agent": "case-reader/1.0", "Accept": "text/html,application/xhtml+xml,text/plain;q=0.9"}
SKIPPED = {"script", "style", "noscript", "template", "svg", "canvas", "head", "iframe", "object"}
VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"}
BLOCKS = {"address", "article", "aside", "blockquote", "br", "dd", "details", "div", "dl", "dt", "fieldset", "figcaption",
          "figure", "footer", "form", "header", "hr", "li", "main", "nav", "ol", "p", "pre", "section", "table", "tr", "ul"}
CELLS = {"td", "th"}
HEADINGS = {"h1": "#", "h2": "##", "h3": "###", "h4": "####", "h5": "#####", "h6": "######"}
META_CHARSET_RE = re.compile(rb"""<meta[^>]+charset=["']?([\w-]+)""", re.I)
EMBEDDED_IPV4 = (ipaddress.ip_network("::/96"), ipaddress.ip_network("64:ff9b::/96"))


class FetchError(Exception):
    """The page was not obtained; `code` tells the procedure what to do next."""

    def __init__(self, code: str, status: int | None = None):
        super().__init__(code)
        self.code, self.status = code, status


@dataclass
class Page:
    text: str
    sha256: str
    complete: bool
    final_url: str


def normalized(url: str) -> str:
    """The same address in ASCII: national letters and spaces in a path are ordinary on Russian sites."""
    try:
        parts = urlsplit(url.strip())
        host, port = parts.hostname, parts.port
        if parts.scheme not in ("http", "https") or not host or parts.username or parts.password:
            raise FetchError("address_blocked")
        name = f"[{host}]" if ":" in host else host.encode("idna").decode("ascii")
    except (ValueError, UnicodeError):
        raise FetchError("address_blocked") from None
    netloc = f"{name}:{port}" if port else name
    return urlunsplit((parts.scheme, netloc, quote(parts.path, safe=string.punctuation),
                       quote(parts.query, safe=string.punctuation), ""))


def is_public(address: str) -> bool:
    ip = ipaddress.ip_address(address.split("%")[0])
    if ip.version == 6 and (ip.ipv4_mapped or any(ip in network for network in EMBEDDED_IPV4)):
        return False
    return ip.is_global and not ip.is_multicast


def public_address(host: str, port: int) -> str:
    """One address of the host, only if every address it has is public."""
    try:
        found = socket.getaddrinfo(host, port, proto=socket.IPPROTO_TCP)
    except (OSError, UnicodeError):
        raise FetchError("network_unavailable") from None
    addresses = [item[4][0] for item in found]
    if not addresses or not all(is_public(address) for address in addresses):
        raise FetchError("address_blocked")
    return addresses[0]


def check_public(url: str) -> None:
    parts = urlsplit(url)
    public_address(parts.hostname, parts.port or (443 if parts.scheme == "https" else 80))


def connect_public(address, timeout=TIMEOUT_SECONDS, source_address=None):
    """Connect to the checked address, not to a second answer for the same name."""
    host, port = address
    return socket.create_connection((public_address(host, port), port), timeout, source_address)


class PublicHTTP(http.client.HTTPConnection):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._create_connection = connect_public


class PublicHTTPS(http.client.HTTPSConnection):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._create_connection = connect_public


class PublicHTTPHandler(HTTPHandler):
    def http_open(self, req):
        return self.do_open(PublicHTTP, req)


class PublicHTTPSHandler(HTTPSHandler):
    def https_open(self, req):
        # The system checks the certificate: on Windows the store Python sees is often missing roots.
        return self.do_open(PublicHTTPS, req, context=truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT))


class CheckedRedirects(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        target = normalized(urljoin(req.full_url, newurl))
        check_public(target)
        return super().redirect_request(req, fp, code, msg, headers, target)


def public_opener():
    return build_opener(ProxyHandler({}), PublicHTTPHandler, PublicHTTPSHandler, CheckedRedirects)


class TextOfPage(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.out, self.skipping = [], []

    def handle_starttag(self, tag, attrs):
        if tag == "body" and "head" in self.skipping:  # the closing tag of head is optional
            self.skipping.clear()
        if self.skipping or tag in SKIPPED:
            if tag not in VOID:
                self.skipping.append(tag)
            return
        if tag in HEADINGS:
            self.out.append(f"\n\n{HEADINGS[tag]} ")
        elif tag in BLOCKS:
            self.out.append("\n\n")

    def handle_endtag(self, tag):
        if self.skipping:
            if tag in self.skipping:
                while self.skipping.pop() != tag:
                    pass
            return
        if tag in HEADINGS or tag in BLOCKS:
            self.out.append("\n\n")
        elif tag in CELLS:
            self.out.append(" | ")

    def handle_data(self, data):
        if not self.skipping:
            self.out.append(re.sub(r"\s+", " ", data))


def html_text(markup: str) -> tuple[str, bool]:
    """Plain text and whether all of it was seen: a tag left open hides what follows it."""
    parser = TextOfPage()
    parser.feed(markup)
    parser.close()
    lines = (line.strip().removesuffix("|").strip() for line in "".join(parser.out).splitlines())
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip(), not parser.skipping


def strict_text(raw: bytes, encoding: str, whole: bool) -> str | None:
    try:
        return codecs.getincrementaldecoder(encoding)().decode(raw, final=whole)
    except (LookupError, UnicodeError):
        return None


def decode(raw: bytes, charset: str | None, whole: bool) -> str:
    """UTF-8 first: it almost never fits by chance, while a declared charset is often wrong."""
    text = strict_text(raw, "utf-8", whole)
    if text is not None:
        return text
    damaged = raw.decode("utf-8", "replace")
    if damaged.count("�") <= len(damaged) * MAX_DAMAGED_SHARE:
        return damaged
    meta = META_CHARSET_RE.search(raw[:4096])
    declared = charset or (meta.group(1).decode("ascii", "replace") if meta else None)
    for encoding in (declared, "cp1251"):
        text = strict_text(raw, encoding, whole) if encoding else None
        if text is not None:
            return text
    raise FetchError("not_text")


def read_body(response) -> tuple[bytes, bool]:
    chunks, size, whole = [], 0, True
    deadline = time.monotonic() + TOTAL_SECONDS
    try:
        while size <= MAX_PAGE_BYTES:
            if time.monotonic() > deadline:
                whole = False
                break
            chunk = response.read1(CHUNK_BYTES)
            if not chunk:
                break
            chunks.append(chunk)
            size += len(chunk)
    except (http.client.HTTPException, OSError):
        whole = False
    length = response.headers.get("Content-Length", "")
    if length.isdigit() and int(length) != size:
        whole = False
    return b"".join(chunks)[:MAX_PAGE_BYTES], whole and size <= MAX_PAGE_BYTES


def fetch_page(url: str, opener=None) -> Page:
    target = normalized(url)
    check_public(target)
    opener = opener or public_opener()
    try:
        with opener.open(Request(target, headers=HEADERS), timeout=TIMEOUT_SECONDS) as response:
            kind = response.headers.get_content_type() if response.headers.get("Content-Type") else "text/html"
            if kind not in TEXT_TYPES:
                raise FetchError("unsupported_type")
            raw, whole = read_body(response)
            charset, final_url = response.headers.get_content_charset(), response.geturl()
    except HTTPError as error:
        raise FetchError("http_error", error.code) from None
    except (URLError, OSError, http.client.HTTPException, ValueError):
        raise FetchError("network_unavailable") from None
    markup = decode(raw, charset, whole)
    text, seen = (markup.strip(), True) if kind == "text/plain" else html_text(markup)
    if len(text) < MIN_TEXT_CHARS:
        raise FetchError("too_little_text")
    return Page(text, hashlib.sha256(raw).hexdigest(), whole and seen, final_url)
