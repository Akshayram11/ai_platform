"""dapp-monitor: an MCP server (Streamable HTTP) for watching DataOS apps, reached only through the dapp gateway.

Every tool takes kind: app (default), service, worker, workflow or nilus. All read-only: nothing here changes anything.
  list_resources  what may be watched, with its state (list_apps: the same for apps);
  status          one resource: its state and its pods (Poros runtime) (app_status: the same for an app);
  recent_logs     the running pods' last lines (Poros /resources/<kind>/<name>/log?tailLines=);
  search_logs   DataOS's stored logs over any time range, old and crashed pods included, by keyword (platform-api);
  log_summary   how many lines, errors and warnings in a window, and which pods wrote them.

Whose eyes: the pod runs as its owner (DataOS gives it DATAOS_RUN_AS_USER / DATAOS_RUN_AS_APIKEY), so it can read what the
owner can read, and no more. MONITOR_ALLOW narrows it:
  "tenant"  the owner watches everything DataOS lets them read; anyone else (the gateway says who: x-dapp-user) only the
            resources they own themselves, plus MONITOR_SHARE ("vulcan:orders-dp, app:chatbot"): the owner's rights are never
            lent to another person;
  "*"       only what the owner owns;
  a list    "chatbot, service:orders-api" (a bare name is an app).
The gateway decides who may call this tool (`dapp tool allow dapp-monitor ...`) and records every call; it reaches us with
TOOL_TOKEN and x-dapp-caller. Without TOOL_TOKEN we answer nothing.
"""

from __future__ import annotations

import contextvars
import hmac
import json
import logging
import os

import httpx
import uvicorn
from logsearch import DEFAULT_VERSIONS, LogSearchError, redact, search
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.server.transport_security import TransportSecuritySettings

log = logging.getLogger("dapp-monitor")
TOKEN = os.environ.get("TOOL_TOKEN", "")
FQDN = os.environ.get("DATAOS_FQDN", "")
TENANT = os.environ.get("DATAOS_TENANT_ID", "")
OWNER = os.environ.get("DATAOS_RUN_AS_USER", "")
KEY = os.environ.get("DATAOS_RUN_AS_APIKEY", "")
_caller: contextvars.ContextVar[str] = contextvars.ContextVar("caller", default="")
_viewer: contextvars.ContextVar[str] = contextvars.ContextVar("viewer", default="")   # the person looking (x-dapp-user)

KINDS = tuple(DEFAULT_VERSIONS)          # the types that run pods; compute, depot, secret, volume... have no logs

mcp = MCPServer("dapp-monitor", instructions=(
    "Watch DataOS resources that run pods: kind app (default), service, vulcan (data products), minerva (query clusters), "
    "lakehouse, lakehouse-rustfs, nilus (pipelines), workflow, worker. Compute, depot, secret, volume and grants have no logs. list_resources: what you may look at. status: is it running, which pods. recent_logs: the running pods' last "
    "lines (what is happening now). search_logs: stored logs over any time range, including pods that crashed or were replaced, "
    "by keyword ('error', 'timeout', '*') with a window (since='1h') or a range (start/end in ISO UTC like "
    "2026-11-26T16:00:00Z, or '2h ago'). log_summary: counts of errors and warnings first, to know where to look. Times are UTC. "
    "Quote the log lines you base an answer on, and say when nothing was found."))


def _base() -> str:
    if not (FQDN and TENANT and KEY):
        raise ToolError("this server is not set up: DATAOS_FQDN, DATAOS_TENANT_ID and the pod's DataOS key are needed")
    return f"https://{FQDN}"


def _headers() -> dict[str, str]:
    return {"apikey": KEY}


def _kind(kind: str) -> str:
    k = (kind or "app").strip().lower()
    if k not in KINDS:
        raise ToolError(f"kind is one of {', '.join(KINDS)}")
    return k


def _mode() -> str:
    """MONITOR_ALLOW: "tenant" = everything DataOS lets the owner read (for the owner; others see their own, below);
    "*" = what the owner owns; or a list "chatbot, service:orders-api" (a bare name is an app). LOGS_APPS: older name."""
    raw = (os.environ.get("MONITOR_ALLOW") or os.environ.get("LOGS_APPS") or "*").strip()
    return "tenant" if raw.lower() == "tenant" else ("*" if raw in ("", "*") else "list")


