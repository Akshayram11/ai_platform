import re
import time

from fastapi.testclient import TestClient

from dapp.auth import Identity
from dapp_dashboard.app import create_app


class FakeHeimdall:
    def identify(self, token):
        return Identity("alice") if token in ("good", "oidc-access") else None


class FakeLogin:
    """Stands in for dapp_login.Login: no network, one valid code."""

    def __init__(self):
        self.refreshed = 0

    def start(self, return_to):
        return "https://x.dataos.cloud/oidc/auth?client_id=c", f"st1|{return_to}"

    def exchange(self, state, code):
        if state == "st1" and code == "good-code":
            return {"access_token": "oidc-access", "refresh_token": "r1", "expires_at": time.time() + 300}
        return None

    def refresh(self, refresh_token):
        self.refreshed += 1
        return {"access_token": "oidc-access", "refresh_token": refresh_token, "expires_at": time.time() + 300}


def client(monkeypatch, login=None):
    monkeypatch.setenv("DATAOS_FQDN", "x.dataos.cloud")
    monkeypatch.setenv("DATAOS_TENANT_ID", "ai")
    for v in ("DAPP_DEV", "DAPP_PROMETHEUS_URL", "DAPP_OIDC_ISSUER", "DAPP_OIDC_CLIENT_ID", "clientSecret"):
        monkeypatch.delenv(v, raising=False)
    return TestClient(create_app(heimdall=FakeHeimdall(), login=login), base_url="https://testserver")


