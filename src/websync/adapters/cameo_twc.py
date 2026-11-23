"""Cameo Teamwork Cloud adapter, built on the ``atwc`` package.

``atwc`` (PyPI, Apache-2.0) provides the HTTP plumbing for TWC's REST API:
an asyncio/``aiohttp`` client with HTTP Basic auth and login/logout
sessions, plus ``ResourceBrowser``/``ModelBrowser`` helpers for projects,
revisions, elements and tagged values. This adapter is the glue that turns
it into a synchronous ``Adapter`` — everything TWC-specific that ``atwc``
does not own (writes, batched traversal, timeouts, retries, error mapping)
lives here and is flagged for live verification in
``docs/tools/cameo-twc.md``.

``atwc`` quirks this adapter works around (see docs/tools/cameo-twc.md):
- its ``create_session`` login json-parses TWC's empty ``204`` login
  response, which always raises on a real server; ``TWCClient`` overrides
  session construction with a 204-safe login/logout,
- it sets no request timeout and offers no TLS verification switch;
  ``TWCClient`` adds both,
- it is read-focused: create/update go through generic POST/PATCH calls
  against resource-scoped element endpoints, with payload shapes to be
  verified against our install.

Conventions:
- ``SyncItem.type`` is the element's ``@type`` with any namespace prefix
  stripped (e.g. ``kerml:Requirement`` -> ``Requirement``).
- ``SyncItem.fields`` maps ``name`` to the element name; every other key is
  a tagged value (tag-definition name -> value).
- ``scope.project`` may be a resource ID or a resource/project title
  (``Name`` or ``Name.MASTER``; ``.MASTER`` wins when ambiguous).
- ``scope.item_type_filter`` (optional) restricts fetch results by type;
  elements without a type are always skipped (tagged values, sections and
  other structural values are not syncable items).
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
import urllib.parse
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime

import aiohttp
from atwc.browsers import ModelBrowser, ResourceBrowser
from atwc.client import Client as ATWCClient
from atwc.utils import extract_ids

from websync.adapters.base import AuthError, SyncError
from websync.config import ScopeConfig
from websync.models import SyncItem

log = logging.getLogger(__name__)

DEFAULT_TIMEOUT = 30.0
MODIFIED_KEYS = ("modified", "kerml:modified", "sysml:modified")


class TransientTWCError(SyncError):
    """A retryable TWC failure (5xx/429, connection/timeout, expired session)."""

    def __init__(self, tool: str, method: str, path: str, status: int) -> None:
        self.method = method
        self.path = path
        self.status = status
        if status:
            super().__init__(tool, f"{method} {path} -> HTTP {status}")
        else:
            super().__init__(tool, f"{method} {path} -> connection failed")


def _parse_dt(value) -> datetime | None:
    if not value:
        return None
    dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    return dt.replace(tzinfo=UTC) if dt.tzinfo is None else dt


def _element_of(wrapper):
    """Normalize an atwc element payload to the element dict itself.

    ``ModelBrowser.get_elements_batch`` yields ``{"data": [meta, element]}``
    wrappers; ``get_element`` already returns the element dict.
    """
    if (
        isinstance(wrapper, dict)
        and isinstance(wrapper.get("data"), list)
        and len(wrapper["data"]) > 1
    ):
        return wrapper["data"][1]
    return wrapper


def _ref_ids(element) -> list[str]:
    """Element IDs referenced from an element's esiData (lists of ``@id`` refs)."""
    ids = []
    esi = element.get("kerml:esiData") or {}
    for value in esi.values():
        if isinstance(value, list):
            ids.extend(str(ref["@id"]) for ref in value if isinstance(ref, dict) and "@id" in ref)
    return ids


def _type_of(element) -> str:
    """Element type with any namespace prefix stripped ('' when untyped)."""
    raw = None
    for key in ("@type", "kerml:esiType", "xsi:type"):
        if element.get(key):
            raw = element[key]
            break
    if raw is None:
        raw = (element.get("kerml:esiData") or {}).get("kerml:esiType")
    return str(raw).rsplit(":", 1)[-1] if raw else ""