def _pairs(raw: str) -> set[tuple[str, str]]:
    out = set()
    for item in (raw or "").split(","):
        item = item.strip()
        if item and item != "*":
            kind, _, name = item.rpartition(":")
            out.add((kind or "app", name))
    return out


def allowed() -> set[tuple[str, str]] | None:
    """The listed (kind, name) pairs in list mode; None in the other modes."""
    return _pairs(os.environ.get("MONITOR_ALLOW") or os.environ.get("LOGS_APPS") or "") if _mode() == "list" else None


def shared() -> set[tuple[str, str]]:
    """MONITOR_SHARE: resources anyone who may use this tool may watch, on top of their own (tenant mode)."""
    return _pairs(os.environ.get("MONITOR_SHARE", ""))


def viewer() -> str:
    """Who is looking: the person the gateway (or the app, which the gateway vouches for) says, else the calling app."""
    return (_viewer.get() or _caller.get() or "").removeprefix("users:id:")


def is_owner_view() -> bool:
    v = viewer()
    return not v or v == OWNER          # no header: a direct test or the owner's own tools


def _poros(kind: str, path: str = "", **params) -> httpx.Response:
    return httpx.get(f"{_base()}/poros/api/v2/tenants/{TENANT}/resources/{kind}{'/' + path if path else ''}",
                     params=params, headers=_headers(), timeout=30)


def _listed(kind: str, owner: str | None = OWNER) -> list[dict]:
    r = _poros(kind, **({"owner": owner} if owner else {}))
    if not r.is_success:
        raise ToolError(f"could not list {kind}s: HTTP {r.status_code}")
    body = r.json()
    return body.get("resources", []) if isinstance(body, dict) else body


def _check(name: str, kind: str) -> dict | None:
    """May the viewer watch this? list mode: it must be listed. "*": it must be the owner's. "tenant": the owner may watch
    whatever DataOS lets them read; anyone else only what they own themselves, plus MONITOR_SHARE. Returns the resource
    when it was fetched."""
    mode, allow = _mode(), allowed()
    if mode == "list":
        if (kind, name) not in allow:
            listed = ", ".join(f"{k}:{n}" for k, n in sorted(allow))
            raise ToolError(f"{kind} {name} is not one of the resources this server may watch: {listed}")
        return None
    r = _poros(kind, name)
    if r.status_code == 404:
        raise ToolError(f"no {kind} {name}; call list_resources(kind='{kind}')")
    if r.status_code in (401, 403):
        raise ToolError(f"DataOS doesn't let this server read {kind} {name}")
    if not r.is_success:
        raise ToolError(f"could not read {kind} {name}: HTTP {r.status_code}")
    res = r.json()
    owner = res.get("owner")
    if mode == "*" and owner != OWNER:
        raise ToolError(f"{kind} {name} is not a resource of {OWNER}; call list_resources")
    if mode == "tenant" and not is_owner_view() and owner != viewer() and (kind, name) not in shared():
        raise ToolError(f"{kind} {name} belongs to {owner}; you can watch your own resources here (call list_resources)")
    return res


def is_sidecar(container: str) -> bool:
    """DataOS adds a log/metrics shipper (vector) to every pod, named <container>-0-sc, plus tunnel containers on the hub."""
    return container.endswith("-sc") or container in ("tunnel-access",)


def main_containers(name: str, kind: str) -> list[str]:
    """The resource's own containers (Poros runtime: `containers`, not `sideCars`), on its dataplane pods: not DataOS's
    hub tunnel pod, not the log shipper. Their stored logs are the ones worth reading."""
    r = _poros(kind, f"{name}/runtime")
    if not r.is_success:
        return []
    found: list[str] = []
    for n in r.json().get("nodeDetails") or []:
        if n.get("dataplane") == "hub":
            continue
        for c in n.get("containers") or []:
            if c and not is_sidecar(c) and c not in found:
                found.append(c)
    return found


def _container_for(name: str, kind: str, container: str | None, include_sidecars: bool) -> str | None:
    if container or include_sidecars:
        return container
    mains = main_containers(name, kind)
    return mains[0] if len(mains) == 1 else None


