"""Identify a caller from a DataOS token or API key via Heimdall (POST /heimdall/api/v1/authorize).

Shared by the gateway and the dashboard. Same contract databots' hub uses
(databots/hub/databots_hub/api/providers.py): body {"token": ...}; reply {allow, valid, result: {id, tags}}.
"""

from __future__ import annotations

import hashlib
import threading
import time
from dataclasses import dataclass, field

import httpx


class IdentityUnavailable(RuntimeError):
    """Heimdall could not be reached or gave a non-verdict reply (fail closed, but say so)."""


@dataclass(frozen=True)
class Identity:
    user_id: str
    tags: tuple[str, ...] = field(default_factory=tuple)

    @property
    def roles(self) -> list[str]:
        return [t for t in self.tags if t.startswith("roles:")]

    @property
    def is_operator(self) -> bool:
        return "roles:id:operator" in self.tags


class Heimdall:
    ttl = 60.0

    def __init__(self, base_url: str, *, transport: httpx.BaseTransport | None = None):
        # base_url like https://<fqdn>  (Heimdall lives at /heimdall)
        self.url = base_url.rstrip("/") + "/heimdall/api/v1/authorize"
        self.http = httpx.Client(timeout=10, transport=transport, headers={"User-Agent": "dapp/0.1"})
        self._cache: dict[str, tuple[float, Identity | None]] = {}
        self._lock = threading.Lock()

    def identify(self, token: str | None) -> Identity | None:
        if not token:
            return None
        key = hashlib.sha256(token.encode()).hexdigest()
        now = time.monotonic()
        with self._lock:
            hit = self._cache.get(key)
            if hit and hit[0] > now:
                return hit[1]
        try:
            r = self.http.post(self.url, json={"token": token})
        except httpx.HTTPError as e:
            raise IdentityUnavailable(f"heimdall unreachable: {e.__class__.__name__}") from e
        try:
            body = r.json()
        except ValueError as e:
            raise IdentityUnavailable(f"heimdall replied HTTP {r.status_code} without JSON") from e
        if not isinstance(body, dict) or ("valid" not in body and "allow" not in body):
            raise IdentityUnavailable(f"heimdall replied HTTP {r.status_code} without a verdict")
        data = body.get("result") or {}
        ident: Identity | None = None
        if body.get("allow") and body.get("valid") and data.get("id"):
            ident = Identity(str(data["id"]), tuple(t for t in data.get("tags") or [] if isinstance(t, str)))
        with self._lock:
            self._cache = {k: v for k, v in self._cache.items() if v[0] > now}
            self._cache[key] = (now + self.ttl, ident)
        return ident


def token_from_headers(headers) -> str | None:
    """Accept `Authorization: Bearer <jwt|apikey>` or an `apikey` header (what dataos-ctl sends)."""
    auth = headers.get("authorization") or ""
    if auth.lower().startswith("bearer "):
        return auth[7:].strip() or None
    return (headers.get("apikey") or "").strip() or None
