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
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from dapp.auth import Heimdall, IdentityUnavailable, token_from_headers
from dapp.client import DataOS, DataOSError
from dapp.config import Context, load_context
from dapp.ctl import ApiWriter
from dapp.platform import ID_RE, Platform, PlatformError, gateway_internal_url, gateway_public_url
from dapp.usage import GatewayError, GatewayUsage, Prometheus, observability_url, service_of
from dapp_login import SCOPES, Login, Settings
from dapp_objects import Client as ObjectsClient
from dapp_objects import ObjectsError

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


TEMPLATES.env.filters["num"] = fmt_num
TEMPLATES.env.filters["when"] = fmt_when


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
    sessions: dict[str, Session] = {}
    sessions_created: dict[str, dict] = {}  # one-time display of newly created API keys

    api = FastAPI(title="dapp dashboard", version="0.1.0")

    # ------------------------------------------------------------------ identity
    def session_of(request: Request) -> Session | None:
        sid = request.cookies.get(SESSION_COOKIE)
        s = sessions.get(sid or "")
        if s and s.expires > time.time():
            if s.token_expires and s.token_expires - 60 < time.time():
                renewed = s.refresh_token and oidc and oidc.refresh(s.refresh_token)
                if not renewed:
                    sessions.pop(sid, None)  # token expired and can't be renewed: sign in again
                    s = None
                else:
                    s.token, s.refresh_token, s.token_expires = (
                        renewed["access_token"], renewed.get("refresh_token"), renewed["expires_at"])
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
        from urllib.parse import urlencode

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

    def set_session_cookie(resp: RedirectResponse, request: Request, sid: str) -> None:
        resp.set_cookie(SESSION_COOKIE, sid, max_age=SESSION_TTL, httponly=True, secure=not dev, samesite="lax",
                        path=cookie_path(request))

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
        resp = RedirectResponse(url, status_code=302)
        resp.set_cookie(STATE_COOKIE, state, max_age=LOGIN_TTL, httponly=True, secure=not dev, samesite="lax",
                        path=cookie_path(request))
        return resp

    @api.get("/_dapp/callback")
    def oidc_callback(request: Request, code: str = "", state: str = ""):
        raw = request.cookies.get(STATE_COOKIE, "")
        expected, _, back_to = raw.partition("|")
        failed = RedirectResponse(f"{request.scope.get('root_path', '')}/login-failed", status_code=302)
        if oidc is None or not expected or not secrets.compare_digest(expected, state or ""):
            return failed
        tokens = oidc.exchange(state, code)
        try:
            ident = tokens and heimdall.identify(tokens["access_token"])
        except IdentityUnavailable:
            ident = None
        if not ident:
            return failed
        sid = secrets.token_urlsafe(32)
        sessions[sid] = Session(tokens["access_token"], ident.user_id, default_tenant, secrets.token_urlsafe(16),
                                time.time() + SESSION_TTL, tokens.get("refresh_token"), tokens["expires_at"])
        resp = RedirectResponse(safe_next(request, back_to), status_code=302)
        set_session_cookie(resp, request, sid)
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
        sid = secrets.token_urlsafe(32)
        sessions[sid] = Session(apikey.strip(), ident.user_id, tenant.strip() or default_tenant,
                                secrets.token_urlsafe(16), time.time() + SESSION_TTL)
        resp = back(request, "/")
        set_session_cookie(resp, request, sid)
        return resp

    @api.post("/logout")
    def logout(request: Request):
        sessions.pop(request.cookies.get(SESSION_COOKIE, ""), None)
        resp = RedirectResponse(f"{request.scope.get('root_path', '')}/login?signed_out=1" if oidc is None
                                else f"{request.scope.get('root_path', '')}/signed-out", status_code=303)
        resp.delete_cookie(SESSION_COOKIE, path=cookie_path(request))
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
        return render(request, "index.html", s, apps=apps, err=err, all=all, llm=llm, by_app=by_app,
                      compute=compute, has_prom=bool(prom_url), waiting=waiting)

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
        return render(request, "tools.html", s, tools=listed, usage=usage, by_fn=by_fn, days=days,
                      gateway_url=gateway_public_url(Context("d", fqdn, s.tenant)))

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

    def _act(request: Request, path: str, fn, ok: str) -> RedirectResponse:
        try:
            fn()
        except (DataOSError, PlatformError, GatewayError, ValueError) as e:
            return back(request, path, err=str(e))
        return back(request, path, msg=ok)

    return api


def _safe(fn):
    try:
        return fn()
    except Exception as e:  # a missing metrics/gateway backend shouldn't break the page
        log.warning("dashboard backend call failed: %s", e)
        return {"error": str(e)}


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