@mcp.tool()
def list_resources(kind: str = "app") -> list[dict]:
    """The resources of one kind that can be watched, with owner and state. kind: app, service, vulcan, minerva, lakehouse,
    lakehouse-rustfs, nilus, workflow, worker."""
    kind = _kind(kind)
    allow, mode = allowed(), _mode()
    if mode == "list":
        rows = []
        for n in sorted(n for k, n in allow if k == kind):
            r = _poros(kind, n)
            body = r.json() if r.is_success else {}
            rows.append({"name": n, "kind": kind, "owner": body.get("owner"),
                         "status": body.get("status") if r.is_success else f"HTTP {r.status_code}",
                         "description": body.get("description") or ""})
        return rows
    if mode == "*":
        listed = _listed(kind)
    elif is_owner_view():
        listed = _listed(kind, owner=None)                       # the whole tenant, as far as DataOS lets the owner read
    else:
        share = {n for k, n in shared() if k == kind}
        listed = [a for a in _listed(kind, owner=None) if a.get("owner") == viewer() or a.get("name") in share]
    return sorted(({"name": a.get("name"), "kind": kind, "owner": a.get("owner"), "status": a.get("status"),
                    "description": a.get("description") or ""} for a in listed),
                  key=lambda x: (x["owner"] != (OWNER if is_owner_view() else viewer()), x["name"] or ""))


@mcp.tool()
def list_apps() -> list[dict]:
    """The apps that can be watched (same as list_resources(kind='app'))."""
    return list_resources("app")


@mcp.tool()
def status(name: str, kind: str = "app") -> dict:
    """One app or service (kind): its state (active, running, error) and its pods with their phase. Is it up now?"""
    kind = _kind(kind)
    res = _check(name, kind)
    if res is None:
        r = _poros(kind, name)
        if r.status_code == 404:
            raise ToolError(f"no {kind} {name}")
        if not r.is_success:
            raise ToolError(f"could not read {kind} {name}: HTTP {r.status_code}")
        res = r.json()
    st = res.get("status") if isinstance(res.get("status"), dict) else {"aggregateStatus": res.get("status")}
    rt = _poros(kind, f"{name}/runtime")
    pods = []
    if rt.is_success:
        for n in rt.json().get("nodeDetails") or []:
            pods.append({"pod": n.get("name"), "phase": n.get("phase"), "type": n.get("type"), "dataplane": n.get("dataplane")})
    return {"name": name, "kind": kind, "state": st.get("aggregateStatus"),
            "runtime": (st.get("runtimeState") or {}).get("status"), "error": st.get("error") or None,
            "owner": res.get("owner"), "pods": pods}


@mcp.tool()
def app_status(app: str) -> dict:
    """One app's state and pods (same as status(name, kind='app'))."""
    return status(app, "app")


@mcp.tool()
def recent_logs(name: str, kind: str = "app", tail: int = 100, pod: str | None = None, include_sidecars: bool = False) -> dict:
    """The running pods' last lines of an app or service (kind), now (no time range: for that use search_logs). tail 1-1000
    lines per container; pod: one pod's name (see status). DataOS's own sidecar containers are left out unless include_sidecars."""
    kind = _kind(kind)
    _check(name, kind)
    if not 1 <= int(tail) <= 1000:
        raise ToolError("tail is 1 to 1000")
    params = {"tailLines": int(tail), **({"node": pod} if pod else {})}
    r = _poros(kind, f"{name}/log", **params)
    if not r.is_success:
        raise ToolError(f"could not read the logs of {kind} {name}: HTTP {r.status_code}")
    try:
        body = r.json()
    except ValueError:
        return {"name": name, "kind": kind, "containers": [{"pod": pod, "container": None,
                                                            "lines": [redact(x) for x in r.text.splitlines()[-int(tail):]]}]}
    out = []
    for node in body.get("nodeLogs") or []:
        for c in node.get("logs") or []:
            if not include_sidecars and is_sidecar(c.get("name") or ""):
                continue
            out.append({"pod": node.get("name"), "container": c.get("name"),
                        "lines": [redact(str(x))[:4000] for x in (c.get("log") or [])][-int(tail):]})
    return {"name": name, "kind": kind, "containers": out}


