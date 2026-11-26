"""Search a DataOS resource's stored logs: any time range, old pods included, by keyword.

DataOS keeps every pod's output (Quickwit, behind platform-api), so this reaches logs that `dapp logs` (the running pod's tail)
cannot: yesterday's, a crashed pod's, "errors between 16:00 and 17:00". API, tested live on 2026-11-26:

    GET https://<fqdn>/platform-api/v2/tenants/<tenant>/resources/<type>/<version>/<name>/logs/search
        q=<keyword or *>  window=1h | start_timestamp=<epoch s>&end_timestamp=<epoch s>
        cgroup=<pod>  container=<name>  stream=stdout|stderr  limit=1..1000  offset=N

It runs as whoever's key it is given, so DataOS decides what may be read.

This file is self-contained (stdlib + httpx) on purpose: apps/dapp-monitor/app/logsearch.py is a copy of it, kept identical by
a test, so the MCP server behind the gateway and the dapp CLI behave the same.
"""
from __future__ import annotations

import re
import time
from datetime import UTC, datetime
from typing import Any

import httpx

# the resource types that run pods (so have logs), and the version their log search answers to (tested live 2026-11-26 for app,
# service, vulcan, minerva, lakehouse, lakehouse-rustfs). Compute, depot, secret, volume... run nothing and have no logs.
DEFAULT_VERSIONS = {"app": "v1alpha", "service": "v2alpha", "vulcan": "v1alpha", "minerva": "v1alpha", "lakehouse": "v1alpha",
                    "lakehouse-rustfs": "v1alpha", "nilus": "v1alpha", "workflow": "v2alpha", "worker": "v2alpha"}
WINDOW_RE = re.compile(r"^(\d{1,4})([smhd])$")
NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")
MAX_MESSAGE = 4000


class LogSearchError(ValueError):
    pass


