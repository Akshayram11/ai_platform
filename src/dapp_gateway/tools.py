"""MCP tools through the gateway.

A tool is an MCP server (Streamable HTTP) registered by its owner. Agents and apps never call it directly:

    POST|GET|DELETE  /mcp/<tenant>/<tool>     the MCP endpoint callers use

The gateway
  * checks who is calling: a DataOS user (token or API key, verified by Heimdall) or a dapp app (its gateway key),
  * checks the tool's allow list (the owner always may; "*" = any signed-in DataOS user; "app:<name>" = that app),
  * injects the tool's own credential, which only the gateway can decrypt (callers and apps never see it),
  * tells the tool who is calling (x-dapp-user) and records every JSON-RPC request: who, which function, ok or not.

Admin API (DataOS token or API key):
    POST   /admin/tools                         register or update {tenant, name, url, ...}
    GET    /admin/tools                         tools you own or may use
    GET    /admin/tools/{tenant}/{name}
    DELETE /admin/tools/{tenant}/{name}
    POST   /admin/tools/{tenant}/{name}/access  {allow: [...], deny: [...]}
    GET    /admin/tool-usage?tool=&since_hours=&group_by=tool,caller,tool_name,day
"""

from __future__ import annotations

import ipaddress
import json
import logging
import os
import re
import secrets
import time
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import urlsplit

import httpx
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel
from starlette.background import BackgroundTask

from dapp.auth import Heimdall, Identity, IdentityUnavailable, token_from_headers

from . import oauth
from .store import KEY_PREFIX, TOOL_GROUPABLE, Store, Tool, tool_view

log = logging.getLogger("dapp.gateway.tools")

NAME_RE = re.compile(r"^[a-z0-9]([a-z0-9-]{0,46}[a-z0-9])?$")
HEADER_RE = re.compile(r"^[A-Za-z0-9-]{1,64}$")
ALLOW_RE = re.compile(r"^(\*|app:\*|app:[a-z0-9-]{1,48}|[A-Za-z0-9_.@-]{1,128})$")
FORWARD_REQ = ("content-type", "accept", "mcp-session-id", "mcp-protocol-version", "last-event-id")
FORWARD_RESP = ("content-type", "mcp-session-id", "cache-control")
MAX_BODY = 1_000_000          # a JSON-RPC request; tool results can be bigger, they are streamed back untouched
SCAN_LIMIT = 256_000          # how much of a response is read to tell ok from error


class Vault:
    """Encrypts tool credentials at rest with DAPP_GATEWAY_SECRET_KEY (a Fernet key, from the gateway's secret).
    Without that key the gateway still proxies tools that need no credential, and refuses to store one."""

    def __init__(self, key: str | None = None):
        key = key if key is not None else os.environ.get("DAPP_GATEWAY_SECRET_KEY", "")
        self._f = None
        if key:
            from cryptography.fernet import Fernet

            self._f = Fernet(key.encode())

    @property
    def ready(self) -> bool:
        return self._f is not None

    def seal(self, value: str) -> str:
        if self._f is None:
            raise HTTPException(503, "the gateway has no DAPP_GATEWAY_SECRET_KEY, so it cannot store tool credentials")
        return self._f.encrypt(value.encode()).decode()

    def open(self, token: str) -> str:
        if self._f is None:
            raise HTTPException(503, "the gateway lost DAPP_GATEWAY_SECRET_KEY: this tool's credential can't be read")
        return self._f.decrypt(token.encode()).decode()


def internal_hosts() -> set[str]:
    return {h.strip().lower() for h in os.environ.get("DAPP_TOOL_INTERNAL_HOSTS", "").split(",") if h.strip()}