@mcp.tool()
def search_logs(name: str, kind: str = "app", query: str = "*", since: str | None = None, start: str | None = None,
                end: str | None = None, stream: str | None = None, pod: str | None = None, limit: int = 100,
                offset: int = 0, container: str | None = None, include_sidecars: bool = False) -> dict:
    """Search the stored logs of an app or service (kind). query: a keyword or phrase ('error', 'Traceback', '*' for all).
    Time: since="1h" (30m, 24h, 7d), or start and end (ISO UTC like 2026-11-26T16:00:00Z, epoch seconds, or '2h ago');
    default the last hour. stream: stdout or stderr. pod: one pod's name. limit 1-1000 (default 100), offset to page. Only
    the resource's own container is searched (not DataOS's log shipper) unless include_sidecars or container is given.
    Returns total, and lines (time UTC, pod, container, stream, message), oldest first; credentials are masked."""
    kind = _kind(kind)
    _check(name, kind)
    log.info("search_logs %s %s query=%r caller=%s viewer=%s", kind, name, query, _caller.get() or "?", viewer() or "?")
    target = _container_for(name, kind, container, include_sidecars)
    try:
        out = search(_base(), TENANT, _headers(), name, kind=kind, query=query, since=since, start=start, end=end,
                     stream=stream, pod=pod, container=target, limit=limit, offset=offset)
    except LogSearchError as e:
        raise ToolError(str(e)) from e
    if not target and not include_sidecars and not container:      # several containers: keep only the resource's own lines
        before = len(out["lines"])
        out["lines"] = [x for x in out["lines"] if not is_sidecar(x.get("container") or "")]
        out["returned"] = len(out["lines"])
        if before != out["returned"]:
            out["note"] = f"{before - out['returned']} lines of DataOS's log shipper left out (include_sidecars=true to see them)"
    out["container"] = target or "all"
    return out


@mcp.tool()
def log_summary(name: str, kind: str = "app", since: str | None = "1h", start: str | None = None,
                end: str | None = None, include_sidecars: bool = False) -> dict:
    """A quick picture of an app's or service's logs in a window: total lines, lines matching error / warn / exception /
    timeout, and the pods that wrote them. Use it before search_logs to know where to look."""
    kind = _kind(kind)
    _check(name, kind)
    window = {"since": None if (start or end) else since, "start": start, "end": end,
              "container": _container_for(name, kind, None, include_sidecars)}
    try:
        everything = search(_base(), TENANT, _headers(), name, kind=kind, query="*", limit=1000, **window)
        counts = {word: search(_base(), TENANT, _headers(), name, kind=kind, query=word, limit=1, **window)["total"]
                  for word in ("error", "warn", "exception", "timeout")}
    except LogSearchError as e:
        raise ToolError(str(e)) from e
    pods: dict[str, int] = {}
    for line in everything["lines"]:
        pods[line["pod"] or "?"] = pods.get(line["pod"] or "?", 0) + 1
    return {"name": name, "kind": kind, "container": window["container"] or "all", "window": everything["query"],
            "total_lines": everything["total"],
            "matching": counts, "pods_in_first_1000_lines": pods,
            "first": everything["lines"][0]["time"] if everything["lines"] else None,
            "last": everything["lines"][-1]["time"] if everything["lines"] else None}


def build():
    hosts = [h.strip() for h in os.environ.get("TOOL_ALLOWED_HOSTS", "dapp-monitor:8080,dapp-monitor,localhost:8080")
             .split(",") if h.strip()]
    inner = mcp.streamable_http_app(stateless_http=True, json_response=True, host="0.0.0.0",
                                    transport_security=TransportSecuritySettings(allowed_hosts=hosts))

    async def app(scope, receive, send):
        if scope["type"] == "http":
            if scope["path"].endswith("/healthz"):
                await send({"type": "http.response.start", "status": 200,
                            "headers": [(b"content-type", b"application/json")]})
                return await send({"type": "http.response.body", "body": b'{"ok": true}'})
            headers = {k.decode().lower(): v.decode() for k, v in scope["headers"]}
            given = headers.get("authorization", "").removeprefix("Bearer ").strip()
            if not TOKEN or not hmac.compare_digest(given.encode(), TOKEN.encode()):
                body = json.dumps({"error": "dapp-monitor is reached through the dapp gateway"}).encode()
                await send({"type": "http.response.start", "status": 401,
                            "headers": [(b"content-type", b"application/json")]})
                return await send({"type": "http.response.body", "body": body})
            token = _caller.set(headers.get("x-dapp-caller", ""))
            vtoken = _viewer.set(headers.get("x-dapp-user", ""))
            try:
                return await inner(scope, receive, send)
            finally:
                _caller.reset(token)
                _viewer.reset(vtoken)
        return await inner(scope, receive, send)

    return app


app = build()

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="[dapp-monitor] %(message)s")
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", "8080")), log_level="warning")