_SECRETS = [
    (re.compile(r"(?i)\b(code|state|token|access_token|refresh_token|id_token|apikey|api_key|key|password|passwd|pwd|secret|"
                r"client_secret|signature|sig)=([^&\s\"']+)"), r"\1=•••"),
    (re.compile(r"(?i)\b(bearer|basic)\s+[A-Za-z0-9._~+/=-]{8,}"), r"\1 •••"),
    (re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b"), "•••jwt•••"),
    (re.compile(r"(?i)(\"(?:password|token|apikey|api_key|secret|authorization)\"\s*:\s*\")[^\"]+"), r"\1•••"),
]


def redact(text: str) -> str:
    """Hide what looks like a credential in a log line (?token=, Bearer ..., JWTs, "password": "...") before anyone, or any
    model, reads it."""
    for rx, repl in _SECRETS:
        text = rx.sub(repl, text)
    return text


def to_epoch(when: str | float, *, now: float | None = None) -> int:
    """A moment as Unix seconds: epoch seconds, an ISO time ('2026-11-26T16:00:00Z', '2026-11-26 16:00', UTC when no zone),
    or relative to now ('2h ago', '30m ago', 'now')."""
    now = time.time() if now is None else now
    if isinstance(when, (int, float)):
        return int(when)
    s = str(when).strip().lower()
    if s in ("", "now"):
        return int(now)
    if re.fullmatch(r"\d{9,11}(\.\d+)?", s):
        return int(float(s))
    m = re.fullmatch(r"(\d{1,4})\s*([smhd])\w*\s+ago", s)
    if m:
        return int(now - int(m.group(1)) * {"s": 1, "m": 60, "h": 3600, "d": 86400}[m.group(2)])
    try:
        dt = datetime.fromisoformat(str(when).strip().replace("Z", "+00:00").replace(" ", "T", 1))
    except ValueError as e:
        raise LogSearchError(f"can't read the time {when!r}: use ISO like 2026-11-26T16:00:00Z, epoch seconds, or '2h ago'") from e
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return int(dt.timestamp())


def build_params(*, query: str = "*", since: str | None = None, start: Any = None, end: Any = None, pod: str | None = None,
                 container: str | None = None, stream: str | None = None, limit: int = 100, offset: int = 0,
                 now: float | None = None) -> dict[str, Any]:
    """The query string for the API, checked. Either a window (since='1h') or a range (start, end); default the last hour."""
    params: dict[str, Any] = {"q": (query or "*").strip()[:500] or "*"}
    if start is not None or end is not None:
        if since:
            raise LogSearchError("give either since (like 1h) or start/end, not both")
        t0 = to_epoch(start if start is not None else "1h ago", now=now)
        t1 = to_epoch(end if end is not None else "now", now=now)
        if t1 <= t0:
            raise LogSearchError("end must be after start")
        if t1 - t0 > 31 * 86400:
            raise LogSearchError("search at most 31 days at a time")
        params.update(start_timestamp=t0, end_timestamp=t1)
    else:
        window = (since or "1h").strip().lower()
        m = WINDOW_RE.match(window)
        if not m:
            raise LogSearchError("since is a number and a unit: 30m, 1h, 24h, 7d")
        seconds = int(m.group(1)) * {"s": 1, "m": 60, "h": 3600, "d": 86400}[m.group(2)]
        if seconds > 31 * 86400:
            raise LogSearchError("search at most 31 days at a time")
        params["window"] = window
    if stream:
        if stream not in ("stdout", "stderr"):
            raise LogSearchError("stream is stdout or stderr")
        params["stream"] = stream
    if pod:
        params["cgroup"] = pod.strip()
    if container:
        params["container"] = container.strip()
    if not 1 <= int(limit) <= 1000:
        raise LogSearchError("limit is 1 to 1000")
    params["limit"], params["offset"] = int(limit), max(0, int(offset))
    return params


def shape(body: dict[str, Any]) -> dict[str, Any]:
    """The API's answer as a small, readable result: total, and one line per log entry (oldest first)."""
    data = body.get("data") or {}
    rows = data.get("results") or []
    if isinstance(rows, dict):                        # the v1 shape: grouped by pod/container
        rows = [{**{k: v for k, v in g.items() if k != "logs"}, **line} for g in rows.values() for line in g.get("logs") or []]
    lines = [{"time": r.get("time"), "pod": r.get("name"), "container": r.get("containerName"), "stream": r.get("stream"),
              "message": redact(str(r.get("message") or ""))[:MAX_MESSAGE]} for r in rows]
    lines.sort(key=lambda x: x["time"] or "")
    return {"total": data.get("total_hits", len(lines)), "offset": data.get("offset", 0), "limit": data.get("limit"),
            "returned": len(lines), "lines": lines}


def search(base_url: str, tenant: str, headers: dict[str, str], name: str, *, kind: str = "app", version: str | None = None,
           client: httpx.Client | None = None, timeout: float = 30, **query: Any) -> dict[str, Any]:
    """Search one resource's logs. `headers` carries the caller's DataOS key (apikey or Authorization)."""
    if not NAME_RE.match(name or ""):
        raise LogSearchError(f"{name!r} is not a resource name")
    if kind not in DEFAULT_VERSIONS:
        raise LogSearchError(f"kind is one of {', '.join(DEFAULT_VERSIONS)}")
    version = version or DEFAULT_VERSIONS[kind]
    params = build_params(**query)
    url = f"{base_url.rstrip('/')}/platform-api/v2/tenants/{tenant}/resources/{kind}/{version}/{name}/logs/search"
    own = client is None
    c = client or httpx.Client(timeout=timeout)
    try:
        r = c.get(url, params=params, headers={**headers, "accept": "application/json"})
    except httpx.HTTPError as e:
        raise LogSearchError(f"log search is not reachable: {e.__class__.__name__}") from e
    finally:
        if own:
            c.close()
    if r.status_code in (401, 403):
        raise LogSearchError(f"DataOS refused reading the logs of {kind} {name} (HTTP {r.status_code}): no permission")
    if r.status_code == 404:
        raise LogSearchError(f"no {kind} {name} (version {version}) in tenant {tenant}")
    if not r.is_success or not r.headers.get("content-type", "").startswith("application/json"):
        raise LogSearchError(f"log search failed: HTTP {r.status_code}")
    out = shape(r.json())
    out.update(resource=f"{kind}:{version}:{name}", query=params)
    return out