def check_tool_url(url: str) -> str:
    """Tools must be https on a public name. Plain http, localhost and private addresses only for hosts listed in
    DAPP_TOOL_INTERNAL_HOSTS: otherwise anyone could register a "tool" that reads the gateway's own network."""
    parts = urlsplit(url.strip())
    host = (parts.hostname or "").lower()
    if parts.scheme not in ("https", "http") or not host or parts.username or parts.password:
        raise HTTPException(422, "tool url must be http(s)://host/path, without user:password")
    if host in internal_hosts():
        return url.strip()
    if parts.scheme != "https":
        raise HTTPException(422, f"tool url must be https (or {host} must be in DAPP_TOOL_INTERNAL_HOSTS)")
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        ip = None
    if host == "localhost" or host.endswith((".localhost", ".local", ".internal", ".svc", ".cluster.local")) or (
            ip is not None and not ip.is_global):
        raise HTTPException(422, f"{host} is not a public address; ask the gateway admin to list it in "
                                 "DAPP_TOOL_INTERNAL_HOSTS")
    return url.strip()


def jsonrpc_requests(body: bytes) -> list[dict]:
    """The requests (messages with an id) in a JSON-RPC body: one object or a batch. Notifications are not counted."""
    try:
        msg = json.loads(body)
    except ValueError:
        return []
    items = msg if isinstance(msg, list) else [msg]
    return [m for m in items if isinstance(m, dict) and "method" in m and "id" in m]


def response_errors(content_type: str, data: bytes) -> set[str]:
    """Ids (as JSON text) of JSON-RPC replies that failed: a protocol error, or a tool result marked isError."""
    msgs: list[Any] = []
    if "text/event-stream" in content_type:
        for line in data.decode(errors="replace").splitlines():
            if line.startswith("data:"):
                try:
                    msgs.append(json.loads(line[5:].strip()))
                except ValueError:
                    pass
    else:
        try:
            msgs.append(json.loads(data))
        except ValueError:
            return set()
    flat = [m for x in msgs for m in (x if isinstance(x, list) else [x]) if isinstance(m, dict)]
    return {json.dumps(m.get("id")) for m in flat
            if "error" in m or (isinstance(m.get("result"), dict) and m["result"].get("isError") is True)}


class ToolRequest(BaseModel):
    tenant: str
    name: str
    url: str
    description: str = ""
    auth_header: str = ""            # e.g. Authorization; empty = the tool needs no credential
    auth_value: str | None = None    # e.g. "Bearer abc"; None keeps the stored one, "" removes it
    allow: list[str] | None = None


class AccessRequest(BaseModel):
    allow: list[str] = []
    deny: list[str] = []


class OAuthSetup(BaseModel):
    redirect_uri: str                # the dashboard's callback; the MCP server sends people back there
    client_id: str = ""              # only for servers without dynamic client registration
    client_secret: str = ""
    scope: str | None = None         # None: what the server says it supports


class OAuthStart(BaseModel):
    back: str = ""                   # a dashboard page to return to afterwards


class OAuthFinish(BaseModel):
    state: str
    code: str


def connect_url(name: str) -> str:
    base = os.environ.get("DAPP_DASHBOARD_URL") or f"https://{os.environ.get('DATAOS_FQDN', '')}/dapp"
    return f"{base.rstrip('/')}/tools?connect={name}"


def not_connected(t: Tool, person: str) -> JSONResponse:
    """What a caller gets when the person hasn't signed in to an OAuth tool (or their sign-in ran out)."""
    link = connect_url(t.name)
    who = person or "the person using this app"
    return JSONResponse({"error": {"type": "tool_not_connected", "tool": t.name, "connect_url": link,
                                   "message": f"{who} has not connected {t.name}. Connect it once: {link}"}},
                        status_code=401)


