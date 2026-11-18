"""Jama Connect adapter (REST API v1).

Community Python wrappers for Jama are unmaintained, so this is a thin client
for the documented REST v1 API: https://api.jamacloud.com (or on-prem equivalent).

Conventions (see docs/tools/jama.md):
- ``SyncItem.type`` is the item type's key/label (resolved from itemTypeId);
  ``create()`` resolves it back to an ID via GET /rest/v1/itemtypes.
- Core fields ``name`` / ``description`` map to the item's top-level fields.
- Custom fields use dotted paths in the config: ``fieldData.<filterId>.<fieldId>``
  which the adapter nests into ``location.fieldData`` on writes and flattens
  on reads.
- ``scope.project`` MUST be the numeric Jama project/container ID.
- ``scope.item_type_filter`` (optional) restricts fetch results by type key.
- Change detection filters client-side on ``lastActivityDate`` (server clocks
  are untrusted; hashes in id_map decide the final classification).
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import UTC, datetime

from websync.adapters.base import SyncError
from websync.adapters.http import JSONClient
from websync.config import ScopeConfig
from websync.models import SyncItem

API_ROOT = "/rest/v1"
DEFAULT_PAGE_SIZE = 50


def _parse_dt(value) -> datetime | None:
    if not value:
        return None
    dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    return dt.replace(tzinfo=UTC) if dt.tzinfo is None else dt


@dataclass
class JamaSettings:
    base_url: str
    client_id: str
    client_secret: str
    verify: bool = True
    timeout: float = 30.0

    @classmethod
    def from_env(cls) -> JamaSettings:
        base_url = os.environ.get("JAMA_BASE_URL", "")
        if not base_url:
            raise SyncError("jama", "JAMA_BASE_URL is not set")
        return cls(
            base_url=base_url,
            client_id=os.environ.get("JAMA_CLIENT_ID", ""),
            client_secret=os.environ.get("JAMA_CLIENT_SECRET", ""),
            verify=os.environ.get("JAMA_INSECURE_SKIP_VERIFY", "").lower()
            not in {"1", "true", "yes"},
            timeout=float(os.environ.get("JAMA_TIMEOUT", "30")),
        )


class JamaAdapter:
    tool_name = "jama"

    def __init__(self, settings: JamaSettings, scope: ScopeConfig | None = None) -> None:
        if not settings.client_id or not settings.client_secret:
            raise SyncError(self.tool_name, "JAMA_CLIENT_ID/JAMA_CLIENT_SECRET are not set")
        self.settings = settings
        self.scope = scope
        self._client = JSONClient(
            self.tool_name,
            settings.base_url,
            verify=settings.verify,
            timeout=settings.timeout,
        )
        self._client.set_auth_hook(self._reauth)
        self._token: str | None = None
        self._type_names: dict[str, str] = {}  # itemTypeId -> key/label
        self._type_ids: dict[str, int] = {}  # key/label -> itemTypeId

    @classmethod
    def from_env(cls, scope: ScopeConfig | None = None) -> JamaAdapter:
        return cls(JamaSettings.from_env(), scope)

    # ------------------------------------------------------------ auth

    def _ensure_auth(self) -> None:
        if self._token is not None:
            return
        session = self._client.session
        session.auth = (self.settings.client_id, self.settings.client_secret)
        try:
            resp = session.post(
                f"{self._client.base_url}/rest/oauth/token",
                data={"grant_type": "client_credentials"},
                timeout=self.settings.timeout,
                headers={"Accept": "application/json"},
                verify=self.settings.verify,
            )
        finally:
            session.auth = None
        if resp.status_code >= 400:
            raise SyncError(
                self.tool_name,
                f"oauth token request -> HTTP {resp.status_code}: {resp.text[:300]}",
            )
        token = resp.json().get("access_token")
        if not token:
            raise SyncError(
                self.tool_name, f"oauth response missing access_token: {resp.text[:300]}"
            )
        self._token = str(token)
        self._client.set_headers({"Authorization": f"Bearer {self._token}"})

    def _reauth(self) -> None:
        self._token = None
        self._ensure_auth()

    # ------------------------------------------------------------ scope / types

    def _project_id(self) -> int:
        if self.scope is None:
            raise SyncError(self.tool_name, "scope.project is required for Jama links")
        ref = str(self.scope.project)
        if not ref.isdigit():
            raise SyncError(
                self.tool_name,
                f"scope.project must be the numeric Jama project ID, got {ref!r}",
            )
        return int(ref)

    def _load_item_types(self) -> None:
        if self._type_ids:
            return
        self._ensure_auth()
        data = self._client.get(f"{API_ROOT}/itemtypes", params={"maxResults": 150}) or {}
        for entry in data.get("data") or []:
            type_id = int(entry["id"])
            name = entry.get("key") or entry.get("label") or str(type_id)
            self._type_names[str(type_id)] = name
            self._type_ids.setdefault(name, type_id)

    def _type_name(self, type_id) -> str:
        key = str(type_id)
        if key not in self._type_names:
            self._ensure_auth()
            data = self._client.get(f"{API_ROOT}/itemtypes/{type_id}") or {}
            entry = data.get("data") or {}
            self._type_names[key] = entry.get("key") or entry.get("label") or key
        return self._type_names[key]

    def _resolve_type_id(self, ref: str) -> int:
        self._load_item_types()
        if ref.isdigit() and ref in self._type_names:
            return int(ref)
        if ref in self._type_ids:
            return self._type_ids[ref]
        raise SyncError(
            self.tool_name,
            f"unknown item type {ref!r} (known: {sorted(self._type_ids)})",
        )

    def _type_allowed(self, type_: str) -> bool:
        if not self.scope or not self.scope.item_type_filter:
            return True
        return type_ in self.scope.item_type_filter

    # ------------------------------------------------------------ protocol

    def healthcheck(self) -> None:
        self._ensure_auth()
        self._client.get(f"{API_ROOT}/users/current")

    def fetch_changed(self, whence: datetime) -> list[SyncItem]:
        self._ensure_auth()
        project = self._project_id()
        entries = self._client.paginate(
            f"{API_ROOT}/items",
            params={"project": project, "startAt": 0, "maxResults": DEFAULT_PAGE_SIZE},
            extract=self._extract_page,
            next_params=self._next_page_params,
        )
        items = []
        for entry in entries:
            modified = _parse_dt(entry.get("lastActivityDate"))
            if modified is not None and modified < whence:
                continue
            item = self._to_item(entry)
            if self._type_allowed(item.type):
                items.append(item)
        return items

    def get(self, native_id: str) -> SyncItem:
        self._ensure_auth()
        data = self._client.get(f"{API_ROOT}/items/{native_id}") or {}
        entry = data.get("data", data)
        return self._to_item(entry)

    def create(self, item: SyncItem) -> str:
        type_id = self._resolve_type_id(item.type)
        core, custom = _split_fields(item.fields)
        payload = {
            "project": self._project_id(),
            "itemTypeId": type_id,
            "name": core.get("name", ""),
            "description": core.get("description", ""),
        }
        data = self._client.post(f"{API_ROOT}/items", json_body=payload) or {}
        created = data.get("data") or {}
        if "id" not in created:
            raise SyncError(self.tool_name, f"create response missing id: {data!r}")
        native_id = str(created["id"])
        if custom:
            self._patch_fields(native_id, custom)
        return native_id

    def update(self, native_id: str, changes: dict) -> None:
        core, custom = _split_fields(changes)
        if not core and not custom:
            return
        self._patch_fields(native_id, custom, core)

    # ------------------------------------------------------------ helpers

    def _patch_fields(self, native_id: str, custom: dict, core: dict | None = None) -> None:
        payload: dict = dict(core or {})
        if custom:
            payload["location"] = {"fieldData": _nest_custom_fields(custom)}
        self._client.patch(f"{API_ROOT}/items/{native_id}", json_body=payload)

    @staticmethod
    def _extract_page(data):
        return (data or {}).get("data") or []

    @staticmethod
    def _next_page_params(data, current):
        info = ((data or {}).get("meta") or {}).get("pageInfo") or {}
        start = int(info.get("startIndex", 0))
        count = int(info.get("resultCount", 0))
        total = int(info.get("totalResults", 0))
        if not count or start + count >= total:
            return None
        return {**current, "startAt": start + count}

    def _to_item(self, entry: dict) -> SyncItem:
        fields = {
            "name": entry.get("name"),
            "description": entry.get("description"),
        }
        field_data = ((entry.get("location") or {}).get("fieldData")) or {}
        for filter_id, values in field_data.items():
            for field_id, value in values.items():
                fields[f"fieldData.{filter_id}.{field_id}"] = value
        return SyncItem(
            native_id=str(entry["id"]),
            type=self._type_name(entry.get("itemType")),
            fields=fields,
            changed_at=_parse_dt(entry.get("lastActivityDate")),
        )


def _split_fields(fields: dict) -> tuple[dict, dict]:
    """Split native fields into core (top-level) and custom (dotted) fields."""
    core, custom = {}, {}
    for key, value in (fields or {}).items():
        if key.startswith("fieldData."):
            custom[key] = value
        else:
            core[key] = value
    return core, custom


def _nest_custom_fields(custom: dict) -> dict:
    """``fieldData.<filterId>.<fieldId>`` -> ``{<filterId>: {<fieldId>: v}}``."""
    nested: dict = {}
    for key, value in custom.items():
        _, filter_id, field_id, *_ = key.split(".")
        nested.setdefault(filter_id, {})[field_id] = value
    return nested
