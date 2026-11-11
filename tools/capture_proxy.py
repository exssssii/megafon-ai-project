"""A local proxy that lets the screenshot browser reach only the address the rule approved.

Used by `tools/web_capture.py`. The browser is started with this proxy, so it never resolves a
name itself: for every connection, including each hop of a redirect, it hands the proxy a name and
a port. The proxy resolves the name once with the given rule, connects to exactly that address and
only then passes bytes through. A refused name gets no connection at all: the proxy closes the
browser's socket without an answer and records the refusal code. HTTPS goes through as a tunnel
(`CONNECT`), so the proxy sees neither the page nor the certificate, and the browser checks the
certificate as usual. Plain HTTP requests come in absolute form and are sent on as one request with
`Connection: close`.
"""
import selectors
import socket
import socketserver
import threading
from typing import Callable
from urllib.parse import urlsplit

from web_fetch import FetchError

MAX_HEADER_BYTES = 64 * 1024
CHUNK_BYTES = 64 * 1024
DROPPED_HEADERS = {"proxy-connection", "proxy-authorization", "connection", "keep-alive"}


class _Server(socketserver.ThreadingTCPServer):
    daemon_threads = True
    allow_reuse_address = False


class GuardProxy:
    """Start with `start()`, give the browser `server`, stop with `stop()`.

    `resolve(host, port)` returns the address to connect to, or raises `FetchError` with the code
    of the refusal. The codes of refused connections are collected in `refused`, in order.
    """

    def __init__(self, resolve: Callable[[str, int], str], timeout: float):
        self.resolve, self.timeout = resolve, timeout
        self.refused: list[str] = []
        self._sockets: set[socket.socket] = set()
        self._lock = threading.Lock()
        self._server: _Server | None = None

    @property
    def server(self) -> str:
        host, port = self._server.server_address[:2]
        return f"http://{host}:{port}"

    def start(self) -> "GuardProxy":
        proxy = self

        class Handler(socketserver.BaseRequestHandler):
            def handle(self):
                proxy._handle(self.request)

        self._server = _Server(("127.0.0.1", 0), Handler)
        threading.Thread(target=self._server.serve_forever, daemon=True).start()
        return self

    def stop(self) -> None:
        if self._server is None:
            return
        self._server.shutdown()
        self._server.server_close()
        with self._lock:
            open_sockets, self._sockets = list(self._sockets), set()
        for sock in open_sockets:
            _close(sock)
        self._server = None

    def _track(self, sock: socket.socket) -> None:
        with self._lock:
            self._sockets.add(sock)

    def _forget(self, sock: socket.socket) -> None:
        with self._lock:
            self._sockets.discard(sock)
        _close(sock)

    def _refuse(self, code: str) -> None:
        with self._lock:
            self.refused.append(code)

    def _handle(self, client: socket.socket) -> None:
        self._track(client)
        upstream = None
        try:
            client.settimeout(self.timeout)
            head, rest = _read_head(client)
            if head is None:
                return
            lines = head.split(b"\r\n")
            parts = lines[0].split(b" ")
            if len(parts) != 3:
                return
            method, target, version = parts
            try:
                if method == b"CONNECT":
                    host, port = _split_authority(target.decode("ascii"))
                    path_line = None
                else:
                    url = urlsplit(target.decode("ascii"))
                    if url.scheme != "http" or not url.hostname:
                        self._refuse("address_blocked")
                        return
                    host, port = url.hostname, url.port or 80
                    path = url.path or "/"
                    path_line = b" ".join((method, (path + (f"?{url.query}" if url.query else "")).encode("ascii"),
                                           version))
            except (UnicodeError, ValueError):
                self._refuse("address_blocked")
                return
            try:
                address = self.resolve(host, port)
            except FetchError as error:
                self._refuse(error.code)
                return
            try:
                upstream = socket.create_connection((address, port), self.timeout)
            except OSError:
                self._refuse("network_unavailable")
                return
            self._track(upstream)
            if path_line is None:
                client.sendall(b"HTTP/1.1 200 Connection Established\r\n\r\n")
            else:
                kept = [line for line in lines[1:] if line and
                        line.split(b":", 1)[0].strip().lower().decode("latin-1") not in DROPPED_HEADERS]
                upstream.sendall(b"\r\n".join([path_line, *kept, b"Connection: close", b"", b""]))
            if rest:
                upstream.sendall(rest)
            _pipe(client, upstream, self.timeout)
        except OSError:
            pass
        finally:
            if upstream is not None:
                self._forget(upstream)
            self._forget(client)


def _read_head(client: socket.socket) -> tuple[bytes | None, bytes]:
    data = b""
    while b"\r\n\r\n" not in data:
        if len(data) > MAX_HEADER_BYTES:
            return None, b""
        chunk = client.recv(CHUNK_BYTES)
        if not chunk:
            return None, b""
        data += chunk
    head, rest = data.split(b"\r\n\r\n", 1)
    return head, rest


def _split_authority(authority: str) -> tuple[str, int]:
    if authority.startswith("["):
        host, _, port = authority[1:].partition("]:")
    else:
        host, _, port = authority.rpartition(":")
    if not host or not port.isdigit() or not 0 < int(port) < 65536:
        raise ValueError(authority)
    return host, int(port)


def _pipe(first: socket.socket, second: socket.socket, timeout: float) -> None:
    """Pass bytes both ways until one side closes or nothing moves for `timeout` seconds."""
    peer = {first: second, second: first}
    with selectors.DefaultSelector() as selector:
        selector.register(first, selectors.EVENT_READ)
        selector.register(second, selectors.EVENT_READ)
        while True:
            ready = selector.select(timeout)
            if not ready:
                return
            for key, _ in ready:
                chunk = key.fileobj.recv(CHUNK_BYTES)
                if not chunk:
                    return
                peer[key.fileobj].sendall(chunk)


def _close(sock: socket.socket) -> None:
    try:
        sock.shutdown(socket.SHUT_RDWR)
    except OSError:
        pass
    try:
        sock.close()
    except OSError:
        pass
