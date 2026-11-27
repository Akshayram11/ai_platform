"""Dashboard server. Every DataOS call is made *as the signed-in user*, so Poros/Heimdall permissions apply.

Sign-in: automatic DataOS OIDC login (same as dapp apps with login: true). The browser is sent to DataOS,
comes back to /_dapp/callback, and the tokens stay server-side behind a session cookie (refreshed as needed).
API clients (dapp CLI, agents) send `Authorization: Bearer` / `apikey` headers to /api/*.
Without OIDC settings (local dev) it falls back to pasting a DataOS API key.

Env:
  DATAOS_FQDN, DATAOS_TENANT_ID       instance and default tenant
  DAPP_PROMETHEUS_URL                 Thanos/Prometheus query URL (compute usage)
  DAPP_GATEWAY_INTERNAL_URL           dapp gateway (LLM usage), default http://dapp-gateway:8080
  ROOT_PATH                           ingress path, e.g. /dapp
  DAPP_DEV=1                          local dev: use your dataos-ctl login when not signed in
"""

from __future__ import annotations

import json
import logging
import os
import re
import secrets
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

import httpx
import yaml
from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from pydantic import SecretStr, ValidationError

from dapp.agent_runtime.mcp_transport import Server, ToolError
from dapp.auth import Heimdall, IdentityUnavailable, token_from_headers
from dapp.claude import DEFAULTS as CLAUDE_DEFAULTS
from dapp.claude import ClaudeDeploy, instance_name
from dapp.client import DataOS, DataOSError
from dapp.config import Context, load_context
from dapp.ctl import ApiWriter
from dapp.models import (
    CATALOG,
    TOKENS_PER_GB_S,
    ModelDeploy,
    catalog_entry,
    defaults,
    defaults_for,
    estimate,
    explain_defaults,
    hf_quants,
    quality_of,
    rates,
    speed_of,
    tier_of,
    traits_of,
    with_hf_facts,
)
from dapp.pipelines import HOWS, SCHEDULE_WORDS, PipelineRequest
from dapp.pipelines import summary as pipeline_summary
from dapp.platform import (
    ID_RE,
    PipelineAccessError,
    Platform,
    PlatformError,
    gateway_internal_url,
    gateway_public_url,
)
from dapp.sources import PRESETS, SourceRequest, from_url, redact
from dapp.usage import (
    GatewayError,
    GatewayUsage,
    Prometheus,
    observability_url,
    parse_tokens,
    service_of,
    show_tokens,
)
from dapp_login import SCOPES, Login, Settings
from dapp_objects import Client as ObjectsClient
from dapp_objects import ObjectsError

from .agents import mount_agents

log = logging.getLogger("dapp.dashboard")
TEMPLATES = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))
SESSION_COOKIE = "dapp_session"
STATE_COOKIE = "dapp_login_state"
SESSION_TTL = 12 * 3600
LOGIN_TTL = 600


@dataclass
class Session:
    token: str
    user_id: str
    tenant: str
    csrf: str
    expires: float
    refresh_token: str | None = None
    token_expires: float = 0.0  # 0 = token doesn't expire (API key)


_UNREAD = object()


class SessionBox:
    """Sessions live in the browser, encrypted, not in this process: a deploy or a restart replaces the pod, and a
    session kept in memory would sign everyone out (and break sign-ins that were half done).

    The key comes from DAPP_SESSION_KEY or the OIDC client secret (a DataOS secret, the same on every pod). A sealed
    session can be longer than one cookie allows, so it is split over dapp_session, dapp_session_1, ..."""

    CHUNK = 3800
    MAX_CHUNKS = 4

    def __init__(self, key: bytes):
        import base64
        import hashlib

        from cryptography.fernet import Fernet

        self._f = Fernet(base64.urlsafe_b64encode(hashlib.sha256(b"dapp-dashboard-session:" + key).digest()))

    def seal(self, s: Session) -> str:
        import zlib

        return self._f.encrypt(zlib.compress(json.dumps(asdict(s)).encode())).decode()

    def open(self, value: str) -> Session | None:
        import zlib

        from cryptography.fernet import InvalidToken

        if not value:
            return None
        try:
            s = Session(**json.loads(zlib.decompress(self._f.decrypt(value.encode(), ttl=SESSION_TTL))))
        except (InvalidToken, ValueError, TypeError, zlib.error):
            return None
        return s if s.expires > time.time() else None

    @classmethod
    def names(cls) -> list[str]:
        return [SESSION_COOKIE] + [f"{SESSION_COOKIE}_{i}" for i in range(1, cls.MAX_CHUNKS)]

    def read(self, cookies) -> Session | None:
        return self.open("".join(cookies.get(n, "") for n in self.names()))

    def write(self, resp, s: Session, **cookie) -> None:
        value = self.seal(s)
        parts = [value[i:i + self.CHUNK] for i in range(0, len(value), self.CHUNK)]
        if len(parts) > self.MAX_CHUNKS:
            raise ValueError("session too large for cookies")
        for i, name in enumerate(self.names()):
            if i < len(parts):
                resp.set_cookie(name, parts[i], max_age=SESSION_TTL, **cookie)
            else:
                resp.delete_cookie(name, path=cookie.get("path", "/"))


def fmt_bytes(n: float | None) -> str:
    if n is None:
        return "–"
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if abs(n) < 1024:
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} PiB"


def fmt_num(n: float | None, digits: int = 0) -> str:
    if n is None:
        return "–"
    return f"{n:,.{digits}f}"


def sparkline(points: list[tuple[float, float]], w: int = 240, h: int = 40) -> str:
    if len(points) < 2:
        return ""
    vals = [v for _, v in points]
    lo, hi = min(vals), max(vals)
    span = (hi - lo) or 1
    step = w / (len(vals) - 1)
    coords = " ".join(f"{i * step:.1f},{h - 2 - (v - lo) / span * (h - 4):.1f}" for i, v in enumerate(vals))
    return (f'<svg class="spark" viewBox="0 0 {w} {h}" preserveAspectRatio="none" aria-hidden="true">'
            f'<polyline points="{coords}" fill="none" stroke="currentColor" stroke-width="1.5"/></svg>')


TEMPLATES.env.filters["bytes"] = fmt_bytes
def fmt_when(value: str | None) -> str:
    """2026-11-23T08:01:19.279926 (UTC, from the gateway) or ...Z (from DataOS) -> 23 Nov 2026, 08:01 UTC."""
    if not value:
        return "–"
    from datetime import UTC, datetime

    try:
        if isinstance(value, (int, float)):  # epoch seconds (immortal-obj)
            return datetime.fromtimestamp(value, tz=UTC).strftime("%d %b %Y, %H:%M UTC")
        return datetime.fromisoformat(str(value).replace("Z", "")).strftime("%d %b %Y, %H:%M UTC")
    except ValueError:
        return str(value)


def fmt_money(v: float | None) -> str:
    """An estimate, not a bill: cents for small amounts, whole numbers for big ones."""
    if v is None:
        return "–"
    cur = rates()["currency"]
    return f"<{cur}0.01" if v < 0.01 else (f"{cur}{v:.2f}" if v < 10 else f"{cur}{v:,.0f}")


TEMPLATES.env.filters["money"] = fmt_money
TEMPLATES.env.filters["num"] = fmt_num
TEMPLATES.env.filters["when"] = fmt_when
TEMPLATES.env.filters["tokens"] = show_tokens


def fmt_pretty(text: str) -> str:
    """JSON in a trace (messages, tool arguments and results) indented for reading; anything else as it is."""
    try:
        return json.dumps(json.loads(text), indent=2, ensure_ascii=False)
    except (TypeError, ValueError):
        return text


TEMPLATES.env.filters["pretty"] = fmt_pretty


