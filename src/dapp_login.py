"""DataOS sign-in for apps deployed with dapp. Standard library only, so it can be copied into any image.

    from dapp_login import protect
    app = protect(app)          # any ASGI app (FastAPI, Starlette, ...)

dapp sets everything through env when `login: true` (the default for non-public apps):
    DAPP_OIDC_ISSUER        https://<fqdn>/oidc
    DAPP_OIDC_CLIENT_ID     OIDC client Poros registers in Heimdall for this app
    clientSecret            from the app's <app>-oidc DataOS secret (also DAPP_SESSION_KEY)
    DAPP_PUBLIC_URL         https://<fqdn>/<path>
    DAPP_HEIMDALL_URL       https://<fqdn>/heimdall
    DAPP_ALLOWED_USERS      comma list of DataOS user ids (owners are always in it); "*" = any DataOS user
    DAPP_ALLOWED_ROLES      comma list of Heimdall role ids, e.g. engineering-tenant-admin

Browser users go through the DataOS OIDC login; API callers may send `Authorization: Bearer <token|apikey>`
or an `apikey` header. The app sees the caller in the `x-dapp-user` header (incoming copies are dropped).
Routes under <path>/_dapp/: login, callback, logout, me.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import os
import secrets
import threading
import time
from http.cookies import SimpleCookie
from urllib.error import HTTPError
from urllib.parse import parse_qs, quote, urlencode
from urllib.request import Request, urlopen

log = logging.getLogger("dapp_login")

COOKIE = "dapp_auth"
STATE_COOKIE = "dapp_login_state"
SESSION_TTL = 8 * 3600
LOGIN_TTL = 600
# federated:id lets Heimdall match the DataOS user; without it the token is refused.
SCOPES = "openid profile email groups federated:id"
USER_AGENT = "dapp-login/0.1"


def _env_list(name: str) -> set[str]:
    return {v.strip() for v in os.environ.get(name, "").split(",") if v.strip()}


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def _http_json(req: Request) -> tuple[int, dict]:
    req.add_header("User-Agent", USER_AGENT)  # the DataOS edge rejects Python's default agent
    try:
        with urlopen(req, timeout=10) as r:
            return r.status, json.loads(r.read() or b"{}")
    except HTTPError as e:
        try:
            return e.code, json.loads(e.read() or b"{}")
        except ValueError:
            return e.code, {}


class Settings:
    def __init__(self) -> None:
        self.issuer = os.environ.get("DAPP_OIDC_ISSUER", "").rstrip("/")
        self.client_id = os.environ.get("DAPP_OIDC_CLIENT_ID", "")
        self.client_secret = os.environ.get("DAPP_OIDC_CLIENT_SECRET") or os.environ.get("clientSecret", "")
        self.public_url = os.environ.get("DAPP_PUBLIC_URL", "").rstrip("/")
        self.heimdall = os.environ.get("DAPP_HEIMDALL_URL", "").rstrip("/")
        self.session_key = (os.environ.get("DAPP_SESSION_KEY") or self.client_secret or secrets.token_hex(32)).encode()
        self.users = _env_list("DAPP_ALLOWED_USERS")
        self.roles = _env_list("DAPP_ALLOWED_ROLES")
        self.disabled = os.environ.get("DAPP_AUTH", "oidc") == "off"

    @property
    def base_path(self) -> str:
        if "://" in self.public_url:
            rest = self.public_url.split("://", 1)[1]
            return "/" + rest.split("/", 1)[1] if "/" in rest else ""
        return ""

    def missing(self) -> list[str]:
        need = {"DAPP_OIDC_ISSUER": self.issuer, "DAPP_OIDC_CLIENT_ID": self.client_id,
                "clientSecret": self.client_secret, "DAPP_PUBLIC_URL": self.public_url,
                "DAPP_HEIMDALL_URL": self.heimdall}
        return [k for k, v in need.items() if not v]


class Login:
    def __init__(self, settings: Settings | None = None, scopes: str = SCOPES):
        self.s = settings or Settings()
        self.scopes = scopes
        self._meta: dict | None = None
        self._pending: dict[str, tuple[str, float]] = {}
        self._ident_cache: dict[str, tuple[float, dict | None]] = {}
        self._lock = threading.Lock()

    # ---------------- identity
    def identify(self, token: str) -> dict | None:
        key = hashlib.sha256(token.encode()).hexdigest()
        now = time.time()
        with self._lock:
            hit = self._ident_cache.get(key)
            if hit and hit[0] > now:
                return hit[1]
        status, body = _http_json(Request(self.s.heimdall + "/api/v1/authorize", method="POST",
                                          data=json.dumps({"token": token}).encode(),
                                          headers={"Content-Type": "application/json"}))
        if status >= 500 or "valid" not in body and "allow" not in body:
            raise RuntimeError(f"Heimdall unavailable (HTTP {status})")
        data = body.get("result") or {}
        ident = None
        if body.get("allow") and body.get("valid") and data.get("id"):
            ident = {"id": data["id"], "tags": [t for t in data.get("tags") or [] if isinstance(t, str)]}
        with self._lock:
            self._ident_cache = {k: v for k, v in self._ident_cache.items() if v[0] > now}
            self._ident_cache[key] = (now + 60, ident)
        return ident

    def allowed(self, ident: dict) -> bool:
        if "*" in self.s.users or ident["id"] in self.s.users:
            return True
        return any(f"roles:id:{r}" in ident["tags"] for r in self.s.roles)

    # ---------------- session cookie (signed, stateless: survives restarts)
    def seal(self, ident: dict) -> str:
        payload = _b64(json.dumps({"u": ident["id"], "t": ident["tags"], "e": int(time.time()) + SESSION_TTL}).encode())
        sig = _b64(hmac.new(self.s.session_key, payload.encode(), hashlib.sha256).digest())
        return f"{payload}.{sig}"

    def unseal(self, value: str) -> dict | None:
        try:
            payload, sig = value.split(".", 1)
            good = _b64(hmac.new(self.s.session_key, payload.encode(), hashlib.sha256).digest())
            if not hmac.compare_digest(sig, good):
                return None
            data = json.loads(_unb64(payload))
            if data["e"] < time.time():
                return None
            return {"id": data["u"], "tags": data["t"]}
        except (ValueError, KeyError):
            return None

    # ---------------- OIDC
    def metadata(self) -> dict:
        if self._meta is None:
            status, data = _http_json(Request(self.s.issuer + "/.well-known/openid-configuration"))
            if status != 200 or "authorization_endpoint" not in data:
                raise RuntimeError(f"OIDC discovery failed (HTTP {status})")
            self._meta = data
        return self._meta

    @property
    def redirect_uri(self) -> str:
        return f"{self.s.public_url}/_dapp/callback"

    def start(self, return_to: str) -> tuple[str, str]:
        state = secrets.token_urlsafe(24)
        verifier = secrets.token_urlsafe(48)
        challenge = _b64(hashlib.sha256(verifier.encode()).digest())
        with self._lock:
            now = time.time()
            self._pending = {k: v for k, v in self._pending.items() if v[1] > now}
            self._pending[state] = (verifier, now + LOGIN_TTL)
        q = urlencode({"response_type": "code", "client_id": self.s.client_id, "redirect_uri": self.redirect_uri,
                       "scope": self.scopes, "state": state, "code_challenge": challenge,
                       "code_challenge_method": "S256"})
        return f"{self.metadata()['authorization_endpoint']}?{q}", f"{state}|{return_to}"

    def exchange(self, state: str, code: str) -> dict | None:
        """Swap the callback code for tokens: {access_token, refresh_token?, expires_at}."""
        with self._lock:
            entry = self._pending.pop(state, None)
        if not entry or entry[1] < time.time() or not code:
            return None
        return self._token_request({"grant_type": "authorization_code", "code": code,
                                    "redirect_uri": self.redirect_uri, "code_verifier": entry[0]})

    def refresh(self, refresh_token: str) -> dict | None:
        tokens = self._token_request({"grant_type": "refresh_token", "refresh_token": refresh_token})
        if tokens:
            tokens.setdefault("refresh_token", refresh_token)
        return tokens

    def finish(self, state: str, code: str) -> str | None:
        tokens = self.exchange(state, code)
        return tokens["access_token"] if tokens else None

    def _token_request(self, form: dict) -> dict | None:
        basic = base64.b64encode(f"{quote(self.s.client_id, safe='')}:{quote(self.s.client_secret, safe='')}".encode())
        status, data = _http_json(Request(
            self.metadata()["token_endpoint"], method="POST", data=urlencode(form).encode(),
            headers={"Content-Type": "application/x-www-form-urlencoded", "Authorization": f"Basic {basic.decode()}"}))
        if status != 200 or not data.get("access_token"):
            log.warning("token request %s failed: HTTP %s %s", form["grant_type"], status, data.get("error", ""))
            return None
        return {"access_token": data["access_token"], "refresh_token": data.get("refresh_token"),
                "expires_at": time.time() + int(data.get("expires_in") or 300)}


def _headers(scope) -> dict[str, str]:
    return {k.decode().lower(): v.decode() for k, v in scope.get("headers", [])}


def _cookies(headers: dict[str, str]) -> dict[str, str]:
    c = SimpleCookie()
    try:
        c.load(headers.get("cookie", ""))
    except Exception:
        return {}
    return {k: m.value for k, m in c.items()}


def _cookie(name: str, value: str, path: str, max_age: int) -> str:
    return f"{name}={value}; Path={path or '/'}; Max-Age={max_age}; HttpOnly; Secure; SameSite=Lax"


PAGE = """<!doctype html><meta charset=utf-8><meta name=viewport content="width=device-width,initial-scale=1">
<title>{title}</title><style>body{{font:15px system-ui;margin:0;display:grid;place-items:center;min-height:100vh;
background:#f7f7f5;color:#1c1c1a}}@media(prefers-color-scheme:dark){{body{{background:#141413;color:#ecebe6}}}}
main{{max-width:440px;padding:24px}}code{{font-size:13px}}</style><main><h2>{title}</h2><p>{body}</p></main>"""


def protect(app, *, settings: Settings | None = None, public_paths: tuple[str, ...] = ("/healthz",)):
    s = settings or Settings()
    if s.disabled:
        return app
    gaps = s.missing()
    if gaps:
        raise RuntimeError(f"dapp_login: missing env {', '.join(gaps)} (set login: false or deploy with dapp)")
    login = Login(s)
    base = s.base_path

    async def send_simple(send, status: int, body: str, headers: list[tuple[str, str]] = (), ctype="text/html"):
        raw = body.encode()
        hs = [(b"content-type", f"{ctype}; charset=utf-8".encode()), (b"content-length", str(len(raw)).encode()),
              (b"cache-control", b"no-store")] + [(k.encode(), v.encode()) for k, v in headers]
        await send({"type": "http.response.start", "status": status, "headers": hs})
        await send({"type": "http.response.body", "body": raw})

    def caller(headers: dict[str, str]) -> dict | None:
        ident = login.unseal(_cookies(headers).get(COOKIE, ""))
        if ident is None:
            auth = headers.get("authorization", "")
            token = auth[7:].strip() if auth.lower().startswith("bearer ") else headers.get("apikey", "").strip()
            if token:
                ident = login.identify(token)
        return ident

    async def websocket(scope, receive, send):
        """Websockets carry the same cookie: check it before the app sees the connection (a terminal is a shell)."""
        path = scope.get("path", "")
        rel = path[len(base):] if base and path.startswith(base) else path
        if rel not in public_paths:
            try:
                ident = caller(_headers(scope))
            except RuntimeError:
                ident = None
            if ident is None or not login.allowed(ident):
                await receive()  # websocket.connect
                return await send({"type": "websocket.close", "code": 4401 if ident is None else 4403})
            scope = dict(scope)
            scope["headers"] = [(k, v) for k, v in scope.get("headers", []) if k.lower() != b"x-dapp-user"]
            scope["headers"].append((b"x-dapp-user", ident["id"].encode()))
        return await app(scope, receive, send)

    async def middleware(scope, receive, send):
        if scope["type"] == "websocket":
            return await websocket(scope, receive, send)
        if scope["type"] != "http":
            return await app(scope, receive, send)
        path = scope.get("path", "")
        rel = path[len(base):] if base and path.startswith(base) else path
        if rel in public_paths:
            return await app(scope, receive, send)
        headers = _headers(scope)
        query = parse_qs(scope.get("query_string", b"").decode())

        if rel == "/_dapp/login":
            target = (query.get("next") or [base + "/"])[0]
            if not target.startswith("/") or target.startswith("//"):
                target = base + "/"
            url, state = login.start(target)
            return await send_simple(send, 302, "", [("location", url),
                                                     ("set-cookie", _cookie(STATE_COOKIE, state, base, LOGIN_TTL))])
        if rel == "/_dapp/callback":
            raw_state = _cookies(headers).get(STATE_COOKIE, "")
            state, _, back = raw_state.partition("|")
            if not state or (query.get("state") or [""])[0] != state:
                return await send_simple(send, 400, PAGE.format(title="Sign-in failed",
                                         body=f"The sign-in state didn't match. <a href='{base}/_dapp/login'>Try again</a>."))
            token = login.finish(state, (query.get("code") or [""])[0])
            ident = token and login.identify(token)
            if not ident:
                return await send_simple(send, 401, PAGE.format(title="Sign-in failed",
                                         body=f"DataOS didn't accept the sign-in. <a href='{base}/_dapp/login'>Try again</a>."))
            return await send_simple(send, 302, "", [
                ("location", back or base + "/"),
                ("set-cookie", _cookie(COOKIE, login.seal(ident), base, SESSION_TTL)),
                ("set-cookie", _cookie(STATE_COOKIE, "", base, 0))])
        if rel == "/_dapp/logout":
            return await send_simple(send, 302, "", [("location", base + "/"),
                                                     ("set-cookie", _cookie(COOKIE, "", base, 0))])

        try:
            ident = caller(headers)
        except RuntimeError as e:
            return await send_simple(send, 503, json.dumps({"error": str(e)}), ctype="application/json")
        if ident is None:
            if "text/html" in headers.get("accept", ""):
                nxt = quote(path + ("?" + scope["query_string"].decode() if scope.get("query_string") else ""))
                return await send_simple(send, 302, "", [("location", f"{base}/_dapp/login?next={nxt}")])
            return await send_simple(send, 401, json.dumps({"error": "DataOS sign-in required"}),
                                     ctype="application/json")
        if not login.allowed(ident):
            return await send_simple(send, 403, PAGE.format(
                title="No access",
                body=f"You're signed in as <b>{ident['id']}</b>, but this app hasn't been shared with you. "
                     f"Ask an owner to run <code>dapp access allow &lt;app&gt; {ident['id']}</code>. "
                     f"<a href='{base}/_dapp/logout'>Sign out</a>"))
        if rel == "/_dapp/me":
            return await send_simple(send, 200, json.dumps(ident), ctype="application/json")

        scope = dict(scope)
        scope["headers"] = [(k, v) for k, v in scope.get("headers", []) if k.lower() != b"x-dapp-user"]
        scope["headers"].append((b"x-dapp-user", ident["id"].encode()))
        return await app(scope, receive, send)

    return middleware
