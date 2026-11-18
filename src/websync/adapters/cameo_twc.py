"""Cameo Teamwork Cloud adapter.

No maintained upstream Python client exists for TWC's REST API, so this is a
deliberately thin client scoped to the endpoints we use. TWC REST shapes vary
between server versions; every path/payload below is documented (with the
assumptions we must verify on a live instance) in docs/tools/cameo-twc.md.

Conventions:
- ``SyncItem.fields`` maps ``name`` to the element name; every other key is
  stored as an element property value (``properties[key]``).
- ``SyncItem.type`` is the element type string as stored on the server.
- ``scope.project`` may be a numeric project ID or the project name.
- ``scope.item_type_filter`` (optional) restricts fetch results by type.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import UTC, datetime

from websync.adapters.base import SyncError
from websync.adapters.http import JSONClient
from websync.config import ScopeConfig
from websync.models import SyncItem

DEFAULT_AUTH_PATH = "/authentication"


def _parse_dt(value) -> datetime | None:
    if not value:
        return None
    dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    return dt.replace(tzinfo=UTC) if dt.tzinfo is None else dt


@dataclass
class TWCSettings:
    base_url: str
    username: str
    password: str
    verify: bool = True
    timeout: float = 30.0
    auth_path: str = DEFAULT_AUTH_PATH

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
            timeout=float(os.environ.get("CAMEO_TWC_TIMEOUT", "30")),
            auth_path=os.environ.get("CAMEO_TWC_AUTH_PATH", DEFAULT_AUTH_PATH),
        )


class CameoTWCAdapter:
    tool_name = "cameo_twc"

    def __init__(self, settings: TWCSettings, scope: ScopeConfig | None = None) -> None:
        if not settings.username or not settings.password:
            raise SyncError(self.tool_name, "CAMEO_TWC_USER/CAMEO_TWC_PASSWORD are not set")
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
        self._project_id: str | None = None

    @classmethod
    def from_env(cls, scope: ScopeConfig | None = None) -> CameoTWCAdapter:
        return cls(TWCSettings.from_env(), scope)

    # ------------------------------------------------------------ auth

    def _ensure_auth(self) -> None:
        if self._token is not None:
            return
        session = self._client.session
        session.auth = (self.settings.username, self.settings.password)
        try:
            data = self._client.post(self.settings.auth_path) or {}
        finally:
            session.auth = None
        token = data.get("token") or data.get("jwt") or data.get("access_token")
        if not token:
            raise SyncError(
                self.tool_name, f"auth response missing token (got keys={sorted(data)})"
            )
        self._token = str(token)
        self._client.set_headers({"Authorization": f"Bearer {self._token}"})

    def _reauth(self) -> None:
        self._token = None
        self._ensure_auth()

    # ------------------------------------------------------------ project

    def _resolve_project(self) -> str:
        if self._project_id is not None:
            return self._project_id
        self._ensure_auth()  # healthcheck must also verify auth, even for IDs
        if self.scope is None:
            raise SyncError(self.tool_name, "scope.project is required for TWC links")
        ref = str(self.scope.project)
        if ref.isdigit():
            self._project_id = ref
            return ref
        projects = self._client.get("/projects")
        if isinstance(projects, dict):
            projects = projects.get("projects") or projects.get("data") or []
        for project in projects or []:
            if str(project.get("id")) == ref or project.get("name") == ref:
                self._project_id = str(project["id"])
                return self._project_id
        raise SyncError(self.tool_name, f"project {ref!r} not found")

    def _elements_path(self) -> str:
        return f"/projects/{self._resolve_project()}/elements"

    # ------------------------------------------------------------ protocol

    def healthcheck(self) -> None:
        self._resolve_project()  # auth + project lookup, raises on failure

    def fetch_changed(self, whence: datetime) -> list[SyncItem]:
        self._resolve_project()
        items = []
        for element in self._client.paginate(
            self._elements_path(),
            params={"page": 1},
            extract=self._extract_page,
            next_params=self._next_page_params,
        ):
            modified = _parse_dt(element.get("modified"))
            if modified is not None and modified < whence:
                continue  # client-side change filter (server clocks are untrusted)
            item = self._to_item(element)
            if self._type_allowed(item.type):
                items.append(item)
        return items

    def get(self, native_id: str) -> SyncItem:
        self._ensure_auth()
        return self._to_item(self._client.get(f"/elements/{native_id}"))

    def create(self, item: SyncItem) -> str:
        data = self._client.post(
            self._elements_path(),
            json_body=self._to_payload(type_=item.type, fields=item.fields),
        )
        if not data or "id" not in data:
            raise SyncError(self.tool_name, f"create response missing id: {data!r}")
        return str(data["id"])

    def update(self, native_id: str, changes: dict) -> None:
        self._client.patch(
            f"/elements/{native_id}",
            json_body=self._to_payload(type_=None, fields=changes),
        )

    # ------------------------------------------------------------ helpers

    @staticmethod
    def _extract_page(data):
        if isinstance(data, dict):
            return data.get("elements") or data.get("data") or []
        return data or []

    @staticmethod
    def _next_page_params(data, current):
        if not isinstance(data, dict):
            return None
        page = data.get("page") or {}
        total = int(page.get("total") or 0)
        size = int(page.get("size") or 50)
        page_no = int(current.get("page", 1))
        if total and page_no * size < total:
            return {**current, "page": page_no + 1}
        return None

    def _type_allowed(self, type_: str) -> bool:
        if not self.scope or not self.scope.item_type_filter:
            return True
        return type_ in self.scope.item_type_filter

    @staticmethod
    def _to_item(element: dict) -> SyncItem:
        fields = dict(element.get("properties") or {})
        if element.get("name"):
            fields["name"] = element["name"]
        return SyncItem(
            native_id=str(element["id"]),
            type=str(element.get("type") or ""),
            fields={k: v for k, v in fields.items() if k is not None},
            changed_at=_parse_dt(element.get("modified")),
        )

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
