"""A tiny threaded HTTP stub server for aiohttp-based contract tests.

The natural choice here would be ``aioresponses``, but it is incompatible
with the aiohttp >= 3.13 wheels required on Python 3.15 (``ClientResponse``
signature change) and older aiohttp cannot be built on this platform. The
stub server provides the same contract-test properties — canned responses
per (method, path[, request body]), FIFO consumption, request recording —
while exercising the real aiohttp + atwc stack end to end.
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class _Route:
    __slots__ = ("method", "path", "body", "queue")

    def __init__(self, method: str, path: str, body) -> None:
        self.method = method
        self.path = path
        self.body = body  # expected parsed request body (None = match any)
        self.queue: list[dict] = []

    def matches(self, method: str, path: str, parsed_body) -> bool:
        if self.method != method or self.path != path:
            return False
        return self.body is None or self.body == parsed_body


class StubHTTPServer:
    """Serves FIFO canned responses; records every request it receives.

    ``add`` may be called several times for the same (method, path[, body]):
    responses are consumed in the order they were added. Requests that match
    no route (or exhaust a route's queue) get a 404 — a request the test did
    not expect is a contract failure, not an error.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._routes: list[_Route] = []
        self.calls: list[dict] = []
        self._server: ThreadingHTTPServer | None = None

    @property
    def url(self) -> str:
        host, port = self._server.server_address[:2]  # type: ignore[index]
        return f"http://{host}:{port}/"

    def add(
        self,
        method: str,
        path: str,
        *,
        status: int = 200,
        json=None,
        repeat: int = 1,
        body=None,
    ) -> None:
        """Queue ``repeat`` responses for (method, path); ``body`` (when given)
        must equal the parsed JSON request body for the route to match."""
        with self._lock:
            route = next(
                (
                    r
                    for r in self._routes
                    if r.method == method and r.path == path and r.body == body
                ),
                None,
            )
            if route is None:
                route = _Route(method, path, body)
                self._routes.append(route)
            for _ in range(repeat):
                route.queue.append({"status": status, "json": json})

    def calls_for(self, method: str, path: str | None = None) -> list[dict]:
        return [
            call
            for call in self.calls
            if call["method"] == method and (path is None or call["path"] == path)
        ]

    def start(self) -> StubHTTPServer:
        server = self

        class Handler(BaseHTTPRequestHandler):
            def _handle(self) -> None:
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length) if length else b""
                try:
                    parsed = json.loads(raw) if raw else None
                except ValueError:
                    parsed = None
                with server._lock:
                    server.calls.append(
                        {
                            "method": self.command,
                            "path": self.path,
                            "headers": dict(self.headers.items()),
                            "body": parsed,
                        }
                    )
                    route = next(
                        (r for r in server._routes if r.matches(self.command, self.path, parsed)),
                        None,
                    )
                    if route and route.queue:
                        response = route.queue.pop(0)
                    else:
                        response = {
                            "status": 404,
                            "json": {"error": f"unexpected {self.command} {self.path}"},
                        }
                data = b"" if response["json"] is None else json.dumps(response["json"]).encode()
                self.send_response(response["status"])
                if response["json"] is not None:
                    self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                if data:
                    self.wfile.write(data)

            do_GET = do_POST = do_PATCH = do_PUT = do_DELETE = _handle

            def log_message(self, *args) -> None:  # keep test output quiet
                pass

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self._server.serve_forever, daemon=True).start()
        return self

    def stop(self) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
            self._server = None