def create_app(heimdall: Heimdall | None = None, login: Login | None = None, *, platform_factory=None,
               gateway_factory=None, prom_factory=None, objects_factory=None, app_objects_factory=None) -> FastAPI:
    """The *_factory arguments let tests put fakes behind the real routes; production leaves them out."""
    fqdn = os.environ.get("DATAOS_FQDN", "")
    default_tenant = os.environ.get("DATAOS_TENANT_ID", "")
    dev = os.environ.get("DAPP_DEV") == "1"
    if dev and not fqdn:
        ctx = load_context()
        fqdn, default_tenant = ctx.fqdn, default_tenant or ctx.tenant
    heimdall = heimdall or Heimdall(f"https://{fqdn}")
    if login is None:
        settings = Settings()
        if not settings.missing() and not settings.disabled:
            login = Login(settings, scopes=SCOPES + " offline_access")
    oidc = login
    prom_url = os.environ.get("DAPP_PROMETHEUS_URL") or (observability_url(f"https://{fqdn}") if fqdn else None)
    session_key = os.environ.get("DAPP_SESSION_KEY", "").encode() or getattr(getattr(login, "s", None), "session_key", b"")
    box = SessionBox(session_key or secrets.token_bytes(32))   # no key (local dev): sessions last until a restart
    sessions_created: dict[str, dict] = {}  # one-time display of newly created API keys

    api = FastAPI(title="dapp dashboard", version="0.1.0")

    # ------------------------------------------------------------------ identity
    def session_of(request: Request) -> Session | None:
        s = getattr(request.state, "session", _UNREAD)
        if s is _UNREAD:                                 # read (and maybe renew) once per request
            s = box.read(request.cookies)
            if s and s.token_expires and s.token_expires - 60 < time.time():
                renewed = s.refresh_token and oidc and oidc.refresh(s.refresh_token)
                if not renewed:
                    s = None  # token expired and can't be renewed: sign in again
                else:
                    s.token, s.refresh_token, s.token_expires = (
                        renewed["access_token"], renewed.get("refresh_token"), renewed["expires_at"])
                    request.state.session_renewed = True    # the middleware below sends the new cookie
            request.state.session = s
        if s:
            return s
        token = token_from_headers(request.headers)
        if token:
            ident = heimdall.identify(token)
            if ident:
                return Session(token, ident.user_id, request.query_params.get("tenant") or default_tenant,
                               "", time.time() + 60)
        if dev:
            ctx = load_context()
            token = ctx.apikey or ctx.access_token
            if token:
                return Session(token, ctx.user_id or "dev", default_tenant or ctx.tenant, "dev", time.time() + 60)
        return None

    def platform_for(s: Session, tenant: str | None = None) -> Platform:
        if platform_factory:
            return platform_factory(s, tenant)
        is_jwt = s.token.count(".") == 2
        ctx = Context("dashboard", fqdn, tenant or s.tenant, s.user_id,
                      None if is_jwt else s.token, s.token if is_jwt else None)
        client = DataOS(ctx)
        return Platform(client, writer=ApiWriter(client))

    agent_hooks: dict[str, Any] = {}      # set by the Agent Builder: which of your agents use which tool

    def gateway_for(s: Session) -> GatewayUsage:
        if gateway_factory:
            return gateway_factory(s)
        is_jwt = s.token.count(".") == 2
        headers = {"Authorization": f"Bearer {s.token}"} if is_jwt else {"apikey": s.token}
        return GatewayUsage(gateway_internal_url(), headers)

    def prom_for(s: Session) -> Prometheus:
        if prom_factory:
            return prom_factory(s)
        is_jwt = s.token.count(".") == 2
        return Prometheus(prom_url, headers={"Authorization": f"Bearer {s.token}"} if is_jwt else {"apikey": s.token})

    def need(request: Request) -> Session:
        s = session_of(request)
        if s is None:
            raise HTTPException(401, "sign in required")
        return s

    def check_csrf(s: Session, token: str) -> None:
        if s.csrf and not secrets.compare_digest(s.csrf, token or ""):
            raise HTTPException(403, "bad CSRF token; reload the page")

    def render(request: Request, template: str, s: Session | None, /, **ctx: Any) -> HTMLResponse:
        # positional-only: page data may contain any key, including `name` (the app page does)
        return TEMPLATES.TemplateResponse(request, template, {
            "s": s, "fqdn": fqdn, "flash": request.query_params.get("msg"),
            "error": request.query_params.get("err"), **ctx})

    def back(request: Request, path: str, msg: str | None = None, err: str | None = None) -> RedirectResponse:
        q = urlencode({k: v for k, v in (("msg", msg), ("err", err)) if v})
        root = request.scope.get("root_path", "")
        sep = "&" if "?" in path else "?"  # paths like /apps/x?tab=access already have a query string
        return RedirectResponse(f"{root}{path}{sep + q if q else ''}", status_code=303)

    @api.exception_handler(HTTPException)
    async def _http(request: Request, exc: HTTPException):
        if request.url.path.startswith(request.scope.get("root_path", "") + "/api/"):
            return JSONResponse({"error": exc.detail}, status_code=exc.status_code)
        if exc.status_code == 401:
            root = request.scope.get("root_path", "")
            if request.method == "GET" and request.url.path.rstrip("/") != root.rstrip("/"):
                from urllib.parse import (
                    quote,  # with OIDC this goes on to DataOS, then back to the page asked for
                )

                here = request.url.path + (f"?{request.url.query}" if request.url.query else "")
                return back(request, f"/login?next={quote(here, safe='')}")
            return back(request, "/login")
        resp = render(request, "error.html", session_of(request), status=exc.status_code, detail=exc.detail)
        resp.status_code = exc.status_code
        return resp

    # ------------------------------------------------------------------ auth pages
    def cookie_path(request: Request) -> str:
        return request.scope.get("root_path", "") or "/"

    def set_session_cookie(resp: Response, request: Request, s: Session) -> None:
        box.write(resp, s, httponly=True, secure=not dev, samesite="lax", path=cookie_path(request))

    @api.middleware("http")
    async def _renewed_session(request: Request, call_next):
        resp = await call_next(request)
        if getattr(request.state, "session_renewed", False) and request.state.session:
            set_session_cookie(resp, request, request.state.session)
        return resp

    def safe_next(request: Request, target: str | None) -> str:
        root = request.scope.get("root_path", "")
        if target and target.startswith("/") and not target.startswith("//"):
            return target
        return root + "/"

    @api.get("/login", response_class=HTMLResponse)
    @api.get("/_dapp/login", response_class=HTMLResponse)
    def login_page(request: Request, next: str | None = None):
        if oidc is None:  # no OIDC configured (local dev): paste an API key
            return render(request, "login.html", None, tenant=default_tenant)
        url, state = oidc.start(safe_next(request, next))
        log.info("sign-in start: host=%s fwd-host=%s referer=%s cookies=%s", request.headers.get("host"),
                 request.headers.get("x-forwarded-host"), (request.headers.get("referer") or "")[:80], sorted(request.cookies))
        resp = RedirectResponse(url, status_code=302)
        resp.set_cookie(Login.state_cookie(state.split("|", 1)[0]), state, max_age=LOGIN_TTL, httponly=True, secure=not dev, samesite="lax",
                        path=cookie_path(request))
        return resp

    @api.get("/_dapp/callback")
    def oidc_callback(request: Request, code: str = "", state: str = ""):
        expected, verifier, back_to = Login.find_state(request.cookies, state or "")
        failed = RedirectResponse(f"{request.scope.get('root_path', '')}/login-failed", status_code=302)
        if oidc is None or not expected or not secrets.compare_digest(expected, state or ""):
            log.warning("sign-in callback: the state cookie is missing or doesn't match (host=%s scheme=%s "
                        "fwd-host=%s cookies=%s)", request.headers.get("host"), request.url.scheme,
                        request.headers.get("x-forwarded-host"), sorted(request.cookies))   # names only
            return failed
        tokens = oidc.exchange(state, code, verifier)
        try:
            ident = tokens and heimdall.identify(tokens["access_token"])
        except IdentityUnavailable:
            ident = None
        if not ident:
            return failed
        s = Session(tokens["access_token"], ident.user_id, default_tenant, secrets.token_urlsafe(16),
                    time.time() + SESSION_TTL, tokens.get("refresh_token"), tokens["expires_at"])
        resp = RedirectResponse(safe_next(request, back_to), status_code=302)
        set_session_cookie(resp, request, s)
        resp.delete_cookie(Login.state_cookie(expected), path=cookie_path(request))
        resp.delete_cookie(STATE_COOKIE, path=cookie_path(request))
        return resp

    @api.post("/login")
    def login(request: Request, apikey: str = Form(...), tenant: str = Form("")):
        if oidc is not None:
            return back(request, "/login")
        try:
            ident = heimdall.identify(apikey.strip())
        except IdentityUnavailable as e:
            return back(request, "/login", err=f"DataOS sign-in unavailable: {e}")
        if ident is None:
            return back(request, "/login", err="That API key or token was not accepted by DataOS.")
        s = Session(apikey.strip(), ident.user_id, tenant.strip() or default_tenant,
                    secrets.token_urlsafe(16), time.time() + SESSION_TTL)
        resp = back(request, "/")
        set_session_cookie(resp, request, s)
        return resp

    @api.post("/logout")
    def logout(request: Request):
        resp = RedirectResponse(f"{request.scope.get('root_path', '')}/login?signed_out=1" if oidc is None
                                else f"{request.scope.get('root_path', '')}/signed-out", status_code=303)
        for name in SessionBox.names():
            resp.delete_cookie(name, path=cookie_path(request))
        return resp

    @api.get("/login-failed", response_class=HTMLResponse)
    def login_failed(request: Request):
        return render(request, "login_failed.html", None)

    @api.get("/signed-out", response_class=HTMLResponse)
    def signed_out(request: Request):
        return render(request, "signed_out.html", None)

    # ------------------------------------------------------------------ pages
    @api.get("/", response_class=HTMLResponse)
    def index(request: Request, all: bool = False):
        s = need(request)
        p = platform_for(s)
        try:
            apps = p.list_apps(everyone=all, with_access=True)
        except DataOSError as e:
            apps, err = [], str(e)
        else:
            err = None
        llm = _safe(lambda: gateway_for(s).summary(group_by="app"))
        by_app = {r["app"]: r for r in (llm or {}).get("rows", [])} if isinstance(llm, dict) else {}
        compute = {}
        if prom_url:
            prom = prom_for(s)
            p = platform_for(s)

            def now(a: dict) -> dict | None:  # only this app's own DataOS service: same-named apps of others share job_name
                service = p.service_name(a["name"])
                return prom.app_now(a["name"], a["tenant"], service) if service else None

            shown = [a for a in apps[:50] if a.get("state") == "running"]
            with ThreadPoolExecutor(max_workers=8) as pool:
                for a, res in zip(shown, pool.map(lambda a: _safe(lambda: now(a)), shown)):
                    compute[a["name"]] = res
        waiting = _safe(lambda: pending_counts([a["name"] for a in apps if a.get("owner") == s.user_id]))
        waiting = {k: v for k, v in waiting.items() if isinstance(v, int)} if isinstance(waiting, dict) else {}
        services = _safe(lambda: p.list_services(everyone=all))
        services = services if isinstance(services, list) else []
        return render(request, "index.html", s, apps=apps, err=err, all=all, llm=llm, by_app=by_app,
                      compute=compute, has_prom=bool(prom_url), waiting=waiting, services=services)

    @api.get("/apps/{name}", response_class=HTMLResponse)
    def app_page(request: Request, name: str, tab: str = "overview", days: int = 30):
        s = need(request)
        p = platform_for(s)
        try:
            st = p.status(name)
        except (PlatformError, DataOSError) as e:
            raise HTTPException(404, str(e))
        ctx: dict[str, Any] = {"app": st, "tab": tab, "name": name, "has_prom": bool(prom_url)}
        service = service_of(st.get("runtime_detail"))
        if tab == "overview" and prom_url and service:
            prom = prom_for(s)
            ctx["compute"] = _safe(lambda: prom.app_now(name, st["tenant"], service))
            end = time.time()
            series = _safe(lambda: prom.app_series(name, st["tenant"], end - 86400, end, "15m", service))
            if isinstance(series, dict) and "error" not in series:
                ctx["cpu_spark"] = sparkline(series.get("cpu_cores", []))
                ctx["mem_spark"] = sparkline(series.get("memory_bytes", []))
        if tab == "llm":
            keys_now = _safe(gateway_for(s).list_keys)
            ctx["app_llm_keys"] = [k for k in keys_now if k["app"] == name and k["tenant"] == st["tenant"]] \
                if isinstance(keys_now, list) else []
            ctx["uses_gateway"] = any(x.get("id", "").split(":")[-1] == f"dapp-{name}-llm"
                                      for x in ((st.get("spec") or {}).get("service") or {}).get("secrets") or [])
        days = days if days in (1, 7, 30, 90) else 30
        if tab == "llm":
            hours = days * 24
            ctx["days"] = days
            ctx["llm"] = _safe(lambda: gateway_for(s).summary(app=name, since_hours=hours, group_by="day,model"))
            by_user = _safe(lambda: gateway_for(s).summary(app=name, since_hours=hours, group_by="end_user"))
            if isinstance(by_user, dict) and isinstance(by_user.get("rows"), list):  # biggest users first
                by_user["rows"].sort(key=lambda r: r["input_tokens"] + r["output_tokens"], reverse=True)
            ctx["by_user"] = by_user
            # this month's token budgets: the owner sees every user and can change them; anyone else sees their own row
            ctx["budget"] = _safe(lambda: gateway_for(s).budgets(st["tenant"], name)) if ctx.get("uses_gateway") else None
        elif tab == "overview":
            ctx["llm"] = _safe(lambda: gateway_for(s).summary(app=name, group_by="model"))
            # an app that serves a model for the gateway (llm-small): which apps called it and how much
            served = _safe(lambda: gateway_for(s).models_served_by(name))
            ctx["served"] = [{"model": m, "usage": _safe(lambda m=m: gateway_for(s).summary(
                model=m, since_hours=days * 24, group_by="app"))} for m in served] if isinstance(served, list) else []
            ctx["days"] = days
        if tab == "secrets":
            ctx["secrets"] = _safe(lambda: p.list_secrets(name))
        if tab == "access":
            ctx["access"] = _safe(lambda: p.list_access(name))
            al = _safe(lambda: p.allowlist(name))
            if isinstance(al, dict) and "error" not in al:
                al["everyone"] = "*" in al["users"]
                al["extra_users"] = [u for u in al["users"] if u != "*" and u not in al["owners"]]
            ctx["allow"] = al
            if isinstance(al, dict) and s.user_id in (al.get("owners") or []):
                ctx["requests"] = _safe(lambda: pending_requests(name))
            people = _safe(p.list_users)  # for the picker; fine if the viewer may not list users
            ctx["people"] = [u for u in people if u["type"] == "person"] if isinstance(people, list) else []
        if tab == "logs":
            ctx["logs"] = _safe(lambda: p.logs_text(name, tail=300))
        return render(request, "app.html", s, **ctx)

    @api.post("/apps/{name}/scale")
    def scale(request: Request, name: str, replicas: int = Form(...), csrf: str = Form("")):
        s = need(request)
        check_csrf(s, csrf)
        return _act(request, f"/apps/{name}", lambda: platform_for(s).scale(name, replicas),
                    f"Scaled {name} to {replicas}")

    @api.post("/apps/{name}/delete")
    def delete(request: Request, name: str, confirm: str = Form(""), purge: bool = Form(False),
               csrf: str = Form("")):
        s = need(request)
        check_csrf(s, csrf)
        if confirm != name:
            return back(request, f"/apps/{name}", err="Type the app name to confirm deletion.")
        return _act(request, "/", lambda: platform_for(s).delete(name, purge=purge), f"Deleted {name}")

    @api.post("/apps/{name}/secrets")
    def secret_set(request: Request, name: str, secret: str = Form(...), pairs: str = Form(...),
                   csrf: str = Form("")):
        s = need(request)
        check_csrf(s, csrf)
        data = {}
        for line in pairs.splitlines():
            if "=" in line and not line.strip().startswith("#"):
                k, v = line.split("=", 1)
                data[k.strip()] = v.strip()
        return _act(request, f"/apps/{name}?tab=secrets", lambda: platform_for(s).set_secret(name, secret, data),
                    f"Saved secret {secret} ({len(data)} keys)")

    @api.post("/apps/{name}/secrets/{secret}/delete")
    def secret_rm(request: Request, name: str, secret: str, csrf: str = Form("")):
        s = need(request)
        check_csrf(s, csrf)
        return _act(request, f"/apps/{name}?tab=secrets", lambda: platform_for(s).delete_secret(name, secret),
                    f"Removed secret {secret}")

    def split_ids_field(text: str) -> list[str]:
        """"alice, bob carol" -> ["alice","bob","carol"], every one a plausible DataOS id."""
        out = [t for t in re.split(r"[,\s]+", text.strip()) if t]
        bad = [t for t in out if t == "*" or not ID_RE.match(t)]
        if bad:
            raise ValueError(f"not a valid id: {', '.join(bad[:3])}"
                             + (" (use the 'anyone signed in' checkbox for everyone)" if "*" in bad else ""))
        return out

    @api.post("/apps/{name}/allow")
    def allow(request: Request, name: str, users: str = Form(""), roles: str = Form(""), everyone: str = Form(""),
              csrf: str = Form("")):
        s = need(request)
        check_csrf(s, csrf)
        try:
            u, r = split_ids_field(users), split_ids_field(roles)
        except ValueError as e:
            return back(request, f"/apps/{name}?tab=access", err=str(e))
        if everyone:
            u.append("*")
        if not (u or r):
            return back(request, f"/apps/{name}?tab=access", err="Type at least one user or role.")
        dest = f"/apps/{name}?tab=access"
        try:
            res = platform_for(s).allow(name, u, r)
        except (DataOSError, PlatformError, ValueError) as e:
            return back(request, dest, err=str(e))
        shown = [res["resolved"].get(x, x) for x in u] + [f"role {x}" for x in r]
        what = ", ".join(shown).replace("*", "anyone signed in to DataOS")
        fixed = "".join(f" ({typed} is {uid})" for typed, uid in res["resolved"].items())
        return back(request, dest, msg=f"Allowed {what}{fixed}. The app restarts (about 40s) to apply it.")

    @api.post("/apps/{name}/deny")
    def deny(request: Request, name: str, kind: str = Form(...), value: str = Form(...), csrf: str = Form("")):
        s = need(request)
        check_csrf(s, csrf)
        if kind not in ("user", "role") or not (value == "*" or ID_RE.match(value)):
            return back(request, f"/apps/{name}?tab=access", err="Unknown entry.")
        p = platform_for(s)
        fn = (lambda: p.deny(name, [value], [])) if kind == "user" else (lambda: p.deny(name, [], [value]))
        return _act(request, f"/apps/{name}?tab=access", fn,
                    f"Removed {value if value != '*' else 'anyone signed in'}. The app restarts (about 40s) to apply it.")

    @api.post("/apps/{name}/access")
    def access_grant(request: Request, name: str, user: str = Form(...), use_case: str = Form(...),
                     values: str = Form(""), csrf: str = Form("")):
        s = need(request)
        check_csrf(s, csrf)
        vals: dict[str, list[dict[str, str]]] = {}
        for line in values.splitlines():
            atom, _, kv = line.strip().partition(":")
            k, _, v = kv.partition("=")
            if atom and k:
                vals.setdefault(atom, []).append({k: v})
        return _act(request, f"/apps/{name}?tab=access",
                    lambda: platform_for(s).grant_access(name, user, use_case=use_case, values=vals),
                    f"Granted {use_case} to {user}")

    @api.post("/apps/{name}/access/{grant}/delete")
    def access_revoke(request: Request, name: str, grant: str, csrf: str = Form("")):
        s = need(request)
        check_csrf(s, csrf)
        return _act(request, f"/apps/{name}?tab=access", lambda: platform_for(s).revoke_access(grant),
                    f"Revoked {grant}")

    @api.get("/keys", response_class=HTMLResponse)
    def keys(request: Request, created: str | None = None):
        s = need(request)
        p = platform_for(s)
        llm_keys = _safe(gateway_for(s).list_keys)
        groups: dict[tuple[str, str], list[dict]] = {}
        if isinstance(llm_keys, list):
            for k in llm_keys:
                groups.setdefault((k["app"], k["tenant"]), []).append(k)
        groups_list = [{"app": app, "tenant": tenant, "entries": sorted(ks, key=lambda k: k["created"], reverse=True),
                        "active": sum(1 for k in ks if not k["revoked"])}
                       for (app, tenant), ks in sorted(groups.items())]
        return render(request, "keys.html", s, keys=_safe(p.list_keys),
                      llm_error=llm_keys.get("error") if isinstance(llm_keys, dict) else None,
                      llm_groups=groups_list, created=sessions_created.pop(created or "", None))

    @api.post("/keys")
    def key_create(request: Request, key_name: str = Form(...), duration: str = Form("720h"),
                   csrf: str = Form("")):
        s = need(request)
        check_csrf(s, csrf)
        try:
            k = platform_for(s).create_key(key_name, duration, "created from dapp dashboard")
        except DataOSError as e:
            return back(request, "/keys", err=str(e))
        ref = secrets.token_urlsafe(8)  # show the key once, without putting it in a URL
        sessions_created[ref] = k
        return back(request, f"/keys?created={ref}")

    @api.post("/keys/{key_name}/delete")
    def key_delete(request: Request, key_name: str, csrf: str = Form("")):
        s = need(request)
        check_csrf(s, csrf)
        return _act(request, "/keys", lambda: platform_for(s).delete_key(key_name), f"Deleted key {key_name}")

    @api.post("/llm-keys/{app}/rotate")
    def llm_key_rotate(request: Request, app: str, back_to: str = Form("/keys"), csrf: str = Form("")):
        s = need(request)
        check_csrf(s, csrf)
        dest = back_to if back_to.startswith("/") and not back_to.startswith("//") else "/keys"
        p = platform_for(s)
        try:
            started = p.start_llm_rotation(app)
        except (DataOSError, PlatformError, ValueError) as e:
            return back(request, dest, err=str(e))

        def finish() -> None:  # the old key is revoked only after DataOS has restarted the app on the new one
            try:
                done = p.finish_llm_rotation(app, started["old_prefixes"], started["before_pods"])
                log.info("rotation of %s finished: %s", app, done)
            except Exception:
                log.exception("finishing the key rotation of %s failed", app)

        threading.Thread(target=finish, name=f"rotate-{app}", daemon=True).start()
        return back(request, dest, msg=f"New key {started['new_key_prefix']}… issued and stored for {app}. DataOS is "
                                       "restarting the app (about 40s); the old key is revoked once it is up.")

    @api.post("/llm-keys/{app}/{prefix}/revoke")
    def llm_key_revoke(request: Request, app: str, prefix: str, back_to: str = Form("/keys"), csrf: str = Form("")):
        s = need(request)
        check_csrf(s, csrf)
        dest = back_to if back_to.startswith("/") and not back_to.startswith("//") else "/keys"
        return _act(request, dest, lambda: platform_for(s).revoke_llm_key(app, prefix), f"Revoked key {prefix}…")

    # ------------------------------------------------------------------ token budgets (the app's owner only)
    def _budget_act(request: Request, app: str, fn) -> RedirectResponse:
        dest = f"/apps/{app}?tab=llm"
        try:
            out = fn(platform_for(need(request)).status(app)["tenant"])
        except (DataOSError, PlatformError, GatewayError, ValueError) as e:
            return back(request, dest, err=str(e))
        note = (out or {}).get("note") if isinstance(out, dict) else None
        return back(request, dest, msg="Budgets saved." + (f" {note}" if note else ""))

    @api.post("/budgets/{app}")
    def budget_set(request: Request, app: str, app_tokens: str = Form(""), per_user: str = Form(""), csrf: str = Form("")):
        """The form shows the current values: a cleared field removes that budget."""
        s = need(request)
        check_csrf(s, csrf)

        def save(tenant: str):
            clear = [k for k, v in (("app", app_tokens), ("default_user", per_user)) if not v.strip()]
            return gateway_for(s).set_budgets(
                tenant, app, app_tokens=parse_tokens(app_tokens) if app_tokens.strip() else None,
                default_user_tokens=parse_tokens(per_user) if per_user.strip() else None, clear=clear)
        return _budget_act(request, app, save)

    @api.post("/budgets/{app}/users")
    def budget_user_set(request: Request, app: str, user: str = Form(...), tokens: str = Form(...), csrf: str = Form("")):
        s = need(request)
        check_csrf(s, csrf)
        def save(tenant: str):
            ids, _ = platform_for(s).resolve_users([user])          # a typo raises with the closest ids
            return gateway_for(s).set_user_budget(tenant, app, ids[0] if ids else user, parse_tokens(tokens))
        return _budget_act(request, app, save)

    @api.post("/budgets/{app}/users/{user}/delete")
    def budget_user_delete(request: Request, app: str, user: str, csrf: str = Form("")):
        s = need(request)
        check_csrf(s, csrf)
        return _budget_act(request, app, lambda tenant: gateway_for(s).delete_user_budget(tenant, app, user))

    # ------------------------------------------------------------------ MCP tools
    @api.get("/tools", response_class=HTMLResponse)
    def tools(request: Request, days: int = 30):
        s = need(request)
        g = gateway_for(s)
        listed = _safe(lambda: g.list_tools(s.tenant))
        mine = [t["name"] for t in listed if t.get("mine")] if isinstance(listed, list) else []
        usage = by_fn = None
        if mine:
            usage = _safe(lambda: g.tool_usage(tool=None, tenant=s.tenant, since_hours=days * 24,
                                               group_by="tool,caller"))
            by_fn = _safe(lambda: g.tool_usage(tool=None, tenant=s.tenant, since_hours=days * 24,
                                               group_by="tool,tool_name"))
        back_to = request.query_params.get("return", "")
        names = {t["name"] for t in listed} if isinstance(listed, list) else set()
        used_by = {}
        if mine and agent_hooks.get("tool_users"):
            found = _safe(lambda: agent_hooks["tool_users"](s))
            used_by = found if isinstance(found, dict) and "error" not in found else {}
        return render(request, "tools.html", s, tools=listed, usage=usage, by_fn=by_fn, days=days,
                      gateway_url=gateway_public_url(Context("d", fqdn, s.tenant)),
                      presets=[p for p in tool_presets(fqdn) if p["name"] not in names],
                      connect=request.query_params.get("connect", ""), used_by=used_by,
                      adding=bool(request.query_params.get("add")) or (isinstance(listed, list) and not listed),
                      back_to=back_to if _local_path(back_to) else "", added=request.query_params.get("added", ""))

    def check_tool(s, name: str) -> str:
        """Ask the tool, through the gateway and as the signed-in person, which functions it has."""
        url = f"{gateway_internal_url()}/mcp/{s.tenant}/{name}"
        try:
            with httpx.Client(timeout=httpx.Timeout(20, connect=5)) as http:
                listed = Server(name, url, s.token, http, retry_seconds=0).call("tools/list", {}, s.user_id)
        except (ToolError, httpx.HTTPError, ValueError) as e:
            return f"{name}: saved, but it did not answer through the gateway: {str(e)[:220]}"
        fns = [f.get("name", "?") for f in listed.get("tools", [])]
        shown = ", ".join(fns[:8]) + (f" and {len(fns) - 8} more" if len(fns) > 8 else "")
        return f"{name}: connected · {len(fns)} tool{'' if len(fns) == 1 else 's'}" + (f": {shown}" if fns else "")

    def _tool_saved(request: Request, name: str, note: str, back_to: str) -> RedirectResponse:
        q = {"added": name, **({"return": back_to} if _local_path(back_to) else {})}
        good = ": connected" in note
        return back(request, "/tools?" + urlencode(q), **({"msg": note} if good else {"err": note}))

    def oauth_callback(request: Request) -> str:
        base = os.environ.get("DAPP_PUBLIC_URL", "").rstrip("/") or f"https://{fqdn}{request.scope.get('root_path', '')}"
        return f"{base}/tools/oauth/callback"

    def begin_sign_in(request: Request, s, name: str, back_to: str) -> RedirectResponse:
        try:
            url = gateway_for(s).oauth_start(s.tenant, name, back_to)["authorize_url"]
        except GatewayError as e:
            return back(request, "/tools", err=f"{name}: {e}")
        return RedirectResponse(url, status_code=303)

    @api.post("/tools/{name}/connect")
    def tool_connect(request: Request, name: str, back_to: str = Form("", alias="return"), csrf: str = Form("")):
        s = need(request)
        check_csrf(s, csrf)
        return begin_sign_in(request, s, name, back_to if _local_path(back_to) else "/tools")

    @api.get("/tools/oauth/callback")
    def tool_callback(request: Request, code: str = "", state: str = "", error: str = "", error_description: str = ""):
        s = need(request)
        if error or not code or not state:
            return back(request, "/tools", err="The sign-in was not completed" + (f": {error_description or error}"
                                                                                   if error else ""))
        try:
            done = gateway_for(s).oauth_finish(state, code)
        except GatewayError as e:
            return back(request, "/tools", err=str(e))
        target = done.get("back") or "/tools"
        target = target if _local_path(target) else "/tools"
        sep = "&" if "?" in target else "?"
        return RedirectResponse(f"{request.scope.get('root_path', '')}{target}{sep}"
                                + urlencode({"msg": f"{done['tool']}: connected as you", "connected": done["tool"]}),
                                status_code=303)

    @api.post("/tools/{name}/acts-as")
    def tool_acts_as(request: Request, name: str, mode: str = Form("user"), csrf: str = Form("")):
        s = need(request)
        check_csrf(s, csrf)
        say = {"owner": f"{name}: everyone allowed to use it now uses your account.",
               "user": f"{name}: each person signs in with their own account again."}
        return _act(request, "/tools", lambda: gateway_for(s).tool_act_as(s.tenant, name, mode), say.get(mode, name))

    @api.post("/tools/{name}/disconnect")
    def tool_disconnect(request: Request, name: str, csrf: str = Form("")):
        s = need(request)
        check_csrf(s, csrf)
        return _act(request, "/tools", lambda: gateway_for(s).oauth_disconnect(s.tenant, name),
                    f"{name}: disconnected. Connect again any time.")

    @api.post("/tools/add")
    def tool_add(request: Request, name: str = Form(""), url: str = Form(""), description: str = Form(""),
                 auth_header: str = Form("Authorization"), auth_value: str = Form(""), everyone: str = Form(""),
                 back_to: str = Form("", alias="return"), csrf: str = Form("")):
        s = need(request)
        check_csrf(s, csrf)
        name, url = name.strip().lower(), url.strip()
        if not name or not url:
            return back(request, "/tools?add=1", err="A tool needs a name and the MCP server's URL")
        try:
            platform_for(s).save_tool(name, url=url, description=description.strip(), auth_header=auth_header.strip(),
                                      auth_value=auth_value.strip() or None, allow=["*"] if everyone else None)
        except (DataOSError, PlatformError, GatewayError, ValueError) as e:
            return back(request, "/tools?add=1", err=str(e))
        if not auth_value.strip():
            # No key given: does the server ask each person to sign in (OAuth, like Notion)? If so, set that up and
            # send the owner straight to sign in; the others connect the same way, with their own account.
            try:
                found = gateway_for(s).oauth_setup(s.tenant, name, oauth_callback(request))
            except GatewayError as e:
                return back(request, "/tools?" + urlencode({"added": name}), err=f"{name}: saved, but sign-in could not be "
                                                                                 f"set up: {e}")
            if found.get("needs_key"):
                hint = (" For Data Product MCP use your DataOS API key (Keys page)." if name == "dataproduct-mcp" else "")
                return back(request, "/tools?" + urlencode({"added": name}),
                            err=f"{name}: saved, but the server asks for a key or token and does not offer sign-in. "
                                f"Open Edit on it and paste one.{hint}")
            if found.get("needs_sign_in"):
                return begin_sign_in(request, s, name, "/tools?" + urlencode({"added": name, **(
                    {"return": back_to} if _local_path(back_to) else {})}))
        return _tool_saved(request, name, check_tool(s, name), back_to)

    @api.post("/tools/{name}/edit")
    def tool_edit(request: Request, name: str, url: str = Form(""), description: str = Form(""),
                  auth_header: str = Form(""), auth_value: str = Form(""), remove_credential: str = Form(""),
                  csrf: str = Form("")):
        s = need(request)
        check_csrf(s, csrf)
        value = "" if remove_credential else (auth_value.strip() or None)     # empty field: keep the stored one
        try:
            platform_for(s).save_tool(name, url=url.strip(), description=description.strip(),
                                      auth_header=auth_header.strip(), auth_value=value)
        except (DataOSError, PlatformError, GatewayError, ValueError) as e:
            return back(request, "/tools", err=str(e))
        return _tool_saved(request, name, check_tool(s, name), "")

    @api.post("/tools/{name}/check")
    def tool_check(request: Request, name: str, csrf: str = Form("")):
        s = need(request)
        check_csrf(s, csrf)
        note = check_tool(s, name).replace(": saved, but it", ": it")
        return back(request, "/tools", **({"msg": note} if ": connected" in note else {"err": note}))

    @api.post("/tools/{name}/allow")
    def tool_allow(request: Request, name: str, users: str = Form(""), apps: str = Form(""),
                   everyone: str = Form(""), csrf: str = Form("")):
        s = need(request)
        check_csrf(s, csrf)
        try:
            who = split_ids_field(users) + [f"app:{a}" for a in split_ids_field(apps)] + (["*"] if everyone else [])
        except ValueError as e:
            return back(request, "/tools", err=str(e))
        if not who:
            return back(request, "/tools", err="Type a user, an app, or tick everyone")
        return _act(request, "/tools", lambda: platform_for(s).tool_allow(name, who),
                    f"{name}: allowed {', '.join(who)}")

    @api.post("/tools/{name}/deny")
    def tool_deny(request: Request, name: str, value: str = Form(...), csrf: str = Form("")):
        s = need(request)
        check_csrf(s, csrf)
        return _act(request, "/tools", lambda: platform_for(s).tool_deny(name, [value]), f"{name}: removed {value}")

    @api.post("/tools/{name}/delete")
    def tool_delete(request: Request, name: str, confirm: str = Form(""), csrf: str = Form("")):
        s = need(request)
        check_csrf(s, csrf)
        if confirm != name:
            return back(request, "/tools", err=f"Type {name} to confirm")
        return _act(request, "/tools", lambda: platform_for(s).delete_tool(name), f"Removed tool {name}")

    # ------------------------------------------------------------------ JSON API (CLI / agents)
    @api.get("/api/apps")
    def api_apps(request: Request, all: bool = False):
        return platform_for(need(request)).list_apps(everyone=all, with_access=True)

    @api.get("/api/apps/{name}")
    def api_app(request: Request, name: str):
        try:
            return platform_for(need(request)).status(name)
        except PlatformError as e:
            raise HTTPException(404, str(e))

    @api.get("/api/apps/{name}/usage")
    def api_usage(request: Request, name: str, since_hours: int = 720):
        s = need(request)
        st = platform_for(s).status(name)  # also proves the caller can see the app
        out: dict[str, Any] = {"llm": _safe(lambda: gateway_for(s).summary(app=name, since_hours=since_hours))}
        if prom_url:
            out["compute"] = _safe(lambda: prom_for(s).app_now(name, st["tenant"]))
        return out

    @api.get("/healthz")
    def healthz():
        return {"ok": True}

    # ------------------------------------------------------------------ immortal-obj storage
    def objects_for(s: Session) -> ObjectsClient:
        """immortal-obj through the gateway, as the signed-in person: their own and shared objects, nobody else's."""
        if objects_factory:
            return objects_factory(s)
        return ObjectsClient(f"{gateway_internal_url()}/mcp/{s.tenant}/immortal-obj", s.token, timeout=20)

    def storage_back(request: Request, ref: str, msg: str | None = None, err: str | None = None) -> RedirectResponse:
        from urllib.parse import quote

        return back(request, f"/storage/object?ref={quote(ref, safe='')}", msg=msg, err=err)

    def storage_act(request: Request, ref: str, fn, ok: str) -> RedirectResponse:
        try:
            fn()
        except (ObjectsError, OSError, ValueError) as e:
            return storage_back(request, ref, err=str(e))
        return storage_back(request, ref, msg=ok)

    def quoted(name: str) -> str:
        return '"' + name.replace('"', '""') + '"'

    def inspect_object(c: ObjectsClient, ref: str, kind: str | None, table: str | None, file: str | None) -> dict:
        """What the page shows for one object: its tables and rows (db) or its files (volume)."""
        out: dict[str, Any] = {"kind": kind}
        if kind in (None, "db"):
            try:
                db = c.open(ref)
                names = [t["name"] for t in db.sql(
                    "select name from sqlite_master where type = 'table' and name not like 'sqlite_%' order by name")]
                if table not in names:  # default: the key-value table, unless it is empty and the object has its own tables
                    others = [n for n in names if n != "_kv"]
                    table = "_kv" if "_kv" in names else (others[0] if others else None)
                    if table == "_kv" and others and not db.sql("select 1 as x from _kv limit 1"):
                        table = others[0]
                out.update(kind="db", tables=names, table=table)
                if table:
                    out["rows"] = db.sql(f"select * from {quoted(table)} limit 100")
                    out["count"] = db.sql(f"select count(*) as n from {quoted(table)}")[0]["n"]
                return out
            except ObjectsError:
                if kind == "db":
                    raise
        vol = c.volume(ref)
        out.update(kind="volume", files=vol.ls())
        if file:
            data = vol.read(file)
            out["file"] = {"path": file, "size": len(data), "text": None}
            try:
                out["file"]["text"] = data[:20000].decode()
            except UnicodeDecodeError:
                pass
        return out

    def storage_detail(request: Request, s: Session, ref: str, *, table: str | None = None, file: str | None = None,
                       sql_query: str = "", sql_rows: list | None = None, sql_error: str | None = None):
        c = objects_for(s)
        try:
            listing = c.list()
        except (ObjectsError, OSError) as e:
            return back(request, "/storage", err=str(e))
        own = {o["name"]: o for o in listing.get("own", [])}
        shared = {o["object"]: o for o in listing.get("shared_with_you", [])}
        info = own.get(ref)
        mode = "owner" if info else (shared[ref]["mode"] if ref in shared else None)
        try:
            view = inspect_object(c, ref, info["kind"] if info else None, table, file)
        except (ObjectsError, OSError) as e:
            return back(request, "/storage", err=f"{ref}: {e}")
        return render(request, "storage_object.html", s, ref=ref, info=info, mode=mode, view=view,
                      can_write=mode in ("owner", "write"), sql_query=sql_query, sql_rows=sql_rows, sql_error=sql_error)

    @api.get("/storage", response_class=HTMLResponse)
    def storage(request: Request):
        s = need(request)
        try:
            listing, error = objects_for(s).list(), None
        except (ObjectsError, OSError) as e:
            listing, error = {"own": [], "shared_with_you": [], "you": ""}, str(e)
        return render(request, "storage.html", s, listing=listing, storage_error=error)

    @api.get("/storage/object", response_class=HTMLResponse)
    def storage_object(request: Request, ref: str, table: str = "", file: str = ""):
        s = need(request)
        return storage_detail(request, s, ref, table=table or None, file=file or None)

    @api.post("/storage/put")
    def storage_put(request: Request, ref: str = Form(...), key: str = Form(...), value: str = Form(""),
                    csrf: str = Form("")):
        s = need(request)
        check_csrf(s, csrf)
        try:
            parsed = json.loads(value)
        except ValueError:
            parsed = value  # plain text is stored as a string
        return storage_act(request, ref, lambda: objects_for(s).open(ref.strip()).put(key.strip(), parsed),
                           f"Saved {key.strip()}")

    @api.post("/storage/write")
    def storage_write(request: Request, ref: str = Form(...), path: str = Form(...), content: str = Form(""),
                      csrf: str = Form("")):
        s = need(request)
        check_csrf(s, csrf)
        return storage_act(request, ref, lambda: objects_for(s).volume(ref.strip()).write(path.strip(), content),
                           f"Saved file {path.strip()}")

    @api.post("/storage/sql", response_class=HTMLResponse)
    def storage_sql(request: Request, ref: str = Form(...), query: str = Form(...), csrf: str = Form("")):
        s = need(request)
        check_csrf(s, csrf)
        try:
            rows = objects_for(s).open(ref).sql(query)
        except (ObjectsError, OSError) as e:
            return storage_detail(request, s, ref, sql_query=query, sql_error=str(e))
        return storage_detail(request, s, ref, sql_query=query, sql_rows=rows)

    @api.post("/storage/share")
    def storage_share(request: Request, ref: str = Form(...), who: str = Form(...), mode: str = Form("read"),
                      csrf: str = Form("")):
        s = need(request)
        check_csrf(s, csrf)
        return storage_act(request, ref, lambda: objects_for(s).open(ref).share(who.strip(), mode),
                           f"Shared with {who.strip()} ({mode})")

    @api.post("/storage/unshare")
    def storage_unshare(request: Request, ref: str = Form(...), who: str = Form(...), csrf: str = Form("")):
        s = need(request)
        check_csrf(s, csrf)
        return storage_act(request, ref, lambda: objects_for(s).call("unshare", {"object": ref, "who": who}),
                           f"Stopped sharing with {who}")

    @api.post("/storage/drop")
    def storage_drop(request: Request, ref: str = Form(...), confirm: str = Form(""), csrf: str = Form("")):
        s = need(request)
        check_csrf(s, csrf)
        if confirm != ref:
            return storage_back(request, ref, err=f"Type {ref} to confirm")
        try:
            objects_for(s).open(ref).drop()
        except (ObjectsError, OSError) as e:
            return storage_back(request, ref, err=str(e))
        return back(request, "/storage", msg=f"Deleted {ref}")

    # ------------------------------------------------------------------ access requests
    # Someone opens a private app they can't use, clicks "Request access" (it links here), and the app's owners see
    # it on the app's Access tab, where Approve does what `dapp access allow` does. Requests live in the dashboard's
    # own immortal-obj object; people only reach them through the checks below.
    requests_ready = {"ok": False}

    def requests_db():
        """The `access-requests` object (None when this dashboard has no storage, i.e. no `objects: true`)."""
        if app_objects_factory:
            client = app_objects_factory()
        else:
            client = ObjectsClient()  # DAPP_OBJECTS_URL and DAPP_LLM_KEY, which `objects: true` sets
            if not (client.url and client.key):
                return None
        db = client.open("access-requests")
        if not requests_ready["ok"]:
            db.sql("create table if not exists requests (id text primary key, app text, who text, note text, "
                   "status text default 'pending', created text default current_timestamp, decided_by text, "
                   "decided_at text)")
            requests_ready["ok"] = True
        return db

    def pending_requests(name: str) -> list[dict]:
        db = requests_db()
        return db.sql("select id, who, note, created from requests where app = ? and status = 'pending' "
                      "order by created, rowid", [name]) if db else []

    def pending_counts(names: list[str]) -> dict[str, int]:
        db = requests_db()
        if not db or not names:
            return {}
        rows = db.sql("select app, count(*) as n from requests where status = 'pending' and app in "
                      f"({','.join('?' * len(names))}) group by app", names)
        return {r["app"]: r["n"] for r in rows}

    @api.get("/request-access", response_class=HTMLResponse)
    def request_access_page(request: Request, app: str = ""):
        s = need(request)
        if not ID_RE.match(app):
            raise HTTPException(404, "unknown app")
        last, problem = None, None
        try:
            db = requests_db()
            if db is None:
                problem = "Access requests aren't switched on for this dashboard. Ask the app's owner directly."
            else:
                rows = db.sql("select status, note, created from requests where app = ? and who = ? "
                              "order by created desc, rowid desc limit 1", [app, s.user_id])
                last = rows[0] if rows else None
        except (ObjectsError, OSError) as e:
            problem = f"Requests are unavailable right now: {e}"
        return render(request, "request_access.html", s, target=app, last=last, problem=problem)

    @api.post("/request-access")
    def request_access(request: Request, app: str = Form(...), note: str = Form(""), csrf: str = Form("")):
        s = need(request)
        check_csrf(s, csrf)
        if not ID_RE.match(app):
            raise HTTPException(404, "unknown app")
        dest, note = f"/request-access?app={app}", " ".join(note.split())[:300]
        try:
            db = requests_db()
            if db is None:
                return back(request, dest, err="Access requests aren't switched on for this dashboard.")
            if db.sql("select id from requests where app = ? and who = ? and status = 'pending'", [app, s.user_id]):
                db.sql("update requests set note = ? where app = ? and who = ? and status = 'pending'",
                       [note, app, s.user_id])
            else:
                if db.sql("select count(*) as n from requests where who = ? and status = 'pending'",
                          [s.user_id])[0]["n"] >= 20:
                    return back(request, dest, err="You have too many open requests. Wait for answers first.")
                db.sql("insert into requests (id, app, who, note) values (?, ?, ?, ?)",
                       [uuid.uuid4().hex[:12], app, s.user_id, note])
        except (ObjectsError, OSError) as e:
            return back(request, dest, err=f"Could not send the request: {e}")
        return back(request, dest, msg="Request sent. The owner sees it in the dashboard.")

    def open_request(s: Session, name: str, rid: str):
        """(platform, db, who) for a still-open request on an app the signed-in person owns."""
        p = platform_for(s)
        if s.user_id not in (p.allowlist(name).get("owners") or []):
            raise HTTPException(403, "only an owner of this app can decide access requests")
        db = requests_db()
        rows = db.sql("select who, status from requests where id = ? and app = ?", [rid, name]) if db else []
        if not rows or rows[0]["status"] != "pending":
            raise ValueError("that request is no longer open")
        return p, db, rows[0]["who"]

    def decide(db, rid: str, status: str, by: str) -> None:
        db.sql("update requests set status = ?, decided_by = ?, decided_at = current_timestamp where id = ?",
               [status, by, rid])

    @api.post("/apps/{name}/requests/{rid}/approve")
    def request_approve(request: Request, name: str, rid: str, csrf: str = Form("")):
        s = need(request)
        check_csrf(s, csrf)
        dest = f"/apps/{name}?tab=access"
        try:
            p, db, who = open_request(s, name, rid)
            p.allow(name, users=[who])
            decide(db, rid, "approved", s.user_id)
        except (DataOSError, PlatformError, GatewayError, ValueError, ObjectsError, OSError) as e:
            return back(request, dest, err=str(e))
        return back(request, dest, msg=f"Allowed {who}. The app restarts to apply it (about 40 seconds).")

    @api.post("/apps/{name}/requests/{rid}/deny")
    def request_deny(request: Request, name: str, rid: str, csrf: str = Form("")):
        s = need(request)
        check_csrf(s, csrf)
        dest = f"/apps/{name}?tab=access"
        try:
            _, db, who = open_request(s, name, rid)
            decide(db, rid, "denied", s.user_id)
        except (DataOSError, PlatformError, ValueError, ObjectsError, OSError) as e:
            return back(request, dest, err=str(e))
        return back(request, dest, msg=f"Declined the request from {who}.")

    # ------------------------------------------------------------------ models
    # Small language models deployed on DataOS CPUs (llama.cpp), private, reached through the gateway by name.
    models_compute = os.environ.get("DAPP_MODELS_COMPUTE", "engineering-compute")
    models_dataplane = os.environ.get("DAPP_MODELS_DATAPLANE", "poliksdp01")

    def deploy_form(entry, hf: str = "", quants: dict[str, float] | None = None) -> dict[str, Any]:
        """The values the Deploy form starts with: the sizes that fit this model, and llama.cpp's own defaults."""
        base = defaults_for(entry) if entry else defaults(1.0)
        sampling = {k: "" for k in ("temperature", "top_k", "top_p", "min_p", "repeat_penalty")}
        return {**sampling, **base, "name": entry.id if entry else "", "model_id": entry.id if entry else "custom", "hf": hf,
                "quant": entry.quant if entry else "", "compute": models_compute, "batch": 2048, "ubatch": 512,
                "flash_attn": "auto", "cache_type_k": "f16", "cache_type_v": "f16", "max_tokens": -1, "reasoning": "auto",
                "jinja": "on", "context_shift": "off", "cont_batching": "on", "timeout_s": 3600}

    def parse_deploy(form) -> ModelDeploy:
        """Form text to a ModelDeploy: empty optional boxes stay unset, and a mistake becomes one readable sentence."""
        def num(key, cast=int):
            raw = str(form.get(key, "")).strip()
            if raw == "":
                return None
            try:
                return cast(raw)
            except ValueError:
                raise ValueError(f"{key.replace('_', ' ')}: {raw!r} is not a number")
        model_id = str(form.get("model_id", "")).strip()
        fields: dict[str, Any] = {"name": str(form.get("name", "")), "model_id": "" if model_id == "custom" else model_id,
                                  "hf": str(form.get("hf", "")), "quant": str(form.get("quant", "")).strip(),
                                  "compute": str(form.get("compute", "")).strip()}
        for key in ("cpu", "cpu_limit", "memory", "memory_limit", "flash_attn", "cache_type_k", "cache_type_v", "reasoning",
                    "jinja", "context_shift", "cont_batching"):
            if str(form.get(key, "")).strip():
                fields[key] = str(form[key]).strip()
        for key in ("disk_gi", "ctx", "parallel", "threads", "batch", "ubatch", "max_tokens", "timeout_s", "top_k"):
            v = num(key)
            if v is not None:
                fields[key] = v
        for key in ("temperature", "top_p", "min_p", "repeat_penalty"):
            v = num(key, float)
            if v is not None:
                fields[key] = v
        try:
            d = ModelDeploy(**fields)
        except ValidationError as e:
            raise ValueError("; ".join(f"{'.'.join(str(x) for x in err['loc'])}: {err['msg']}" for err in e.errors())) from e
        return with_hf_facts(d).check()

    def card(entry) -> dict[str, Any]:
        """One catalog entry as the picker shows it: what it is good at, how fast, what it needs and what it costs."""
        d = defaults_for(entry)
        stars, word = speed_of(entry.size_gb)
        est = estimate(cpu=d["cpu"], cpu_limit=d["cpu_limit"], memory=d["memory"], memory_limit=d["memory_limit"],
                       disk_gi=d["disk_gi"], size_gb=entry.size_gb)
        return {"e": entry, "quality": quality_of(entry), "traits": traits_of(entry), "tier": tier_of(entry), "speed": stars,
                "speed_word": word, "cpu": d["cpu_limit"], "memory": d["memory_limit"], "ctx": d["ctx"], "est": est}

    def disk_of(p: Platform, app: str) -> float | None:
        """The model's Volume size in Gi, for its cost (None when it can't be read)."""
        vol = _safe(lambda: p.client.get_resource("volume", f"{app}-data"[:60].rstrip("-")))
        size = ((vol or {}).get("spec") or {}).get("size", "") if isinstance(vol, dict) else ""
        try:
            return float(str(size).removesuffix("Gi")) if str(size).endswith("Gi") else None
        except ValueError:
            return None

    def model_rows(s: Session, p: Platform) -> list[dict[str, Any]]:
        """The models of this tenant with where each is: ready, starting (downloading or loading) or failed."""
        listed = _safe(p.models)
        if not isinstance(listed, list):
            return []
        rows = []
        for m in listed:
            st = _safe(lambda m=m: p.status(m["app"]))
            live = isinstance(st, dict) and st.get("state") is not None      # a failed lookup is {"error": ...}
            state = st["state"] if live else "unknown"
            phase = "ready" if m.get("ready") else ("failed" if state == "failed" else "starting")
            rows.append({**m, "state": state, "runtime": st.get("runtime") if live else None, "phase": phase,
                         "mine": m.get("owner") == s.user_id})
        return rows

    def chatbot_link(p: Platform, name: str) -> str | None:
        """A link that opens the DataOS chatbot with this model selected, when the chatbot is deployed."""
        app_name = os.environ.get("DAPP_CHATBOT_APP", "chatbot")
        st = _safe(lambda: p.status(app_name))
        if not (isinstance(st, dict) and st.get("state")):
            return None
        path = os.environ.get("DAPP_CHATBOT_PATH", f"/{app_name}")
        return f"https://{fqdn}{path}/?model={name}"

    @api.get("/models", response_class=HTMLResponse)
    def models_page(request: Request):
        s = need(request)
        p = platform_for(s)
        listed = _safe(p.models)
        err = listed.get("error") if isinstance(listed, dict) else None
        rows = model_rows(s, p) if err is None else []
        for r in rows:                         # what a running model costs, from its real resources
            res = ((_safe(lambda r=r: p.status(r["app"])) or {}).get("spec") or {}).get("resources") or {}
            req, lim = res.get("requests") or {}, res.get("limits") or {}
            if req.get("cpu") and req.get("memory") and r.get("source") != "config":
                r["est"] = estimate(cpu=req["cpu"], cpu_limit=lim.get("cpu", req["cpu"]), memory=req["memory"],
                                    memory_limit=lim.get("memory", req["memory"]), disk_gi=disk_of(p, r["app"]) or 0, size_gb=None)
        deployed = {r.get("repo", "").upper(): r["name"] for r in rows}
        cards = [card(c) for c in CATALOG]
        by_id = {c["e"].id: c for c in cards}
        picks = [("Just trying it, or simple tasks", "qwen2.5-0.5b", "Fast and cheap. This is what `small` is."),
                 ("Everyday chat and summaries", "qwen2.5-1.5b", "A clear step up for instructions and JSON, still light."),
                 ("The best answers, and you can wait", "qwen3-4b", "Reasons well; about 5 words per second on CPU.")]
        return render(request, "models.html", s, rows=rows, cards=cards, deployed=deployed, models_error=err, rates=rates(),
                      picks=[(t, by_id[i], why) for t, i, why in picks],
                      tiers=[("light", "Fast and light", "Under 1 GB. Quick answers for simple jobs; cheapest to keep running."),
                             ("balanced", "Balanced", "1 to 2 GB. Better instructions and structure for a modest cost."),
                             ("capable", "Most capable", "2 to 3 GB. Best answers; slower, so give them 4 CPUs.")])

    def estimate_data(entry, quants: dict[str, float] | None) -> dict[str, Any]:
        """What the Deploy form's cost panel needs in the browser: rates, file sizes per quantization, the speed rule."""
        return {"rates": rates(), "sizes": quants or {}, "file_gb": entry.size_gb if entry else None,
                "tokens_per_gb_s": TOKENS_PER_GB_S, "default_quant": entry.quant.upper() if entry else ""}

    def estimate_for(form: dict[str, Any], entry) -> dict[str, Any] | None:
        try:
            return estimate(cpu=str(form["cpu"]), cpu_limit=str(form["cpu_limit"]), memory=str(form["memory"]),
                            memory_limit=str(form["memory_limit"]), disk_gi=float(form["disk_gi"]),
                            size_gb=entry.size_gb if entry else None)
        except (ValueError, KeyError):
            return None

    @api.get("/models/deploy", response_class=HTMLResponse)
    def model_deploy_page(request: Request, id: str = "custom"):
        s = need(request)
        entry = catalog_entry(id)
        if id != "custom" and entry is None:
            raise HTTPException(404, "unknown model")
        quants = hf_quants(entry.repo) if entry else None
        form = deploy_form(entry)
        return render(request, "model_deploy.html", s, entry=entry, form=form, quants=quants, open_custom=False,
                      why=explain_defaults(entry) if entry else [], est=estimate_for(form, entry),
                      est_data=estimate_data(entry, quants), card=card(entry) if entry else None)

    @api.post("/models/deploy", response_class=HTMLResponse)
    async def model_deploy(request: Request):
        s = need(request)
        form = await request.form()
        check_csrf(s, str(form.get("csrf", "")))
        entry = catalog_entry(str(form.get("model_id", "")))
        try:
            d = parse_deploy(form)
            done = platform_for(s).deploy_model(d, compute=d.compute or models_compute, dataplane=models_dataplane)
        except (ValueError, PlatformError, DataOSError, GatewayError) as e:
            values = {**deploy_form(entry, hf=str(form.get("hf", ""))), **{k: v for k, v in form.items() if k != "csrf"}}
            quants = hf_quants(entry.repo) if entry else None
            resp = render(request, "model_deploy.html", s, entry=entry, form=values, error_text=str(e), open_custom=True,
                          quants=quants, why=explain_defaults(entry) if entry else [], est=estimate_for(values, entry),
                          est_data=estimate_data(entry, quants), card=card(entry) if entry else None)
            resp.status_code = 400
            return resp
        return back(request, f"/models/{done['name']}",
                    msg=f"Deploying {done['name']}: the model downloads on the first start (a minute or two), then it answers.")

    @api.get("/models/{name}", response_class=HTMLResponse)
    def model_page(request: Request, name: str, created: str | None = None, days: int = 30):
        s = need(request)
        p = platform_for(s)
        m = next((r for r in model_rows(s, p) if r["name"] == name), None)
        if m is None:
            raise HTTPException(404, f"no model {name}")
        days = days if days in (1, 7, 30, 90) else 30
        st = _safe(lambda: p.status(m["app"]))
        st = st if isinstance(st, dict) and st.get("state") else None
        spec = (st or {}).get("spec") or {}
        res = spec.get("resources") or {}
        req, lim = res.get("requests") or {}, res.get("limits") or {}
        est = None
        if req.get("cpu") and req.get("memory") and m.get("source") != "config":
            est = estimate(cpu=req["cpu"], cpu_limit=lim.get("cpu", req["cpu"]), memory=req["memory"],
                           memory_limit=lim.get("memory", req["memory"]), disk_gi=disk_of(p, m["app"]) or 0, size_gb=None)
        usage = _safe(lambda: gateway_for(s).summary(model=name, since_hours=days * 24, group_by="app"))
        return render(request, "model_detail.html", s, m=m, st=st, svc=(spec.get("service") or {}), res=res, req=req, lim=lim,
                      est=est, usage=usage, days=days, chatbot=chatbot_link(p, name), rates=rates(),
                      base_url=f"{gateway_public_url(Context('d', fqdn, s.tenant))}/openai/v1",
                      created=sessions_created.pop(created or "", None))

    @api.post("/models/{name}/key")
    def model_key(request: Request, name: str, csrf: str = Form("")):
        s = need(request)
        check_csrf(s, csrf)
        try:
            k = platform_for(s).personal_llm_key()
        except Exception as e:  # shown to the person
            return back(request, f"/models/{name}", err=f"Could not create a key: {e}")
        ref = secrets.token_urlsafe(8)  # show the key once, without putting it in a URL
        sessions_created[ref] = k
        return back(request, f"/models/{name}?created={ref}")

    @api.post("/models/{name}/delete")
    def model_delete(request: Request, name: str, confirm: str = Form(""), csrf: str = Form("")):
        s = need(request)
        check_csrf(s, csrf)
        if confirm != name:
            return back(request, f"/models/{name}", err=f"Type {name} to confirm")
        p = platform_for(s)
        try:
            gone = p.delete_model(name)
        except (DataOSError, PlatformError, GatewayError) as e:
            return back(request, f"/models/{name}", err=str(e))
        threading.Thread(target=p.delete_model_volume, args=(gone["volume"],), daemon=True,
                         name=f"volume-{gone['volume']}").start()   # DataOS frees it once the pod has stopped
        return back(request, "/models", msg=f"Deleted {name}. Its Volume (the downloaded model) is removed once the pod stops.")

    # ------------------------------------------------------------------ pipelines (DataOS Nilus)
    # Move data in plain words: where from, where to, how it changes. Everything is applied as the signed-in person; a Depot
    # they may not use is reported with a Request access button, and only its owner (or a tenant admin) can approve that.
    pipelines_compute = os.environ.get("DAPP_PIPELINES_COMPUTE") or models_compute
    pipelines_dest = os.environ.get("DAPP_PIPELINES_DEST", "")       # the Lakehouse Depot the form suggests first

    def depot_choices(p: Platform) -> list[dict[str, Any]]:
        listed = _safe(p.depots)
        return listed if isinstance(listed, list) else []

    def pipeline_form(values: dict[str, Any] | None = None) -> dict[str, Any]:
        return {"name": "", "how": "once", "source_depot": "", "source_object": "", "dest_depot": pipelines_dest,
                "dest_table": "", "schedule": "daily", "cron": "", "incremental_key": "", "primary_key": "", "mask": "",
                "exclude_columns": "", "row_limit": "", "metadata_mode": "shallow", "compute": pipelines_compute,
                "cpu": "200m", "memory": "512Mi", "cpu_limit": "1", "memory_limit": "2Gi", "description": "",
                **(values or {})}

    def parse_pipeline(form) -> PipelineRequest:
        f = {k: str(v).strip() for k, v in form.items()}
        mask: dict[str, str] = {}
        for part in re.split(r"[,\n]", f.get("mask", "")):
            if part.strip():
                col, _, how = part.partition("=")
                mask[col.strip()] = how.strip() or "hash"
        limit = f.get("row_limit", "")
        if limit and not limit.isdigit():
            raise ValueError("row limit must be a whole number")
        schedule = f.get("cron") if f.get("schedule") == "custom" else f.get("schedule", "daily")
        try:
            return PipelineRequest(
                name=f.get("name", ""), how=f.get("how", "once"), source_depot=f.get("source_depot", ""),
                source_object=f.get("source_object", ""), dest_depot=f.get("dest_depot", ""), dest_table=f.get("dest_table", ""),
                schedule=schedule or "daily", incremental_key=f.get("incremental_key", ""), primary_key=f.get("primary_key", ""),
                mask=mask, exclude_columns=[c.strip() for c in f.get("exclude_columns", "").split(",") if c.strip()],
                row_limit=int(limit) if limit else None, metadata_mode=f.get("metadata_mode") or "shallow",
                compute=f.get("compute") or pipelines_compute, cpu=f.get("cpu") or "200m", memory=f.get("memory") or "512Mi",
                cpu_limit=f.get("cpu_limit") or "1", memory_limit=f.get("memory_limit") or "2Gi",
                description=f.get("description", ""))
        except ValidationError as e:
            raise ValueError("; ".join(f"{'.'.join(str(x) for x in err['loc'])}: {err['msg']}" for err in e.errors())) from e

    def depot_requests(p: Platform, s: Session) -> list[dict[str, Any]]:
        """Open requests to use a Depot that this person may decide (they own the Depot, or are a tenant admin)."""
        db = _safe(requests_db)
        if db is None or isinstance(db, dict):
            return []
        rows = _safe(lambda: db.sql("select id, app, who, note, created from requests where app like 'depot:%' "
                                    "and status = 'pending' order by created, rowid"))
        if not isinstance(rows, list):
            return []
        out, can = [], {}
        for r in rows:
            depot = r["app"].removeprefix("depot:")
            if depot not in can:
                d = _safe(lambda depot=depot: p.depot(depot))
                can[depot] = isinstance(d, dict) and "name" in d and bool(_safe(lambda d=d: p.can_grant(d)) is True)
            if can[depot]:
                out.append({**r, "depot": depot})
        return out

    def my_depot_requests(s: Session) -> list[dict[str, Any]]:
        db = _safe(requests_db)
        if db is None or isinstance(db, dict):
            return []
        rows = _safe(lambda: db.sql("select app, status, created, decided_by from requests where app like 'depot:%' "
                                    "and who = ? order by created desc limit 10", [s.user_id]))
        return [{**r, "depot": r["app"].removeprefix("depot:")} for r in rows] if isinstance(rows, list) else []

    @api.get("/pipelines", response_class=HTMLResponse)
    def pipelines_page(request: Request):
        s = need(request)
        p = platform_for(s)
        listed = _safe(p.pipelines)
        err = listed.get("error") if isinstance(listed, dict) else None
        rows = listed if isinstance(listed, list) else []
        with ThreadPoolExecutor(max_workers=8) as pool:
            phases = list(pool.map(lambda r: _safe(lambda: p.pipeline_phase(r["name"])), rows))
        for r, ph in zip(rows, phases):
            r["phase"] = ph if isinstance(ph, str) else "unknown"
            r["mine"] = r.get("owner") == s.user_id
        return render(request, "pipelines.html", s, rows=rows, pipelines_error=err, depots=depot_choices(p),
                      to_decide=depot_requests(p, s), mine_asked=my_depot_requests(s), hows=list(HOWS.values()))

    def new_page(request: Request, s: Session, p: Platform, form: dict[str, Any], *, plan=None, error_text=None,
                 missing=None, status: int = 200) -> HTMLResponse:
        depots = depot_choices(p)
        resp = render(request, "pipeline_new.html", s, form=form, depots=depots, hows=list(HOWS.values()),
                      schedules=SCHEDULE_WORDS, plan=plan, error_text=error_text, missing=missing or [],
                      depot_json=json.dumps({d["name"]: {"modes": d["modes"], "object_word": d["object_word"],
                                                         "label": d["label"], "destination": d["destination"]}
                                             for d in depots}),
                      plan_yaml=yaml.safe_dump(plan["manifest"], sort_keys=False) if plan else "")
        resp.status_code = status
        return resp

    @api.get("/pipelines/new", response_class=HTMLResponse)
    def pipeline_new_page(request: Request, source: str = "", how: str = ""):
        s = need(request)
        p = platform_for(s)
        values = {"source_depot": source} if source else {}
        if how in HOWS:
            values["how"] = how
        return new_page(request, s, p, pipeline_form(values))

    @api.post("/pipelines/new", response_class=HTMLResponse)
    async def pipeline_new(request: Request):
        s = need(request)
        raw = await request.form()
        check_csrf(s, str(raw.get("csrf", "")))
        form = pipeline_form({k: v for k, v in raw.items() if k not in ("csrf", "action")})
        p = platform_for(s)
        try:
            req = parse_pipeline(form)
            if raw.get("action") != "deploy":
                return new_page(request, s, p, form, plan=p.plan_pipeline(req))
            done = p.deploy_pipeline(req)
        except PipelineAccessError as e:
            return new_page(request, s, p, form, error_text=str(e), missing=e.missing, status=403)
        except (ValueError, PlatformError, DataOSError) as e:
            return new_page(request, s, p, form, error_text=str(e), status=400)
        return back(request, f"/pipelines/{done['name']}",
                    msg=f"Started: {done['describe']}. The first run shows here in about a minute.")

    @api.post("/pipelines/request-access")
    def pipeline_request_access(request: Request, depot: str = Form(...), note: str = Form(""), csrf: str = Form("")):
        s = need(request)
        check_csrf(s, csrf)
        if not ID_RE.match(depot):
            raise HTTPException(404, "unknown Depot")
        note = " ".join(note.split())[:300]
        try:
            db = requests_db()
            if db is None:
                return back(request, "/pipelines", err="Access requests aren't switched on for this dashboard.")
            key = f"depot:{depot}"
            if db.sql("select id from requests where app = ? and who = ? and status = 'pending'", [key, s.user_id]):
                db.sql("update requests set note = ? where app = ? and who = ? and status = 'pending'", [note, key, s.user_id])
            else:
                if db.sql("select count(*) as n from requests where who = ? and status = 'pending'", [s.user_id])[0]["n"] >= 20:
                    return back(request, "/pipelines", err="You have too many open requests. Wait for answers first.")
                db.sql("insert into requests (id, app, who, note) values (?, ?, ?, ?)", [uuid.uuid4().hex[:12], key, s.user_id, note])
        except (ObjectsError, OSError) as e:
            return back(request, "/pipelines", err=f"Could not send the request: {e}")
        return back(request, "/pipelines", msg=f"Asked for access to Depot {depot}. Its owner (or a tenant admin) decides "
                                                "on the Pipelines page; you can deploy once they approve.")

    def open_depot_request(s: Session, rid: str):
        p = platform_for(s)
        db = requests_db()
        rows = db.sql("select app, who, note, status from requests where id = ? and app like 'depot:%'", [rid]) if db else []
        if not rows or rows[0]["status"] != "pending":
            raise ValueError("that request is no longer open")
        depot = rows[0]["app"].removeprefix("depot:")
        if not p.can_grant(p.depot(depot)):
            raise HTTPException(403, f"only the owner of Depot {depot} or a tenant admin can decide this")
        return p, db, depot, rows[0]["who"], rows[0]["note"]

    @api.post("/pipelines/requests/{rid}/approve")
    def pipeline_request_approve(request: Request, rid: str, csrf: str = Form("")):
        s = need(request)
        check_csrf(s, csrf)
        try:
            p, db, depot, who, note = open_depot_request(s, rid)
            p.grant_depot_access(depot, who, reason=note)
            decide(db, rid, "approved", s.user_id)
        except (DataOSError, PlatformError, ValueError, ObjectsError, OSError) as e:
            return back(request, "/pipelines", err=str(e))
        return back(request, "/pipelines", msg=f"{who} may now use Depot {depot} and its Secrets (DataOS grants, audited).")

    @api.post("/pipelines/requests/{rid}/deny")
    def pipeline_request_deny(request: Request, rid: str, csrf: str = Form("")):
        s = need(request)
        check_csrf(s, csrf)
        try:
            _, db, depot, who, _ = open_depot_request(s, rid)
            decide(db, rid, "denied", s.user_id)
        except (DataOSError, PlatformError, ValueError, ObjectsError, OSError) as e:
            return back(request, "/pipelines", err=str(e))
        return back(request, "/pipelines", msg=f"Declined {who}'s request for Depot {depot}.")

    @api.get("/pipelines/{name}", response_class=HTMLResponse)
    def pipeline_page(request: Request, name: str):
        s = need(request)
        p = platform_for(s)
        res = _safe(lambda: p.pipeline(name))
        if not isinstance(res, dict) or "name" not in res:
            raise HTTPException(404, f"no pipeline {name}")
        row = pipeline_summary(res)
        runs = _safe(lambda: p.pipeline_runs(name))
        stats = _safe(lambda: p.pipeline_stats(name))
        logs = _safe(lambda: p.pipeline_logs(name, tail=120))
        manifest = {k: res[k] for k in ("name", "version", "type", "description", "spec") if k in res}
        left = _safe(lambda: p.pipeline_leftovers(name))
        return render(request, "pipeline_detail.html", s, r=row, phase=p.pipeline_phase(name),
                      left=left if isinstance(left, dict) and "error" not in left else None,
                      runs=runs if isinstance(runs, list) else [], runs_error=runs.get("error") if isinstance(runs, dict) else None,
                      stats=stats if isinstance(stats, dict) and "error" not in stats else None,
                      logs=logs if isinstance(logs, str) else "", mine=row["owner"] == s.user_id,
                      yaml_text=yaml.safe_dump(manifest, sort_keys=False))

    @api.post("/pipelines/{name}/run")
    def pipeline_run(request: Request, name: str, csrf: str = Form("")):
        s = need(request)
        check_csrf(s, csrf)
        try:
            platform_for(s).rerun_pipeline(name)
        except (DataOSError, PlatformError) as e:
            return back(request, f"/pipelines/{name}", err=str(e))
        return back(request, f"/pipelines/{name}", msg="Running again. The new run shows here in about a minute.")

    @api.post("/pipelines/{name}/delete")
    def pipeline_delete(request: Request, name: str, confirm: str = Form(""), drop_table: str = Form(""),
                        remove_source: str = Form(""), csrf: str = Form("")):
        s = need(request)
        check_csrf(s, csrf)
        if confirm != name:
            return back(request, f"/pipelines/{name}", err=f"Type {name} to confirm")
        try:
            out = platform_for(s).delete_pipeline(name, drop_table=bool(drop_table), remove_source=bool(remove_source))
        except (DataOSError, PlatformError) as e:
            return back(request, f"/pipelines/{name}", err=str(e))
        done = [f"Deleted {name}."]
        if out["table_dropped"]:
            done.append(out["table_dropped"].capitalize() + ".")
        if out["source_removed"]:
            done.append(f"Removed the source {out['source_removed']} and its credentials.")
        if not (out["table_dropped"] or out["source_removed"]) and not out["problems"]:
            done.append("Data it already wrote stays in the destination.")
        problems = " ".join(out["problems"])
        return back(request, "/pipelines", msg=" ".join(done) if not problems else None,
                    err=(" ".join(done) + " " + problems) if problems else None)

    # ------------------------------------------------------------------ sources (connect a new system: Secret + Depot)
    # The fields come from DataOS's own Depot type schemas. Credentials are typed into password boxes, go straight into a
    # DataOS Secret owned by the person, and are never stored here, logged, or sent back to the page (not even after an error).
    def type_fields(p: Platform) -> dict[str, list[dict[str, Any]]]:
        types = _safe(p.source_types)
        if not isinstance(types, dict) or "error" in types:
            return {}
        return {name: [{"key": f.key, "side": f.side, "required": f.required, "kind": f.kind, "default": f.default}
                       for f in t.fields] for name, t in sorted(types.items())}

    def source_page(request: Request, s: Session, p: Platform, form: dict[str, Any], *, error_text=None,
                    status: int = 200) -> HTMLResponse:
        fields = type_fields(p)
        safe_form = {k: v for k, v in form.items() if not k.startswith("secret.") and k != "url"}   # never echo credentials
        resp = render(request, "source_new.html", s, form=safe_form, presets=list(PRESETS.values()), fields=fields,
                      fields_json=json.dumps(fields), presets_json=json.dumps({k: {"type": v.type, "hint": v.hint,
                                                                                   "params": v.params}
                                                                               for k, v in PRESETS.items()}),
                      error_text=error_text, types_error=None if fields else "DataOS did not return its Depot types")
        resp.status_code = status
        return resp

    def parse_source(form, p: Platform) -> SourceRequest:
        f = {k: str(v) for k, v in form.items()}
        preset = f.get("preset", "").strip()
        kind = f.get("type", "").strip() or (PRESETS[preset].type if preset in PRESETS else "")
        spec: dict[str, Any] = {}
        creds: dict[str, SecretStr] = {}
        url = f.get("url", "").strip()
        if url:
            kind_u, spec, from_creds = from_url(url)
            kind = kind or kind_u
            creds = {k: SecretStr(v) for k, v in from_creds.items()}
        for k, v in f.items():
            v = v.strip()
            if k.startswith("spec.") and v:
                key = k[5:]
                if key == "params":
                    spec["params"] = {a.strip(): b.strip() for a, _, b in (x.partition("=") for x in re.split(r"[,\n]", v))
                                      if a.strip()}
                elif key in ("nodes", "brokers"):
                    spec[key] = [x.strip() for x in v.split(",") if x.strip()]
                else:
                    spec[key] = v
            elif k.startswith("secret.") and v:
                creds[k[7:]] = SecretStr(v)
        if not kind:
            raise ValueError("choose what you are connecting")
        try:
            return SourceRequest(name=f.get("name", ""), type=kind, spec=spec, secret=creds,
                                 description=f.get("description", ""), preset=preset if preset in PRESETS else "")
        except ValidationError as e:
            raise ValueError("; ".join(f"{err['loc'][-1]}: {err['msg']}" for err in e.errors())) from None

    @api.get("/sources", response_class=HTMLResponse)
    def sources_page(request: Request):
        s = need(request)
        p = platform_for(s)
        listed = _safe(p.sources)
        return render(request, "sources.html", s, rows=listed if isinstance(listed, list) else [],
                      sources_error=listed.get("error") if isinstance(listed, dict) else None,
                      presets=list(PRESETS.values()))

    @api.get("/sources/new", response_class=HTMLResponse)
    def source_new_page(request: Request, preset: str = "", type: str = ""):
        s = need(request)
        p = platform_for(s)
        kind = type or (PRESETS[preset].type if preset in PRESETS else "")
        return source_page(request, s, p, {"preset": preset if preset in PRESETS else "", "type": kind,
                                          "name": preset.replace("-", "") if preset in PRESETS else ""})

    @api.post("/sources/new", response_class=HTMLResponse)
    async def source_new(request: Request):
        s = need(request)
        raw = await request.form()
        check_csrf(s, str(raw.get("csrf", "")))
        form = {k: v for k, v in raw.items() if k != "csrf"}
        p = platform_for(s)
        try:
            req = parse_source(form, p)
            done = p.add_source(req)
        except (ValueError, PlatformError, DataOSError) as e:
            return source_page(request, s, p, form, error_text=redact(str(e)), status=400)
        return back(request, f"/pipelines/new?source={done['name']}",
                    msg=f"Connected {done['name']}. Its credentials are in Secret {done['secret']}, yours only. "
                        f"Now say what to bring in.")

    @api.post("/sources/{name}/creds")
    async def source_creds(request: Request, name: str):
        s = need(request)
        raw = await request.form()
        check_csrf(s, str(raw.get("csrf", "")))
        creds: dict[str, SecretStr] = {}
        try:
            url = str(raw.get("url", "")).strip()
            if url:
                creds = {k: SecretStr(v) for k, v in from_url(url)[2].items()}
            for k, v in raw.items():
                if k.startswith("secret.") and str(v).strip():
                    creds[k[7:]] = SecretStr(str(v).strip())
            platform_for(s).replace_source_credentials(name, creds)
        except (ValueError, PlatformError, DataOSError) as e:
            return back(request, "/sources", err=redact(str(e)))
        return back(request, "/sources", msg=f"Replaced the credentials of {name}. Pipelines use them from their next run.")

    @api.post("/sources/{name}/delete")
    def source_delete(request: Request, name: str, confirm: str = Form(""), keep_secret: str = Form(""),
                      csrf: str = Form("")):
        s = need(request)
        check_csrf(s, csrf)
        if confirm != name:
            return back(request, "/sources", err=f"Type {name} to confirm")
        try:
            out = platform_for(s).delete_source(name, keep_secret=bool(keep_secret))
        except (PlatformError, DataOSError) as e:
            return back(request, "/sources", err=str(e))
        extra = f" These pipelines used it: {', '.join(out['pipelines_affected'])}." if out["pipelines_affected"] else ""
        return back(request, "/sources", msg=f"Removed {name}" + (" and its credentials." if out["secret_deleted"] else ".") + extra)

    # ------------------------------------------------------------------ Claude Code (one per person)
    # Everyone deploys their own: its pod runs as them (DataOS gives it their identity), so what it deploys is theirs, nobody else can
    # read their files or Claude sign-in, and their code and chats are saved in their own immortal-obj objects.
    claude_compute = os.environ.get("DAPP_CLAUDE_COMPUTE") or models_compute

    def claude_form(values: dict[str, Any] | None = None) -> dict[str, Any]:
        return {**CLAUDE_DEFAULTS, "compute": claude_compute, **(values or {})}

    def claude_estimate(form: dict[str, Any]) -> dict[str, Any] | None:
        try:
            return estimate(cpu=str(form["cpu"]), cpu_limit=str(form["cpu_limit"]), memory=str(form["memory"]),
                            memory_limit=str(form["memory_limit"]), disk_gi=0, size_gb=None)
        except (ValueError, KeyError):
            return None

    def claude_page_data(s: Session, p: Platform, form: dict[str, Any]) -> dict[str, Any]:
        mine = _safe(p.claude_instance)
        if "app" not in mine:                                     # the lookup failed: {"error": ...}
            mine = {"app": instance_name(s.user_id), "exists": False, "state": None, "token": False, "error": mine.get("error"),
                    "url": f"https://{fqdn}/{instance_name(s.user_id)}/"}
        live = {}
        if mine.get("exists"):
            res = ((_safe(lambda: p.status(mine["app"])) or {}).get("spec") or {}).get("resources") or {}
            req, lim = res.get("requests") or {}, res.get("limits") or {}
            if req.get("cpu") and req.get("memory"):
                live = {"req": req, "lim": lim, "est": claude_estimate({"cpu": req["cpu"], "cpu_limit": lim.get("cpu", req["cpu"]),
                                                                        "memory": req["memory"],
                                                                        "memory_limit": lim.get("memory", req["memory"])})}
        everyone = _safe(p.claude_instances)
        return {"mine": mine, "live": live, "everyone": everyone if isinstance(everyone, list) else [],
                "everyone_error": everyone.get("error") if isinstance(everyone, dict) else None, "form": form,
                "est": claude_estimate(form), "rates": rates()}

    @api.get("/claude", response_class=HTMLResponse)
    def claude_page(request: Request):
        s = need(request)
        return render(request, "claude.html", s, **claude_page_data(s, platform_for(s), claude_form()))

    @api.post("/claude/deploy", response_class=HTMLResponse)
    async def claude_deploy(request: Request):
        s = need(request)
        form = await request.form()
        check_csrf(s, str(form.get("csrf", "")))
        p = platform_for(s)
        values = {k: str(form.get(k, "")).strip() for k in ("compute", "cpu", "cpu_limit", "memory", "memory_limit")}
        try:
            d = ClaudeDeploy(**{k: v for k, v in values.items() if v}).check()
            p.deploy_claude(d, compute=d.compute or claude_compute, token=str(form.get("token", "")))
        except (ValueError, PlatformError, DataOSError, GatewayError) as e:
            resp = render(request, "claude.html", s, error_text=str(e),
                          **claude_page_data(s, p, claude_form({k: v for k, v in values.items() if v})))
            resp.status_code = 400
            return resp
        return back(request, "/claude", msg="Deploying your Claude Code: it starts in a minute or two. Open it when it shows running.")

    @api.post("/claude/token")
    def claude_token(request: Request, token: str = Form(""), csrf: str = Form("")):
        s = need(request)
        check_csrf(s, csrf)
        try:
            done = platform_for(s).set_claude_token(token)
        except (ValueError, PlatformError, DataOSError, GatewayError) as e:
            return back(request, "/claude", err=str(e))
        return back(request, "/claude", msg="Token saved. Your Claude Code restarts to use it (about a minute); you stay signed in "
                                            "through restarts." if done["attached"] else
                    "Token saved. It is used when you deploy your Claude Code.")

    @api.post("/claude/stop")
    def claude_stop(request: Request, csrf: str = Form("")):
        s = need(request)
        check_csrf(s, csrf)
        return _act(request, "/claude", lambda: platform_for(s).stop_claude(),
                    "Stopped. The pod is gone and costs nothing; your files, chats and token are kept. Deploy again to continue.")

    @api.post("/claude/delete")
    def claude_delete(request: Request, confirm: str = Form(""), csrf: str = Form("")):
        s = need(request)
        check_csrf(s, csrf)
        app = instance_name(s.user_id)
        if confirm != app:
            return back(request, "/claude", err=f"Type {app} to confirm")
        return _act(request, "/claude", lambda: platform_for(s).delete_claude(),
                    "Deleted, with its token and sign-in. Your files and chats are still in your Storage (workspace, claude-sessions).")

    def _act(request: Request, path: str, fn, ok: str) -> RedirectResponse:
        try:
            fn()
        except (DataOSError, PlatformError, GatewayError, ValueError) as e:
            return back(request, path, err=str(e))
        return back(request, path, msg=ok)

    mount_agents(api, need=need, check_csrf=check_csrf, render=render, gateway_for=gateway_for,
                 platform_for=platform_for, app_objects_factory=app_objects_factory, hooks=agent_hooks)
    return api


