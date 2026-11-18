"""Shared HTTP plumbing for the thin REST adapters.

Every adapter gets, for free:
- a single ``requests.Session`` pinned to the tool's base URL (custom ports
  supported via the ``*_BASE_URL`` env vars),
- TLS verification control for staging instances with self-signed certs,
- request timeouts (a CI job must never hang),
- retries with exponential backoff on 5xx / 429 / connection errors
  (honouring ``Retry-After``),
- re-authenticate-once-and-retry on 401 via an auth hook,
- callback-driven pagination with a hard page cap.
"""

from __future__ import annotations

import logging
import time

import requests
from tenacity import (
    retry,
    retry_if_exception,
    stop_after_attempt,
    wait_exponential,
)

from websync.adapters.base import SyncError

log = logging.getLogger(__name__)

MAX_PAGES = 100


class TransientHTTPError(SyncError):
    """5xx / 429 response worth retrying."""

    def __init__(self, tool: str, method: str, path: str, status: int, body: str) -> None:
        self.method = method
        self.path = path
        self.status = status
        self.body = body
        # The remote body is deliberately NOT part of the message: messages end
        # up in reports/state DB (CI artifacts). It is logged at DEBUG instead.
        log.debug("%s %s -> HTTP %s body: %s", method, path, status, body[:500])
        super().__init__(tool, f"{method} {path} -> HTTP {status}")


class JSONClient:
    def __init__(
        self,
        tool: str,
        base_url: str,
        *,
        verify: bool = True,
        timeout: float = 30.0,
        max_attempts: int = 3,
        retry_multiplier: float = 0.5,
    ) -> None:
        self.tool = tool
        self.base_url = base_url.rstrip("/")
        self.verify = verify
        self.timeout = timeout
        self.max_attempts = max_attempts
        self.retry_multiplier = retry_multiplier
        self._session = requests.Session()
        self._session.verify = verify
        self._headers = {"Accept": "application/json"}
        self._auth_hook = None
        self._authenticating = False  # guards against hook recursion (auth calls returning 401)
        # Idempotent requests retry on 5xx / 429 / connection errors / timeouts.
        self._retry_idempotent = self._make_retry(self._is_transient)
        # Non-idempotent requests (POST creates) must never be re-sent after a
        # failure that may have happened *after* the server acted: that would
        # create duplicates. Only an explicit 429 (request rejected) is retried.
        self._retry_unsafe = self._make_retry(self._is_rejected)

    @staticmethod
    def _is_transient(exc: BaseException) -> bool:
        return isinstance(exc, TransientHTTPError | requests.ConnectionError | requests.Timeout)

    @staticmethod
    def _is_rejected(exc: BaseException) -> bool:
        return isinstance(exc, TransientHTTPError) and exc.status == 429

    def _make_retry(self, predicate):
        return retry(
            stop=stop_after_attempt(self.max_attempts),
            wait=wait_exponential(multiplier=self.retry_multiplier, max=10),
            retry=retry_if_exception(predicate),
            reraise=True,
            before_sleep=lambda state: log.debug("retrying after %r", state.outcome.exception()),
        )(self._attempt)

    # -- configuration --------------------------------------------------

    def set_headers(self, headers: dict) -> None:
        self._headers.update(headers)

    def set_auth_hook(self, hook) -> None:
        """Register a callback run once on 401 before a single retry."""
        self._auth_hook = hook

    @property
    def session(self) -> requests.Session:
        """Raw session (adapters use it for non-JSON auth calls)."""
        return self._session

    # -- core ------------------------------------------------------------

    def _attempt(self, method, path, params, json_body):
        resp = self._session.request(
            method,
            f"{self.base_url}{path}",
            params=params,
            json=json_body,
            timeout=self.timeout,
            headers=self._headers,
        )
        if resp.status_code == 429:
            retry_after = resp.headers.get("Retry-After", "")
            if retry_after.replace(".", "", 1).isdigit():
                time.sleep(min(float(retry_after), 60.0))
            raise TransientHTTPError(self.tool, method, path, resp.status_code, resp.text)
        if resp.status_code >= 500:
            raise TransientHTTPError(self.tool, method, path, resp.status_code, resp.text)
        return resp

    def _send(self, idempotent: bool, method, path, params, json_body):
        runner = self._retry_idempotent if idempotent else self._retry_unsafe
        return runner(method, path, params, json_body)

    def request(
        self, method: str, path: str, *, params=None, json_body=None, idempotent: bool | None = None
    ):
        """``idempotent`` defaults to False for POST and True for other verbs."""
        if idempotent is None:
            idempotent = method.upper() != "POST"
        resp = self._send(idempotent, method, path, params, json_body)
        if resp.status_code == 401 and self._auth_hook is not None and not self._authenticating:
            self._authenticating = True
            try:
                self._auth_hook()
            finally:
                self._authenticating = False
            resp = self._send(idempotent, method, path, params, json_body)
        if resp.status_code >= 400:
            log.debug("%s %s -> HTTP %s body: %s", method, path, resp.status_code, resp.text[:500])
            raise SyncError(self.tool, f"{method} {path} -> HTTP {resp.status_code}")
        if resp.status_code == 204 or not resp.text:
            return None
        try:
            return resp.json()
        except ValueError as exc:
            raise SyncError(self.tool, f"{method} {path} -> response is not valid JSON") from exc

    def get(self, path: str, *, params=None):
        return self.request("GET", path, params=params)

    def post(self, path: str, *, json_body=None, params=None, idempotent: bool = False):
        return self.request("POST", path, params=params, json_body=json_body, idempotent=idempotent)

    def patch(self, path: str, *, json_body=None, params=None):
        return self.request("PATCH", path, params=params, json_body=json_body)

    # -- pagination -------------------------------------------------------

    def paginate(self, path: str, *, params: dict, extract, next_params) -> list:
        """Iterate a paginated endpoint.

        ``extract(data)`` returns the list of items on a page;
        ``next_params(data, current)`` returns the next page's params, or
        ``None`` when done. A bare-list response simply yields one page.
        """
        items: list = []
        current = dict(params)
        for _ in range(MAX_PAGES):
            data = self.get(path, params=current)
            page = extract(data) or []
            items.extend(page)
            if not page:
                break
            nxt = next_params(data, current)
            if nxt is None:
                break
            current = nxt
        else:  # pragma: no cover - pathological servers only
            raise SyncError(self.tool, f"pagination exceeded {MAX_PAGES} pages for {path}")
        return items