def test_oidc_login_is_automatic(monkeypatch):
    c = client(monkeypatch, FakeLogin())
    r = c.get("/", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"].endswith("/login")
    assert c.get("/api/apps").status_code == 401

    # /login goes straight to DataOS (no form), remembering where to come back to
    r = c.get("/login", follow_redirects=False)
    assert r.status_code == 302 and r.headers["location"].startswith("https://x.dataos.cloud/oidc/auth")
    assert "dapp_login_state" in r.cookies

    # a wrong code lands on a stop page, never back in the login loop
    r = c.get("/_dapp/callback?code=bad&state=st1", follow_redirects=False)
    assert r.status_code == 302 and r.headers["location"].endswith("/login-failed")
    assert "Try again" in c.get("/login-failed").text

    r = c.get("/_dapp/callback?code=good-code&state=st1", follow_redirects=False)
    assert r.status_code == 302 and "dapp_session" in r.cookies

    # signed in: the page renders (Poros is unreachable in tests, so it reports an error but still loads)
    page = c.get("/")
    assert page.status_code == 200 and "Your apps" in page.text and "alice" in page.text

    # state-changing forms need the CSRF token
    r = c.post("/apps/chatbot/scale", data={"replicas": 2, "csrf": "nope"}, follow_redirects=False)
    assert r.status_code == 403
    assert re.search(r'name="csrf"', c.get("/keys").text)

    r = c.post("/logout", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"].endswith("/signed-out")
    assert c.get("/", follow_redirects=False).status_code == 303


def test_callback_rejects_state_mismatch(monkeypatch):
    c = client(monkeypatch, FakeLogin())
    c.get("/login", follow_redirects=False)  # state cookie = st1
    r = c.get("/_dapp/callback?code=good-code&state=other", follow_redirects=False)
    assert r.headers["location"].endswith("/login-failed")


def test_expiring_token_is_refreshed(monkeypatch):
    class ShortLived(FakeLogin):
        def exchange(self, state, code):
            t = super().exchange(state, code)
            return t and {**t, "expires_at": time.time() + 30}  # inside the 60s refresh margin

    fake = ShortLived()
    c = client(monkeypatch, fake)
    c.get("/login", follow_redirects=False)
    c.get("/_dapp/callback?code=good-code&state=st1", follow_redirects=False)
    assert c.get("/", follow_redirects=False).status_code == 200  # still signed in
    assert fake.refreshed == 1


def test_failed_refresh_signs_out(monkeypatch):
    class NoRefresh(FakeLogin):
        def exchange(self, state, code):
            t = super().exchange(state, code)
            return t and {**t, "expires_at": time.time() + 30}

        def refresh(self, refresh_token):
            return None

    c = client(monkeypatch, NoRefresh())
    c.get("/login", follow_redirects=False)
    c.get("/_dapp/callback?code=good-code&state=st1", follow_redirects=False)
    assert c.get("/", follow_redirects=False).status_code == 303


def test_api_key_fallback_without_oidc(monkeypatch):
    c = client(monkeypatch, None)
    assert "API key" in c.get("/login").text
    r = c.post("/login", data={"apikey": "bad", "tenant": "ai"}, follow_redirects=False)
    assert "err=" in r.headers["location"]
    r = c.post("/login", data={"apikey": "good", "tenant": "ai"}, follow_redirects=False)
    assert r.status_code == 303 and "dapp_session" in r.cookies
    assert c.get("/").status_code == 200


def signed_in(monkeypatch):
    c = client(monkeypatch, FakeLogin())
    c.get("/login", follow_redirects=False)
    c.get("/_dapp/callback?code=good-code&state=st1", follow_redirects=False)
    token = re.search(r'name="csrf" value="([^"]+)"', c.get("/keys").text).group(1)
    return c, token


def test_access_forms_validate_before_touching_dataos(monkeypatch):
    from urllib.parse import parse_qs, urlparse

    c, tok = signed_in(monkeypatch)

    def post(path, **data):
        r = c.post(path, data={"csrf": tok, **data}, follow_redirects=False)
        assert r.status_code == 303, r.status_code
        return parse_qs(urlparse(r.headers["location"]).query).get("err", [""])[0]

    assert "not a valid id" in post("/apps/bot/allow", users="alice bob/../evil")
    assert "checkbox" in post("/apps/bot/allow", users="*")                 # everyone only via the explicit checkbox
    assert "at least one" in post("/apps/bot/allow", users="  ", roles="")
    assert post("/apps/bot/deny", kind="admin", value="alice") == "Unknown entry."
    assert post("/apps/bot/deny", kind="user", value="a b") == "Unknown entry."
    r = c.post("/apps/bot/allow", data={"csrf": "wrong", "users": "alice"}, follow_redirects=False)
    assert r.status_code == 403                                             # no CSRF token, no change
    r = c.post("/apps/bot/deny", data={"csrf": "wrong", "kind": "user", "value": "alice"}, follow_redirects=False)
    assert r.status_code == 403


def test_redirects_keep_the_tab_and_the_message(monkeypatch):
    from urllib.parse import parse_qs, urlparse

    c, tok = signed_in(monkeypatch)
    r = c.post("/apps/bot/allow", data={"csrf": tok, "users": "a/b"}, follow_redirects=False)
    q = parse_qs(urlparse(r.headers["location"]).query)
    assert q["tab"] == ["access"] and "not a valid id" in q["err"][0]      # both survive; before, tab swallowed err


# ---- every page, through the real routes (a template-only test missed a 500 in the app page) ----
class FakePlatform:
    def __init__(self):
        self.calls = []

    def list_apps(self, everyone=False, with_access=False):
        mk = lambda n, acc: {"name": n, "tenant": "engineering", "owner": "alice", "state": "running", "status": "active",
                             "runtime": "app-service:running:1", "error": None, "managed_by_dapp": True,
                             "url": f"https://x/{n}/", "access": acc}
        return [mk("chatbot", {"mode": "login", "everyone": False, "people": 1, "roles": 0}),
                mk("dash", {"mode": "login", "everyone": True, "people": 1, "roles": 0}),
                mk("gw", {"mode": "public"})]

    def status(self, name):
        if name == "missing":
            from dapp.platform import PlatformError
            raise PlatformError("app 'missing' not found")
        return {"name": name, "tenant": "engineering", "owner": "alice", "state": "running", "status": "active",
                "runtime": "app-service:running:1", "error": None, "url": f"https://x/{name}/", "service": f"{name}svc",
                "spec": {"replicas": 1, "compute": "c", "runAsUser": "alice", "app": {"description": "d", "owners": ["alice"]},
                         "service": {"image": "i:1", "port": 8080, "secrets": [{"id": f"dapp-{name}-llm"}], "http": {}}},
                "runtime_detail": {"nodeDetails": [{"name": f"{name}-pod", "phase": "running", "containers": ["c"],
                                                   "dataplane": "dp1"}]}}

    def list_secrets(self, app):
        return [{"name": f"dapp-{app}-llm", "managed_by_dapp": True, "referenced": True, "owner": "alice"}]

    def list_access(self, app):
        return [{"name": "g1", "use_case": "uc", "subjects": ["users:id:bob"], "status": {}}]

    def allowlist(self, app):
        return {"app": app, "mode": "login", "owners": ["alice"], "users": ["alice", "carol"], "roles": ["data-dev"]}

    def list_users(self):
        return [{"id": "carol", "name": "Carol K", "email": "c@x", "type": "person"}]

    def logs_text(self, name, tail=100, node=None):
        return "==> pod / c <==\nhello from the app"

    def list_keys(self):
        return [{"name": "mine", "type": "apikey", "expiration": "2026-12-20T00:00:00Z", "description": "ci", "system": False}]


class FakeGateway:
    def summary(self, app=None, since_hours=720, group_by="app,model"):
        row = {"app": "chatbot", "model": "gpt-6-luna", "day": "2026-11-23", "end_user": "carol", "requests": 3,
               "input_tokens": 30, "output_tokens": 10, "cache_read_tokens": 0, "cache_write_tokens": 0, "errors": 0,
               "cost_usd": 0.00002}
        return {"owner": "alice", "since_hours": since_hours, "rows": [row],
                "total": {"requests": 3, "input_tokens": 30, "output_tokens": 10, "cost_usd": 0.00002}}

    def list_keys(self):
        return [{"prefix": "dapp_NEW", "tenant": "engineering", "app": "chatbot", "owner": "alice",
                 "created": "2026-11-23T08:01:19.279926", "revoked": False}]


class FakeProm:
    def app_now(self, app, tenant):
        return {"cpu_cores": 0.002, "memory_bytes": 50e6, "cpu_limit_cores": 0.5, "memory_limit_bytes": 4e8,
                "pods": 1, "cpu_core_hours_24h": 0.01, "requests_24h": 5, "errors_24h": None}

    def app_series(self, app, tenant, start, end, step="5m"):
        return {"cpu_cores": [(0, 0.1), (1, 0.2)], "memory_bytes": [(0, 1.0), (1, 2.0)]}


def signed_in_with_fakes(monkeypatch):
    monkeypatch.setenv("DATAOS_FQDN", "x.dataos.cloud")
    monkeypatch.setenv("DATAOS_TENANT_ID", "engineering")
    for v in ("DAPP_DEV", "DAPP_PROMETHEUS_URL", "DAPP_OIDC_ISSUER", "DAPP_OIDC_CLIENT_ID", "clientSecret"):
        monkeypatch.delenv(v, raising=False)
    app = create_app(heimdall=FakeHeimdall(), login=FakeLogin(), platform_factory=lambda s, t=None: FakePlatform(),
                     gateway_factory=lambda s: FakeGateway(), prom_factory=lambda s: FakeProm())
    c = TestClient(app, base_url="https://testserver")
    c.get("/login", follow_redirects=False)
    c.get("/_dapp/callback?code=good-code&state=st1", follow_redirects=False)
    return c


def test_every_page_loads_through_the_real_routes(monkeypatch):
    c = signed_in_with_fakes(monkeypatch)
    home = c.get("/")
    assert home.status_code == 200 and "Your apps" in home.text
    assert "1 person" in home.text and ">everyone<" in home.text and ">public<" in home.text   # Access column
    assert "/apps/chatbot?tab=access" in home.text and "Manage" in home.text
    keys = c.get("/keys")
    assert keys.status_code == 200 and "dapp_NEW" in keys.text and "mine" in keys.text

    expect = {
        "overview": ["chatbot", "47.7 MiB", "app-service:running:1", "chatbot-pod"],
        "llm": ["Usage by user", "carol", "Gateway key", "dapp_NEW", "gpt-6-luna"],
        "secrets": ["dapp-chatbot-llm", "Add or replace a secret"],
        "access": ["Who can open this app", "carol", "data-dev", "Add people", "Advanced"],
        "logs": ["hello from the app"],
        "settings": ["Scale", "Delete app"],
    }
    for tab, needles in expect.items():
        r = c.get(f"/apps/chatbot?tab={tab}")
        assert r.status_code == 200, (tab, r.status_code, r.text[:200])
        for needle in needles:
            assert needle in r.text, (tab, needle)
    assert c.get("/apps/chatbot").status_code == 200                       # the default tab, which is what a click opens
    assert c.get("/apps/chatbot?tab=llm&days=7").status_code == 200
    assert c.get("/apps/missing").status_code == 404                         # unknown app: a clean 404, not a 500
    assert c.get("/api/apps").json()[0]["access"]["mode"] == "login"