def _modified_of(element) -> datetime | None:
    for source in (element, element.get("kerml:esiData") or {}):
        for key in MODIFIED_KEYS:
            if source.get(key):
                return _parse_dt(source[key])
    return None


def _created_id(payload) -> str | None:
    if not isinstance(payload, dict):
        return None
    for key in ("id", "ID", "kerml:esiID"):
        if payload.get(key) is not None:
            return payload[key]
    return (payload.get("kerml:esiData") or {}).get("kerml:esiID")


@dataclass
class TWCSettings:
    base_url: str
    username: str
    password: str
    verify: bool = True
    timeout: float = DEFAULT_TIMEOUT
    max_attempts: int = 3
    retry_multiplier: float = 0.5

    @classmethod
    def from_env(cls) -> TWCSettings:
        base_url = os.environ.get("CAMEO_TWC_BASE_URL", "")
        if not base_url:
            raise SyncError("cameo_twc", "CAMEO_TWC_BASE_URL is not set")
        return cls(
            base_url=base_url,
            username=os.environ.get("CAMEO_TWC_USER", ""),
            password=os.environ.get("CAMEO_TWC_PASSWORD", ""),
            verify=os.environ.get("CAMEO_TWC_INSECURE_SKIP_VERIFY", "").lower()
            not in {"1", "true", "yes"},
            timeout=float(os.environ.get("CAMEO_TWC_TIMEOUT", str(DEFAULT_TIMEOUT))),
        )


class TWCClient(ATWCClient):
    """atwc client with timeouts, TLS control and a 204-safe login/logout.

    Overrides atwc's session construction, which sets neither a timeout nor
    a TLS policy and json-parses the empty 204 login response.
    """

    def __init__(
        self,
        api_url: str,
        login: str,
        password: str,
        *,
        verify: bool = True,
        timeout: float = DEFAULT_TIMEOUT,
    ) -> None:
        super().__init__(api_url, login, password)
        self._verify = verify
        self._aio_timeout = aiohttp.ClientTimeout(total=timeout)

    @asynccontextmanager
    async def create_session(self):
        tstart = time.monotonic()
        connector = aiohttp.TCPConnector(ssl=None if self._verify else False)
        async with aiohttp.ClientSession(
            auth=self._auth,
            raise_for_status=True,
            cookie_jar=aiohttp.CookieJar(unsafe=True),
            timeout=self._aio_timeout,
            connector=connector,
        ) as self._session:
            await self._login()
            try:
                yield self._session
            finally:
                await self._logout()
                self._session = None
                log.debug("TWC session closed (%.1fs)", time.monotonic() - tstart)

    def _url(self, path: str) -> str:
        return urllib.parse.urljoin(self._api_url, path)

    async def _login(self) -> None:
        try:
            response = await self._session.get(self._url("login"))
        except aiohttp.ClientResponseError as exc:
            raise AuthError("cameo_twc", f"login rejected (HTTP {exc.status})") from exc
        if response.status != 204:
            raise AuthError("cameo_twc", f"login failed with HTTP {response.status}")

    async def _logout(self) -> None:
        try:
            await self._session.get(self._url("logout"))
        except aiohttp.ClientError as exc:
            log.debug("TWC logout failed: %r", exc)

    async def _fetch(self, method: str, path: str, data=None):
        assert self._session
        tstart = time.monotonic()
        async with self._session.request(method, self._url(path), data=data) as response:
            body = await response.text()
            status = response.status
        log.debug("TWC %s %s -> %s (%.2fs)", method, path, status, time.monotonic() - tstart)
        if not body:
            return status, None
        try:
            return status, json.loads(body)
        except ValueError as exc:
            raise SyncError("cameo_twc", f"{method} {path} -> response is not valid JSON") from exc

    async def get(self, path: str):
        return await self._fetch("GET", path)

    async def post(self, path: str, data):
        if not isinstance(data, str):
            data = json.dumps(data)
        return await self._fetch("POST", path, data)

    async def patch(self, path: str, data):
        """Not in atwc (it is read-focused); needed for element updates."""
        if not isinstance(data, str):
            data = json.dumps(data)
        return await self._fetch("PATCH", path, data)


