"""Thin REST clients for the DataOS services dapp needs (Poros and Heimdall).

Routes come from dataos-cli/pkg/conf/static/dataos.config.yaml and poros-service/pkg/api/v2.
"""

from __future__ import annotations

import re
from typing import Any

import httpx

from .config import Context


class DataOSError(RuntimeError):
    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status


def _raise_for(r: httpx.Response, what: str) -> None:
    if r.is_success:
        return
    detail = r.text.strip()[:500]
    if r.status_code == 401:
        hint = " (token expired? run `dataos-ctl login` or refresh DATAOS_RUN_AS_APIKEY)"
    elif r.status_code == 403:
        hint = " (no permission for this in DataOS; ask a tenant admin for a grant)"
    else:
        hint = ""
    raise DataOSError(f"{what} failed: HTTP {r.status_code}{hint}: {detail}", r.status_code)


class DataOS:
    """One client per context. Use as a context manager or call close()."""

    def __init__(self, ctx: Context, *, timeout: float = 30.0, transport: httpx.BaseTransport | None = None):
        self.ctx = ctx
        self.http = httpx.Client(
            base_url=ctx.base_url,
            headers={**ctx.auth_headers(), "User-Agent": "dapp/0.1"},
            timeout=timeout,
            transport=transport,
        )

    def __enter__(self) -> DataOS:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def close(self) -> None:
        self.http.close()

    def _call(self, method: str, path: str, **kw: Any) -> httpx.Response:
        try:
            return self.http.request(method.upper(), path, **kw)
        except httpx.HTTPError as e:
            raise DataOSError(f"cannot reach DataOS at {self.ctx.base_url}: {e.__class__.__name__}: {e}") from e

    # ---------------- Poros ----------------
    @property
    def _poros(self) -> str:
        return f"/poros/api/v2/tenants/{self.ctx.tenant}"

    def list_resources(
        self,
        type: str | None = None,
        *,
        owner: str | None = None,
        tags: list[str] | None = None,
        details: bool = False,
    ) -> list[dict[str, Any]]:
        path = f"{self._poros}/resources" + (f"/{type}" if type else "")
        params: dict[str, str] = {}
        if owner:
            params["owner"] = owner
        if tags:
            params["tags"] = ",".join(tags)
        if details:
            params["includeDetails"] = "true"
        r = self._call("get", path, params=params)
        _raise_for(r, f"list {type or 'resources'}")
        body = r.json()
        if isinstance(body, dict):
            return body.get("resources") or []
        return body or []

    def get_resource(self, type: str, name: str) -> dict[str, Any] | None:
        r = self._call("get", f"{self._poros}/resources/{type}/{name}")
        if r.status_code == 404:
            return None
        _raise_for(r, f"get {type}/{name}")
        return r.json()

    def runtime(self, type: str, name: str) -> dict[str, Any]:
        r = self._call("get", f"{self._poros}/resources/{type}/{name}/runtime")
        _raise_for(r, f"runtime {type}/{name}")
        return r.json()

    def logs(self, type: str, name: str, *, node: str | None = None, tail: int = 100) -> Any:
        params: dict[str, str] = {"tailLines": str(tail)}
        if node:
            params["node"] = node
        r = self._call("get", f"{self._poros}/resources/{type}/{name}/log", params=params)
        _raise_for(r, f"logs {type}/{name}")
        try:
            return r.json()
        except ValueError:
            return r.text

    def apply(self, manifest: dict[str, Any]) -> dict[str, Any]:
        """Create or update a resource. Used where dataos-ctl isn't available (the dashboard pod)."""
        typ, name = manifest["type"], manifest["name"]
        body = _normalize_for_api(manifest)
        if self.get_resource(typ, name) is None:
            r = self._call("post", f"{self._poros}/resources", json=body)
        else:
            r = self._call("put", f"{self._poros}/resources/{typ}/{name}", json=body)
        _raise_for(r, f"apply {typ}/{name}")
        return r.json() if r.content else {}

    def delete_resource(self, type: str, name: str, *, cascade: bool = False) -> bool:
        params = {"cascade": "true"} if cascade else {}
        r = self._call("delete", f"{self._poros}/resources/{type}/{name}", params=params)
        if r.status_code == 404:
            return False
        _raise_for(r, f"delete {type}/{name}")
        return True

    # ---------------- Heimdall ----------------
    def whoami(self) -> dict[str, Any]:
        if not self.ctx.user_id:
            raise DataOSError("current user id unknown (not in the dataos-ctl cred file or env)")
        r = self._call("get", f"/heimdall/api/v1/users/{self.ctx.user_id}")
        _raise_for(r, "get user")
        return r.json()

    def list_users(self) -> list[dict[str, str]]:
        """DataOS people (and service users): id, name, email, type. Needs read-users in Heimdall.
        The email is only used to resolve what someone typed to an id; pages show names and ids, never emails."""
        r = self._call("get", "/heimdall/api/v1/users")
        _raise_for(r, "list users")
        body = r.json()
        items = body if isinstance(body, list) else (body.get("items") or body.get("users") or [])
        out = [{"id": u["id"], "name": u.get("name") or "", "email": u.get("email") or "", "type": u.get("type") or ""}
               for u in items if u.get("id")]
        return sorted(out, key=lambda u: (u["type"] != "person", u["id"]))  # people first

    def list_apikeys(self, user_id: str | None = None) -> list[dict[str, Any]]:
        uid = user_id or self.ctx.user_id
        r = self._call("get", f"/heimdall/api/v1/users/{uid}/tokens")
        _raise_for(r, "list api keys")
        keys = r.json() or []
        for k in keys:  # never hand key material back from a list call
            if isinstance(k.get("data"), dict):
                k["data"].pop("apikey", None)
            # keys DataOS made itself (e.g. the run-as key apps start with): deleting one breaks your apps
            k["system"] = "poros apikey" in (k.get("description") or "").lower()
        return keys

    def create_apikey(self, name: str, *, duration: str = "720h", user_id: str | None = None,
                      description: str = "") -> dict[str, Any]:
        body = {"type": "apikey", "name": name, "duration": duration, "description": description}
        if not re.fullmatch(r"[A-Za-z0-9_:]+", name):  # Heimdall's rule (heimdall-service constant.TokenNameRegexp)
            raise DataOSError(f"API key name '{name}' is invalid: use letters, digits and underscore only (no hyphen)")
        uid = user_id or self.ctx.user_id  # dataos-ctl always sends it; without it Heimdall says "user not found"
        if not uid:
            raise DataOSError("cannot create an API key: the current user is unknown")
        body["user_id"] = uid
        r = self._call("post", "/heimdall/api/v1/tokens", json=body)
        _raise_for(r, "create api key")
        return r.json()

    def delete_apikey(self, name: str) -> None:
        r = self._call("delete", f"/heimdall/api/v1/tokens/{name}")
        _raise_for(r, "delete api key")


def _normalize_for_api(manifest: dict[str, Any]) -> dict[str, Any]:
    """Poros' REST body carries the type-specific block under ``spec`` (see dataos-sdk-go poros.ResourceApi).

    Manifests written for dataos-ctl sometimes put it under a key named after the type (``grant:``).
    """
    m = dict(manifest)
    typ = m.get("type")
    if "spec" not in m and typ in m:
        m["spec"] = m.pop(typ)
    return m
