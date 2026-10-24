"""A local release host for StageDPolicyResolver, behind the real httpx client.

Plain HTTP on 127.0.0.1: ``plain_http_client_factory`` builds a real
``httpx.Client`` whose transport rewrites the resolver's https:// URLs to
http://, so every byte goes through httpx and httpcore's own reads and
timeouts. The host's ``mode`` decides how it answers a policy document:

* ``ok``: the document.
* ``header-drip``: a status line, then a header byte every 20 ms that never
  ends the headers. httpcore reads headers with a fresh per-read timeout, so
  none fires.
* ``deflate-forever``: complete headers with ``Content-Encoding: deflate``,
  then empty stored deflate blocks every 20 ms. httpx decodes each to no
  bytes, so ``iter_bytes`` yields nothing and no byte limit is ever reached.

A bundle is always ``b"bundle"``. ``end_drips()`` closes every dripping
connection, so a fetch still reading one ends.
"""

from __future__ import annotations

import socket
import threading
from typing import Any

import httpx

# A deflate stored block with BFINAL=0 and LEN=0: decodes to nothing.
EMPTY_STORED_BLOCK = b"\x00\x00\x00\xff\xff"


class ReleaseHost:
    def __init__(self, document: bytes) -> None:
        self.document = document
        self.mode = "ok"
        self.requests = 0
        self._lock = threading.Lock()
        self._drips_end = threading.Event()
        self._closed = threading.Event()
        self._listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._listener.bind(("127.0.0.1", 0))
        self._listener.listen(16)
        self.port = self._listener.getsockname()[1]
        threading.Thread(target=self._accept, name="release-host", daemon=True).start()

    @property
    def release_url(self) -> str:
        return f"https://127.0.0.1:{self.port}/trust/"

    def end_drips(self) -> None:
        self._drips_end.set()

    def close(self) -> None:
        self._closed.set()
        self._drips_end.set()
        self._listener.close()

    def _accept(self) -> None:
        while not self._closed.is_set():
            try:
                conn, _ = self._listener.accept()
            except OSError:
                return
            threading.Thread(target=self._serve, args=(conn,), daemon=True).start()

    def _serve(self, conn: socket.socket) -> None:
        with conn:
            conn.settimeout(10)
            request = b""
            while b"\r\n\r\n" not in request:
                chunk = conn.recv(4096)
                if not chunk:
                    return
                request += chunk
            path = request.split(b" ", 2)[1].decode("ascii")
            with self._lock:
                self.requests += 1
            mode = "ok" if path.endswith(".bundle") else self.mode
            try:
                if mode == "header-drip":
                    conn.sendall(b"HTTP/1.1 200 OK\r\nX-Drip: ")
                    while not self._drips_end.wait(0.02):
                        conn.sendall(b"x")
                elif mode == "deflate-forever":
                    conn.sendall(b"HTTP/1.1 200 OK\r\nContent-Encoding: deflate\r\n"
                                 b"Connection: close\r\n\r\n")
                    while not self._drips_end.wait(0.02):
                        conn.sendall(EMPTY_STORED_BLOCK)
                else:
                    body = b"bundle" if path.endswith(".bundle") else self.document
                    conn.sendall(b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n"
                                 b"Content-Length: " + str(len(body)).encode() +
                                 b"\r\nConnection: close\r\n\r\n" + body)
            except OSError:
                return


class _PlainHTTP(httpx.HTTPTransport):
    def handle_request(self, request: httpx.Request) -> httpx.Response:
        request.url = request.url.copy_with(scheme="http")
        return super().handle_request(request)


def plain_http_client_factory(**kwargs: Any) -> httpx.Client:
    return httpx.Client(transport=_PlainHTTP(), **kwargs)