class CameoTWCAdapter:
    tool_name = "cameo_twc"
    batch_size = 50  # elements per batch fetch during model traversal

    def __init__(self, settings: TWCSettings, scope: ScopeConfig | None = None) -> None:
        if not settings.username or not settings.password:
            raise SyncError(self.tool_name, "CAMEO_TWC_USER/CAMEO_TWC_PASSWORD are not set")
        self.settings = settings
        self.scope = scope
        self._client = TWCClient(
            settings.base_url,
            settings.username,
            settings.password,
            verify=settings.verify,
            timeout=settings.timeout,
        )
        self._resource: dict | None = None
        self._tagdef_names: dict[str, str] = {}

    @classmethod
    def from_env(cls, scope: ScopeConfig | None = None) -> CameoTWCAdapter:
        return cls(TWCSettings.from_env(), scope)

    # ------------------------------------------------------------ bridge

    def _run(self, op, *args):
        """Run ``op`` in a fresh atwc session, retrying transient failures.

        Each attempt is a complete session (login -> work -> logout): a 401
        mid-run therefore re-authenticates on retry. A 401 on *login* maps
        to :class:`AuthError` (bad credentials) and aborts immediately.
        """
        last: TransientTWCError | None = None
        for attempt in range(self.settings.max_attempts):
            try:
                return asyncio.run(self._with_session(op, *args))
            except TransientTWCError as exc:
                last = exc
            except aiohttp.ClientResponseError as exc:
                info = exc.request_info
                if exc.status in (401, 429) or exc.status >= 500:
                    last = TransientTWCError(self.tool_name, info.method, str(info.url), exc.status)
                else:
                    raise SyncError(
                        self.tool_name, f"{info.method} {info.url} -> HTTP {exc.status}"
                    ) from exc
            except (aiohttp.ClientConnectionError, TimeoutError) as exc:
                last = TransientTWCError(self.tool_name, "request", "", 0)
                log.debug("TWC connection failure: %r", exc)
            if attempt < self.settings.max_attempts - 1:
                time.sleep(self.settings.retry_multiplier * (2**attempt))
        assert last is not None
        raise last

    async def _with_session(self, op, *args):
        async with self._client.create_session():
            return await op(*args)

    # ------------------------------------------------------------ project

    async def _resolve_resource(self) -> dict:
        if self._resource is not None:
            return self._resource
        if self.scope is None or self.scope.project is None:
            raise SyncError(self.tool_name, "scope.project is required for TWC links")
        ref = str(self.scope.project)
        browser = ResourceBrowser(self._client)
        await browser.fetch()
        matches = [
            resource
            for resource in browser.all_resources or []
            if str(resource.get("ID")) == ref
            or resource.get("dcterms:title") in (ref, f"{ref}.MASTER")
        ]
        if not matches:
            raise SyncError(self.tool_name, f"project {ref!r} not found")
        # `.MASTER` is the current working revision of a modeling project.
        self._resource = max(
            matches,
            key=lambda r: str(r.get("dcterms:title", "")).endswith(".MASTER"),
        )
        return self._resource

    # ------------------------------------------------------------ protocol

    def healthcheck(self) -> None:
        self._run(self._healthcheck)

    async def _healthcheck(self) -> None:
        await self._resolve_resource()

    def fetch_changed(self, whence: datetime) -> list[SyncItem]:
        return self._run(self._fetch_changed, whence)

    async def _fetch_changed(self, whence: datetime) -> list[SyncItem]:
        resource = await self._resolve_resource()
        browser = ModelBrowser(self._client, resource)
        items = []
        for element_id, element in (await self._walk_model(browser)).items():
            modified = _modified_of(element)
            if modified is not None and modified < whence:
                continue  # client-side change filter (server clocks are untrusted)
            item = await self._to_item(element_id, element, browser)
            if self._type_allowed(item.type):
                items.append(item)
        return items

    async def _walk_model(self, browser: ModelBrowser) -> dict:
        """Traverse the resource's current revision, batch-fetching elements.

        TWC (via atwc) has no "list all elements" endpoint; the model is
        walked from the revision's root objects, following every ``@id``
        reference in each element's esiData, fetching in batches.
        """
        revdata = await browser.get_revision_info()
        queue = [str(element_id) for element_id in revdata.get("rootObjectIDs") or []]
        elements: dict = {}
        seen: set[str] = set()
        while queue:
            batch = [
                element_id for element_id in queue[: self.batch_size] if element_id not in seen
            ]
            del queue[: self.batch_size]
            if not batch:
                continue
            seen.update(batch)
            result = await browser.get_elements_batch(batch)
            for element_id, wrapper in (result or {}).items():
                element = _element_of(wrapper)
                elements[str(element_id)] = element
                queue.extend(_ref_ids(element))
        return elements

    def get(self, native_id: str) -> SyncItem:
        return self._run(self._get, native_id)

    async def _get(self, native_id: str) -> SyncItem:
        resource = await self._resolve_resource()
        browser = ModelBrowser(self._client, resource)
        element = await browser.get_element(native_id)
        return await self._to_item(native_id, element, browser)

    def create(self, item: SyncItem) -> str:
        return self._run(self._create, item)

    async def _create(self, item: SyncItem) -> str:
        resource = await self._resolve_resource()
        _, payload = await self._client.post(
            f"resources/{resource['ID']}/elements",
            data=self._to_payload(type_=item.type, fields=item.fields),
        )
        native_id = _created_id(payload)
        if native_id is None:
            raise SyncError(self.tool_name, "create response missing element ID")
        return str(native_id)

    def update(self, native_id: str, changes: dict) -> None:
        self._run(self._update, native_id, changes)

    async def _update(self, native_id: str, changes: dict) -> None:
        resource = await self._resolve_resource()
        await self._client.patch(
            f"resources/{resource['ID']}/elements/{native_id}",
            data=self._to_payload(type_=None, fields=changes),
        )

    # ------------------------------------------------------------ helpers

    async def _to_item(self, element_id, element, browser: ModelBrowser) -> SyncItem:
        esi = element.get("kerml:esiData") or {}
        fields = {"name": esi["name"]} if esi.get("name") else {}
        fields.update(await self._tagged_values(browser, element))
        return SyncItem(
            native_id=str(element_id),
            type=_type_of(element),
            fields={key: value for key, value in fields.items() if key is not None},
            changed_at=_modified_of(element),
        )

    async def _tagged_values(self, browser: ModelBrowser, element) -> dict:
        esi = element.get("kerml:esiData") or {}
        tv_ids = extract_ids(esi.get("taggedValue") or [])
        if not tv_ids:
            return {}
        result = await browser.get_elements_batch(tv_ids)
        values = {}
        for wrapper in (result or {}).values():
            tagged_value = _element_of(wrapper)
            tv_esi = tagged_value.get("kerml:esiData") or {}
            def_ids = extract_ids(tv_esi.get("tagDefinition") or [])
            if not def_ids:
                continue
            name = await self._tagdef_name(browser, def_ids[0])
            values[name] = tv_esi.get("value")
        return values

    async def _tagdef_name(self, browser: ModelBrowser, def_id) -> str:
        if def_id not in self._tagdef_names:
            definition = await browser.get_element(def_id)
            esi = definition.get("kerml:esiData") or {}
            self._tagdef_names[def_id] = str(esi.get("name") or def_id)
        return self._tagdef_names[def_id]

    def _type_allowed(self, type_: str) -> bool:
        if not type_:
            return False  # structural values (tagged values, sections, ...) are not items
        if not self.scope or not self.scope.item_type_filter:
            return True
        return type_ in self.scope.item_type_filter

    @staticmethod
    def _to_payload(type_: str | None, fields: dict) -> dict:
        payload: dict = {}
        properties: dict = {}
        for key, value in (fields or {}).items():
            if key == "name":
                payload["name"] = value
            else:
                properties[key] = value
        if properties:
            payload["properties"] = properties
        if type_:
            payload["type"] = type_
        return payload