def mount_tools(api: FastAPI, *, store: Store, heimdall: Heimdall | None, caller: Callable[..., Identity],
                admins: set[str], upstream: httpx.AsyncClient, clean_user: Callable[[str | None], str],
                vault: Vault | None = None, oauth_http: httpx.Client | None = None) -> None:
    vault = vault or Vault()
    oauth_http = oauth_http or httpx.Client(timeout=httpx.Timeout(20, connect=5), follow_redirects=False)

    def find(tenant: str, name: str) -> Tool:
        t = store.get_tool(tenant, name)
        if t is None:
            raise HTTPException(404, f"no tool {tenant}/{name}")
        return t

    def require_owner(t: Tool, ident: Identity) -> None:
        if ident.user_id != t.owner and ident.user_id not in admins:
            raise HTTPException(403, f"{ident.user_id} is not the owner of tool {t.tenant}/{t.name} ({t.owner} is)")

    def allowed(t: Tool) -> list[str]:
        return json.loads(t.allow or "[]")

    def may_use(t: Tool, user_id: str) -> bool:
        a = allowed(t)
        return user_id == t.owner or user_id in admins or user_id in a or "*" in a

    def clean_allow(items: list[str]) -> list[str]:
        bad = [i for i in items if not ALLOW_RE.match(i)]
        if bad:
            raise HTTPException(422, f"not a user id, '*' or app:<name>: {', '.join(bad)}")
        return items

    # ------------------------------------------------------------ admin
    @api.post("/admin/tools")
    def save(body: ToolRequest, ident: Identity = Depends(caller)) -> dict[str, Any]:
        if not NAME_RE.match(body.name):
            raise HTTPException(422, "tool name: lower-case letters, digits and '-', at most 48")
        if body.auth_header and not HEADER_RE.match(body.auth_header):
            raise HTTPException(422, "auth_header must be a plain header name, e.g. Authorization")
        if body.auth_value and not body.auth_header:
            raise HTTPException(422, "auth_value needs auth_header (which header carries it)")
        url = check_tool_url(body.url)
        existing = store.get_tool(body.tenant, body.name)
        if existing is not None:
            require_owner(existing, ident)
        secret = None if body.auth_value is None else (vault.seal(body.auth_value) if body.auth_value else "")
        header = body.auth_header
        if body.auth_value is None and not header and existing is not None:
            header = existing.auth_header        # credential kept, so keep the header it goes in
        t = store.save_tool(tenant=body.tenant, name=body.name, owner=existing.owner if existing else ident.user_id,
                            url=url, description=body.description[:512], auth_header=header,
                            auth_secret=secret, allow=None if body.allow is None else clean_allow(body.allow))
        if body.auth_value and (t.auth_mode or "key") == "oauth":       # a key given: back to one key for everyone
            store.set_tool_oauth(body.tenant, body.name, mode="key", sealed="")
            t = store.get_tool(body.tenant, body.name)
        log.info("%s tool %s/%s by %s", "updated" if existing else "registered", body.tenant, body.name,
                 ident.user_id)
        return tool_view(t, full=True)

    @api.get("/admin/tools")
    def list_tools(tenant: str | None = None, ident: Identity = Depends(caller)) -> list[dict]:
        out = []
        for t in store.list_tools():
            if (tenant and t.tenant != tenant) or not may_use(t, ident.user_id):
                continue
            row = {**tool_view(t, full=ident.user_id in (t.owner, *admins)), "mine": t.owner == ident.user_id}
            if (t.auth_mode or "key") == "oauth":
                row["connected"] = store.get_token(t.tenant, t.name, ident.user_id) is not None
                if row["mine"]:
                    row["connected_users"] = store.connected_users(t.tenant, t.name)
            out.append(row)
        return out

    @api.get("/admin/tools/{tenant}/{name}")
    def get_tool(tenant: str, name: str, ident: Identity = Depends(caller)) -> dict[str, Any]:
        t = find(tenant, name)
        if not may_use(t, ident.user_id):
            raise HTTPException(404, f"no tool {tenant}/{name}")  # don't reveal tools you can't use
        return tool_view(t, full=ident.user_id in (t.owner, *admins))

    @api.delete("/admin/tools/{tenant}/{name}")
    def remove(tenant: str, name: str, ident: Identity = Depends(caller)) -> dict[str, Any]:
        require_owner(find(tenant, name), ident)
        log.info("deleted tool %s/%s by %s", tenant, name, ident.user_id)
        return {"deleted": store.delete_tool(tenant, name), "tool": f"{tenant}/{name}"}

    @api.post("/admin/tools/{tenant}/{name}/access")
    def access(tenant: str, name: str, body: AccessRequest, ident: Identity = Depends(caller)) -> dict[str, Any]:
        t = find(tenant, name)
        require_owner(t, ident)
        now_allowed = [a for a in allowed(t) if a not in set(body.deny)] + clean_allow(body.allow)
        return {"tool": f"{tenant}/{name}", "owner": t.owner, "allow": store.set_tool_allow(tenant, name, now_allowed)}

    @api.get("/admin/tool-usage")
    def usage(tool: str | None = None, tenant: str | None = None, since_hours: int = 720,
              group_by: str = "tool,caller", all_users: bool = False,
              ident: Identity = Depends(caller)) -> dict[str, Any]:
        owner = None if (all_users and ident.user_id in admins) else ident.user_id
        wanted = [g for g in (x.strip() for x in group_by.split(",")) if g in TOOL_GROUPABLE] or ["tool"]
        rows = store.tool_summary(owner=owner, tool=tool, tenant=tenant, since_hours=since_hours, group_by=wanted)
        return {"owner": owner or "*", "since_hours": since_hours, "rows": rows,
                "total": {"calls": sum(r["calls"] for r in rows), "errors": sum(r["errors"] for r in rows)}}

    # ------------------------------------------------------------ OAuth MCP servers: each person signs in
    def oauth_meta(t: Tool) -> dict[str, Any]:
        if (t.auth_mode or "key") != "oauth" or not t.oauth:
            raise HTTPException(409, f"tool {t.tenant}/{t.name} does not use sign-in (OAuth)")
        return json.loads(vault.open(t.oauth))

    def keep_tokens(t: Tool, person: str, reply: dict[str, Any], *, first: bool) -> str:
        access = reply["access_token"]
        expires = None
        if isinstance(reply.get("expires_in"), int | float):
            expires = datetime.now(UTC) + timedelta(seconds=float(reply["expires_in"]))
        refresh = reply.get("refresh_token")
        store.save_token(tenant=t.tenant, tool=t.name, user=person, access=vault.seal(access),
                         refresh=vault.seal(refresh) if refresh else ("" if first else None), expires=expires,
                         scope=str(reply.get("scope") or ""))
        return access

    def person_token(t: Tool, person: str) -> str | None:
        """This person's access token for the tool, refreshed when it is about to run out. None: not connected."""
        tok = store.get_token(t.tenant, t.name, person)
        if tok is None:
            return None
        expires = tok.expires.replace(tzinfo=UTC) if tok.expires and tok.expires.tzinfo is None else tok.expires
        if expires is None or expires > datetime.now(UTC) + timedelta(seconds=60):
            return vault.open(tok.access)
        if not tok.refresh:
            store.delete_token(t.tenant, t.name, person)
            return None
        try:
            reply = oauth.refresh(oauth_meta(t), vault.open(tok.refresh), oauth_http)
        except (oauth.OAuthError, httpx.HTTPError) as e:
            log.info("refresh of %s/%s for %s failed: %s", t.tenant, t.name, person, e)
            store.delete_token(t.tenant, t.name, person)
            return None
        return keep_tokens(t, person, reply, first=False)

    @api.post("/admin/tools/{tenant}/{name}/oauth/setup")
    def oauth_setup(tenant: str, name: str, body: OAuthSetup, ident: Identity = Depends(caller)) -> dict[str, Any]:
        """Find out how the tool's server signs people in and register the gateway with it. Owner only."""
        t = find(tenant, name)
        require_owner(t, ident)
        redirect = check_tool_url(body.redirect_uri)
        try:
            meta = oauth.discover(t.url, oauth_http, check_tool_url)
        except oauth.NoSignIn:
            return {"tool": f"{tenant}/{name}", "auth_mode": t.auth_mode or "key", "needs_sign_in": False}
        except (oauth.OAuthError, httpx.HTTPError) as e:
            raise HTTPException(422, f"could not set up sign-in for {name}: {e}")
        try:
            reg = ({"client_id": body.client_id, "client_secret": body.client_secret,
                    "auth_method": "client_secret_basic" if body.client_secret else "none"}
                   if body.client_id else oauth.register(meta, redirect, oauth_http))
        except (oauth.OAuthError, httpx.HTTPError) as e:
            raise HTTPException(422, f"could not register with {meta['issuer']}: {e}")
        meta.update(reg, redirect_uri=redirect)
        if body.scope is not None:
            meta["scope"] = body.scope
        store.set_tool_oauth(tenant, name, mode="oauth", sealed=vault.seal(json.dumps(meta)))
        log.info("tool %s/%s now signs people in through %s (by %s)", tenant, name, meta["issuer"], ident.user_id)
        return {"tool": f"{tenant}/{name}", "auth_mode": "oauth", "needs_sign_in": True, "issuer": meta["issuer"],
                "registered": not body.client_id}

    @api.post("/admin/tools/{tenant}/{name}/oauth/start")
    def oauth_start(tenant: str, name: str, body: OAuthStart, ident: Identity = Depends(caller)) -> dict[str, Any]:
        """Where to send this person's browser to sign in. Anyone who may use the tool connects their own account."""
        t = find(tenant, name)
        if not may_use(t, ident.user_id):
            raise HTTPException(404, f"no tool {tenant}/{name}")
        meta = oauth_meta(t)
        verifier, challenge = oauth.pkce()
        state = secrets.token_urlsafe(24)
        store.add_pending(state=state, tenant=tenant, tool=name, user=ident.user_id, verifier=vault.seal(verifier),
                          back=body.back)
        return {"authorize_url": oauth.authorize_url(meta, state=state, challenge=challenge)}

    @api.post("/admin/oauth/finish")
    def oauth_finish(body: OAuthFinish, ident: Identity = Depends(caller)) -> dict[str, Any]:
        """The server sent the person back with a code: swap it for their tokens and keep them (encrypted)."""
        p = store.pop_pending(body.state)
        if p is None or p.user != ident.user_id:
            raise HTTPException(400, "this sign-in expired or was started by someone else; connect again")
        t = find(p.tenant, p.tool)
        try:
            reply = oauth.exchange(oauth_meta(t), code=body.code, verifier=vault.open(p.verifier), http=oauth_http)
        except (oauth.OAuthError, httpx.HTTPError) as e:
            raise HTTPException(502, f"{t.name} did not accept the sign-in: {e}")
        keep_tokens(t, p.user, reply, first=True)
        log.info("%s connected tool %s/%s", p.user, t.tenant, t.name)
        return {"tool": t.name, "tenant": t.tenant, "connected": True, "back": p.back}

    @api.get("/admin/tools/{tenant}/{name}/oauth/me")
    def oauth_me(tenant: str, name: str, ident: Identity = Depends(caller)) -> dict[str, Any]:
        t = find(tenant, name)
        if not may_use(t, ident.user_id):
            raise HTTPException(404, f"no tool {tenant}/{name}")
        tok = store.get_token(tenant, name, ident.user_id)
        return {"tool": name, "connected": tok is not None, "scope": tok.scope if tok else "",
                "expires": tok.expires.isoformat() if tok and tok.expires else None}

    @api.delete("/admin/tools/{tenant}/{name}/oauth/me")
    def oauth_disconnect(tenant: str, name: str, ident: Identity = Depends(caller)) -> dict[str, Any]:
        find(tenant, name)
        return {"tool": name, "disconnected": store.delete_token(tenant, name, ident.user_id) > 0}

    # ------------------------------------------------------------ the MCP endpoint
    def who(request: Request, t: Tool) -> tuple[str, str]:
        """(caller, end_user). An app proves itself with its gateway key, a person with a DataOS token/API key."""
        token = request.headers.get("x-api-key") or token_from_headers(request.headers) or ""
        if token.startswith(KEY_PREFIX):
            k = store.lookup(token)
            if k is None:
                raise HTTPException(401, "invalid or revoked dapp app key")
            if k.app.startswith("user:"):
                raise HTTPException(403, "a personal LLM key can't call MCP tools; use your DataOS API key")
            if k.tenant != t.tenant or not ({f"app:{k.app}", "app:*"} & set(allowed(t))):
                raise HTTPException(403, f"app {k.app} may not use tool {t.tenant}/{t.name}; its owner can allow "
                                         f"it with: dapp tool allow {t.name} app:{k.app}")
            return f"app:{k.app}", clean_user(request.headers.get("x-dapp-user"))
        if heimdall is None:
            raise HTTPException(503, "DATAOS_FQDN not configured")
        try:
            ident = heimdall.identify(token or None)
        except IdentityUnavailable as e:
            raise HTTPException(503, f"DataOS identity check unavailable: {e}")
        if ident is None:
            raise HTTPException(401, "a DataOS token or API key (or a dapp app key) is required")
        if not may_use(t, ident.user_id):
            raise HTTPException(403, f"{ident.user_id} may not use tool {t.tenant}/{t.name}; ask {t.owner}")
        return ident.user_id, ""

    @api.api_route("/mcp/{tenant}/{name}", methods=["GET", "POST", "DELETE"])
    async def mcp_proxy(tenant: str, name: str, request: Request):
        t = find(tenant, name)
        caller_id, end_user = who(request, t)
        body = await request.body()
        if len(body) > MAX_BODY:
            raise HTTPException(413, f"request body over {MAX_BODY} bytes")
        reqs = jsonrpc_requests(body) if request.method == "POST" else []

        headers = {h: request.headers[h] for h in FORWARD_REQ if h in request.headers}
        # tell the tool who is calling (it can't verify; we did). Clients can't set these: only FORWARD_REQ passes.
        headers["x-dapp-user"] = end_user or caller_id
        headers["x-dapp-caller"] = caller_id              # a DataOS user id, or app:<name> for an app's gateway key
        if end_user:
            headers["x-dapp-end-user"] = end_user         # the person an app says it acts for (the app vouches)
        signed_in = (t.auth_mode or "key") == "oauth"
        person = ""
        if signed_in:
            # Each person's own sign-in. An app gets one only for the person it says it serves, and only when the
            # tool's owner allowed that app by name (app:* is not enough to borrow people's accounts).
            if caller_id.startswith("app:") and caller_id not in allowed(t):
                raise HTTPException(403, f"{caller_id} must be allowed by name to use {t.name} for its users; its "
                                         f"owner can run: dapp tool allow {t.name} {caller_id}")
            person = end_user if caller_id.startswith("app:") else caller_id
            access = await run_in_threadpool(person_token, t, person) if person else None
            if access is None:
                record(t, caller_id, end_user, reqs, 401, set(), time.monotonic(), failed=True)
                return not_connected(t, person)
            headers["Authorization"] = f"Bearer {access}"
        elif t.auth_header and t.auth_secret:
            headers[t.auth_header] = vault.open(t.auth_secret)
        started = time.monotonic()
        try:
            up = await upstream.send(upstream.build_request(request.method, t.url, headers=headers, content=body),
                                     stream=True)
        except httpx.HTTPError as e:
            record(t, caller_id, end_user, reqs, 502, set(), started, failed=True)
            raise HTTPException(502, f"tool {tenant}/{name} unreachable: {e.__class__.__name__}")
        if signed_in and up.status_code == 401:           # the server no longer takes this token: sign in again
            await up.aclose()
            store.delete_token(t.tenant, t.name, person)
            record(t, caller_id, end_user, reqs, 401, set(), started, failed=True)
            return not_connected(t, person)
        resp_headers = {h: up.headers[h] for h in FORWARD_RESP if h in up.headers}
        ctype = up.headers.get("content-type", "")
        seen = bytearray()

        async def relay():
            async for chunk in up.aiter_raw():
                if len(seen) < SCAN_LIMIT:
                    seen.extend(chunk[: SCAN_LIMIT - len(seen)])
                yield chunk

        async def finish():
            await up.aclose()
            record(t, caller_id, end_user, reqs, up.status_code, response_errors(ctype, bytes(seen)), started)

        return StreamingResponse(relay(), status_code=up.status_code, headers=resp_headers,
                                 background=BackgroundTask(finish))

    def record(t: Tool, caller_id: str, end_user: str, reqs: list[dict], status: int, errors: set[str],
               started: float, *, failed: bool = False) -> None:
        latency = int((time.monotonic() - started) * 1000)
        try:
            for m in reqs:
                params = m.get("params") if isinstance(m.get("params"), dict) else {}
                store.record_tool_call(
                    tenant=t.tenant, tool=t.name, owner=t.owner, caller=caller_id[:160], end_user=end_user,
                    method=str(m.get("method"))[:64],
                    tool_name=str(params.get("name", ""))[:128] if m.get("method") == "tools/call" else "",
                    status=status, error=failed or status >= 400 or json.dumps(m.get("id")) in errors, latency_ms=latency)
        except Exception:  # recording must never break the call
            log.exception("failed to record tool call")