def _safe(fn):
    try:
        return fn()
    except Exception as e:  # a missing metrics/gateway backend shouldn't break the page
        log.warning("dashboard backend call failed: %s", e)
        return {"error": str(e)}


def _local_path(path: str) -> bool:
    """A page of this dashboard to go back to (never another site)."""
    return bool(path) and path.startswith("/") and not path.startswith("//") and "\\" not in path


def tool_presets(fqdn: str) -> list[dict[str, str]]:
    """MCP servers the Add tool form can fill in. dapp-monitor and immortal-obj come with the platform."""
    return [{"name": "dataproduct-mcp", "title": "Data Product MCP", "url": f"https://{fqdn}/dataproduct-mcp/api/v1",
             "description": "DataOS data products: search, semantic schema, run queries",
             "auth_header": "Authorization",
             "hint": "Your DataOS API key (Keys page). Everyone you allow uses the tool with this key, so they see what "
                     "you can see."}]


def main() -> None:
    import uvicorn

    logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO").upper())
    import dapp_metrics

    dapp_metrics.start()
    uvicorn.run(dapp_metrics.instrument(create_app()), host=os.environ.get("HOST", "0.0.0.0"),
                port=int(os.environ.get("PORT", "8080")), root_path=os.environ.get("ROOT_PATH", ""),
                proxy_headers=True)


if __name__ == "__main__":
    main()
