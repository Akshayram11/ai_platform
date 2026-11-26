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
        self.owners = ["alice"]
        self.claude = {"exists": False, "state": None, "token": False}

    def list_apps(self, everyone=False, with_access=False):
        mk = lambda n, acc: {"name": n, "tenant": "engineering", "owner": "alice", "state": "running", "status": "active",
                             "runtime": "app-service:running:1", "error": None, "managed_by_dapp": True,
                             "url": f"https://x/{n}/", "access": acc}
        rows = [mk("chatbot", {"mode": "login", "everyone": False, "people": 1, "roles": 0}),
                mk("dash", {"mode": "login", "everyone": True, "people": 1, "roles": 0}),
                mk("gw", {"mode": "public"})]
        rows[0].update(secrets=["dapp-chatbot-llm", "dapp-chatbot-oidc"], volumes=[], disk=None)
        rows[1].update(secrets=["dapp-dash-code"], volumes=[{"id": "dash-data", "directory": "/data", "readOnly": False}], disk=None)
        rows[2].update(secrets=[], volumes=[], disk="2Gi")
        return rows

    def status(self, name):
        if name == "missing":
            from dapp.platform import PlatformError
            raise PlatformError("app 'missing' not found")
        return {"name": name, "tenant": "engineering", "owner": "alice", "state": "running", "status": "active",
                "runtime": "app-service:running:1", "error": None, "url": f"https://x/{name}/", "service": f"{name}svc",
                "spec": {"replicas": 1, "compute": "c", "runAsUser": "alice", "app": {"description": "d", "owners": ["alice"]},
                         "service": {"image": "i:1", "port": 8080, "secrets": [{"id": f"dapp-{name}-llm"}], "http": {}}},
                "runtime_detail": {"nodeDetails": [{"name": f"{name}-pod", "phase": "running", "containers": ["c"],
                                                   "dataplane": "dp1",
                                                   "labels": {"app": f"servicev2alpha{name}svc-ab12"}}]}}

    def service_name(self, app):
        return f"servicev2alpha{app}svc-ab12"

    def list_secrets(self, app):
        return [{"name": f"dapp-{app}-llm", "managed_by_dapp": True, "referenced": True, "owner": "alice"}]

    def list_access(self, app):
        return [{"name": "g1", "use_case": "uc", "subjects": ["users:id:bob"], "status": {}}]

    def allowlist(self, app):
        return {"app": app, "mode": "login", "owners": self.owners, "users": ["alice", "carol"], "roles": ["data-dev"]}

    def allow(self, app, users=(), roles=(), verify=True):
        self.calls.append(("allow", app, list(users)))
        return {}

    def claude_instance(self):
        return {"app": "claude-alice", "url": "https://x.dataos.cloud/claude-alice/", **self.claude}

    def claude_instances(self):
        return [{"name": "claude-alice", "owner": "alice", "state": self.claude["state"] or "stopped"},
                {"name": "claude-bob", "owner": "bob", "state": "running"}]

    def deploy_claude(self, d, *, compute, token=""):
        from dapp.claude import clean_token
        token = clean_token(token)
        self.calls.append(("deploy_claude", d.cpu, d.cpu_limit, d.memory, d.memory_limit, compute, bool(token)))
        if self.claude["exists"]:
            from dapp.platform import PlatformError
            raise PlatformError("your Claude Code (claude-alice) is already deployed")
        self.claude = {"exists": True, "state": "initializing", "token": bool(token) or self.claude["token"]}
        return {"app": "claude-alice", "url": "https://x/claude-alice/", "token": self.claude["token"], "notes": []}

    def set_claude_token(self, token):
        from dapp.claude import clean_token
        self.calls.append(("claude_token", bool(clean_token(token))))
        self.claude = {**self.claude, "token": True}
        return {"attached": self.claude["exists"]}

    def stop_claude(self):
        self.calls.append(("stop_claude",))
        self.claude = {**self.claude, "exists": False, "state": None}
        return {}

    def delete_claude(self):
        self.calls.append(("delete_claude",))
        self.claude = {"exists": False, "state": None, "token": False}
        return {}

    def models(self):
        return getattr(self, "deployed", []) + [{"name": "small", "tenant": "engineering", "owner": "", "app": "llm-small", "repo": "", "description": "",
                 "created": "", "source": "config", "ready": True},
                {"name": "qwen-test", "tenant": "engineering", "owner": "alice", "app": "model-qwen-test",
                 "repo": "Qwen/Qwen2.5-0.5B-Instruct-GGUF:Q4_K_M", "description": "", "created": "", "source": "dashboard",
                 "ready": True},
                {"name": "bobs-model", "tenant": "engineering", "owner": "bob", "app": "model-bobs-model",
                 "repo": "bartowski/Llama-3.2-1B-Instruct-GGUF:Q4_K_M", "description": "", "created": "", "source": "dashboard",
                 "ready": False}]

    def deploy_model(self, d, *, compute, dataplane):
        self.calls.append(("deploy_model", d.name, d.source, compute, dataplane, d.server_env(), d.cpu_limit, d.memory_limit))
        if d.name == "taken":
            from dapp.platform import PlatformError
            raise PlatformError("a model called taken already exists")
        self.deployed = getattr(self, "deployed", []) + [{"name": d.name, "tenant": "engineering", "owner": "alice", "app": d.app,
                                                          "repo": d.source, "description": "", "created": "", "source": "dashboard",
                                                          "ready": False}]
        return {"name": d.name, "app": d.app, "url": f"https://x/{d.app}/"}

    def delete_model(self, name):
        self.calls.append(("delete_model", name))
        return {"name": name, "app": f"model-{name}", "volume": f"model-{name}-data", "output": ""}

    def delete_model_volume(self, volume):
        self.calls.append(("delete_volume", volume))
        return True

    def personal_llm_key(self, rotate=False):
        return {"key": "dapp_PERSONALKEY", "app": "user:alice", "prefix": "dapp_PERSONAL", "base_url": "https://x/openai/v1"}

    def list_users(self):
        return [{"id": "carol", "name": "Carol K", "email": "c@x", "type": "person"}]

    def logs_text(self, name, tail=100, node=None):
        return "==> pod / c <==\nhello from the app"

    def list_keys(self):
        return [{"name": "mine", "type": "apikey", "expiration": "2026-12-20T00:00:00Z", "description": "ci", "system": False}]

    def tool_allow(self, name, who):
        self.calls.append(("tool_allow", name, who))
        return {"allow": who}

    def tool_deny(self, name, who):
        self.calls.append(("tool_deny", name, who))
        return {"allow": []}

    def delete_tool(self, name):
        self.calls.append(("delete_tool", name))
        return {"deleted": 1}


class FakeGateway:
    def models_served_by(self, app):
        return ["small"] if app == "chatbot" else []

    def summary(self, app=None, since_hours=720, group_by="app,model", model=None):
        row = {"app": "chatbot", "model": "gpt-6-luna", "day": "2026-11-23", "end_user": "carol", "requests": 3,
               "input_tokens": 30, "output_tokens": 10, "cache_read_tokens": 0, "cache_write_tokens": 0, "errors": 0,
               "cost_usd": 0.00002}
        return {"owner": "alice", "since_hours": since_hours, "rows": [row],
                "total": {"requests": 3, "input_tokens": 30, "output_tokens": 10, "cost_usd": 0.00002}}

    def list_keys(self):
        return [{"prefix": "dapp_NEW", "tenant": "engineering", "app": "chatbot", "owner": "alice",
                 "created": "2026-11-23T08:01:19.279926", "revoked": False}]

    def list_tools(self, tenant=None):
        return [{"name": "demo-tools", "owner": "alice", "mine": True, "has_auth": True, "description": "math",
                 "url": "http://demo-tools:8080/mcp", "allow": ["*", "app:chatbot", "bob"],
                 "endpoint": "/mcp/engineering/demo-tools"},
                {"name": "jira", "owner": "bob", "mine": False, "has_auth": True, "description": "tickets",
                 "endpoint": "/mcp/engineering/jira"}]

    def tool_usage(self, tool=None, tenant=None, since_hours=720, group_by="tool,caller"):
        cols = group_by.split(",")
        row = {"tool": "demo-tools", "caller": "carol", "tool_name": "calculate", "calls": 4, "errors": 1,
               "avg_latency_ms": 12}
        return {"rows": [{k: v for k, v in row.items() if k in cols or k in ("calls", "errors", "avg_latency_ms")}],
                "total": {"calls": 4, "errors": 1}}


class FakeProm:
    def app_now(self, app, tenant, service=None):
        assert service == f"servicev2alpha{app}svc-ab12"      # never just the name: others' apps may share it
        return {"cpu_cores": 0.002, "memory_bytes": 50e6, "cpu_limit_cores": 0.5, "memory_limit_bytes": 4e8,
                "pods": 1, "cpu_core_hours_24h": 0.01, "requests_24h": 5, "errors_24h": None}

    def app_series(self, app, tenant, start, end, step="5m", service=None):
        return {"cpu_cores": [(0, 0.1), (1, 0.2)], "memory_bytes": [(0, 1.0), (1, 2.0)]}


def signed_in_with_fakes(monkeypatch, **extra):
    monkeypatch.setenv("DATAOS_FQDN", "x.dataos.cloud")
    monkeypatch.setenv("DATAOS_TENANT_ID", "engineering")
    for v in ("DAPP_DEV", "DAPP_PROMETHEUS_URL", "DAPP_OIDC_ISSUER", "DAPP_OIDC_CLIENT_ID", "clientSecret"):
        monkeypatch.delenv(v, raising=False)
    extra.setdefault("platform_factory", lambda s, t=None: FakePlatform())
    app = create_app(heimdall=FakeHeimdall(), login=FakeLogin(),
                     gateway_factory=lambda s: FakeGateway(), prom_factory=lambda s: FakeProm(), **extra)
    c = TestClient(app, base_url="https://testserver")
    c.get("/login", follow_redirects=False)
    c.get("/_dapp/callback?code=good-code&state=st1", follow_redirects=False)
    return c


def test_every_page_loads_through_the_real_routes(monkeypatch):
    c = signed_in_with_fakes(monkeypatch)
    home = c.get("/")
    assert home.status_code == 200 and "Your apps" in home.text
    assert "1 person" in home.text and ">everyone<" in home.text and ">public<" in home.text   # Access column
    assert "/storage" in home.text and 'class="on"' in home.text                              # nav, current page marked
    for needle in ("<th title=\"DataOS secrets", ">Secrets<", ">Volumes<", "dapp-chatbot-llm", "dapp-chatbot-oidc", "dapp-dash-code",
                   "dash-data", "mounted at /data", "disk 2Gi", "none: files are lost on restart"):   # secrets and volumes, same table
        assert needle in home.text, needle
    assert "?tab=secrets" in home.text
    assert "/apps/chatbot?tab=access" in home.text and "Manage" in home.text
    keys = c.get("/keys")
    assert keys.status_code == 200 and "dapp_NEW" in keys.text and "mine" in keys.text

    expect = {
        "overview": ["chatbot", "47.7 MiB", "app-service:running:1", "chatbot-pod", "Model <code>small</code>: who used it",
                     "Volumes", "none: files are lost on restart"],
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

    tools = c.get("/tools")
    assert tools.status_code == 200, tools.text[:300]
    for needle in ("demo-tools", "every signed-in DataOS user", "app:chatbot", "dapp tool connect jira",
                   "shared with you", "calculate", "4 calls, 1 errors"):
        assert needle in tools.text, needle
    assert "http://demo-tools:8080/mcp" in tools.text and tools.text.count("Upstream:") == 1  # only for your own tool


def test_tool_actions_from_the_dashboard(monkeypatch):
    from urllib.parse import parse_qs, urlparse

    fake = FakePlatform()
    monkeypatch.setenv("DATAOS_FQDN", "x.dataos.cloud")
    monkeypatch.setenv("DATAOS_TENANT_ID", "engineering")
    for v in ("DAPP_DEV", "DAPP_PROMETHEUS_URL", "DAPP_OIDC_ISSUER", "DAPP_OIDC_CLIENT_ID", "clientSecret"):
        monkeypatch.delenv(v, raising=False)
    app = create_app(heimdall=FakeHeimdall(), login=FakeLogin(), platform_factory=lambda s, t=None: fake,
                     gateway_factory=lambda s: FakeGateway(), prom_factory=lambda s: FakeProm())
    c = TestClient(app, base_url="https://testserver")
    c.get("/login", follow_redirects=False)
    c.get("/_dapp/callback?code=good-code&state=st1", follow_redirects=False)
    import re as _re
    csrf = _re.search(r'name="csrf" value="([^"]+)"', c.get("/tools").text).group(1)

    def post(path, **form):
        r = c.post(path, data={"csrf": csrf, **form}, follow_redirects=False)
        return parse_qs(urlparse(r.headers["location"]).query)

    assert "allowed" in post("/tools/demo-tools/allow", users="carol", apps="chatbot", everyone="1")["msg"][0]
    assert fake.calls[-1] == ("tool_allow", "demo-tools", ["carol", "app:chatbot", "*"])
    assert "not a valid id" in post("/tools/demo-tools/allow", users="no way!")["err"][0]   # no 500
    assert "Type a user" in post("/tools/demo-tools/allow")["err"][0]
    post("/tools/demo-tools/deny", value="app:chatbot")
    assert fake.calls[-1] == ("tool_deny", "demo-tools", ["app:chatbot"])
    assert "to confirm" in post("/tools/demo-tools/delete", confirm="nope")["err"][0]
    post("/tools/demo-tools/delete", confirm="demo-tools")
    assert fake.calls[-1] == ("delete_tool", "demo-tools")
    assert c.post("/tools/demo-tools/delete", data={"csrf": "bad", "confirm": "demo-tools"},
                  follow_redirects=False).status_code == 403


def test_storage_pages_against_the_real_server(monkeypatch, tmp_path):
    """Dashboard -> immortal-obj as the signed-in person: browse, try SQL, save, share, delete, read what an app shared."""
    import importlib
    import re
    import sys
    from pathlib import Path

    from starlette.testclient import TestClient as StarletteClient

    import dapp_objects

    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "apps" / "immortal-obj" / "app"))
    monkeypatch.setenv("IMMORTAL_DATA", str(tmp_path))
    monkeypatch.setenv("TOOL_TOKEN", "tool-secret")
    sys.modules.pop("server", None)
    server = importlib.import_module("server")

    class Resp:
        def __init__(self, body):
            self.body = body

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return self.body

    with StarletteClient(server.app, base_url="http://immortal-obj:8080") as tc:
        def as_gateway(caller):
            def opener(req, timeout):
                headers = {k: v for k, v in req.header_items() if k.lower() != "authorization"}
                headers.update({"Authorization": "Bearer tool-secret", "x-dapp-caller": caller})
                return Resp(tc.post("/mcp", content=req.data, headers=headers).content)
            return dapp_objects.Client("http://gw/mcp/engineering/immortal-obj", "k", opener=opener)

        chat = as_gateway("app:chatbot").open("chat", for_user="alice")      # what the chatbot keeps for alice
        chat.sql("create table msgs (role text, content text)")
        chat.sql("insert into msgs values (?, ?)", ["user", "where is revenue?"])
        chat.share("alice")
        shared_ref = chat.name and f"app:chatbot/{chat.name}"

        c = signed_in_with_fakes(monkeypatch, objects_factory=lambda s: as_gateway("alice"))
        home = c.get("/storage")
        assert home.status_code == 200 and "Shared with you" in home.text and shared_ref in home.text
        csrf = re.search(r'name="csrf" value="([^"]+)"', home.text).group(1)

        def post(url, /, **form):
            return c.post(url, data={"csrf": csrf, **form})

        saved = post("/storage/put", ref="scratch", key="hello", value='{"from": "dashboard"}')
        assert saved.status_code == 200 and "Saved hello" in saved.text and "_kv" in saved.text and "dashboard" in saved.text
        ran = post("/storage/sql", ref="scratch", query="create table t (a int)")
        assert "Done. No rows returned." in ran.text
        post("/storage/sql", ref="scratch", query="insert into t values (42)")
        assert "42" in post("/storage/sql", ref="scratch", query="select * from t").text
        assert "attach" in post("/storage/sql", ref="scratch", query="attach database 'x' as y").text.lower()   # guarded

        seen = c.get("/storage/object", params={"ref": shared_ref})            # the app's copy, read-only for alice
        assert "where is revenue?" in seen.text and "shared with you" in seen.text and "Save a value" not in seen.text
        assert 'class="flash err"' in post("/storage/put", ref=shared_ref, key="x", value="1").text    # read share only

        wrote = post("/storage/write", ref="my-files", path="notes/hello.txt", content="Hi from the dashboard")
        assert "notes/hello.txt" in wrote.text and "volume" in wrote.text
        assert "Hi from the dashboard" in c.get("/storage/object", params={"ref": "my-files", "file": "notes/hello.txt"}).text

        assert "bob" in post("/storage/share", ref="scratch", who="bob", mode="read").text
        assert "bob" not in post("/storage/unshare", ref="scratch", who="bob").text.split("Who can use it")[1].split("Delete")[0]
        assert "Type scratch to confirm" in post("/storage/drop", ref="scratch", confirm="nope").text
        gone = post("/storage/drop", ref="scratch", confirm="scratch")
        assert "Deleted scratch" in gone.text and 'href="/storage/object?ref=scratch"' not in gone.text.replace("/dapp", "")


def test_access_requests_end_to_end(monkeypatch, tmp_path):
    """Someone without access asks from the dashboard; the app's owner sees it on the Access tab and approves or declines."""
    import importlib
    import re
    import sys
    from pathlib import Path

    from starlette.testclient import TestClient as StarletteClient

    import dapp_objects

    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "apps" / "immortal-obj" / "app"))
    monkeypatch.setenv("IMMORTAL_DATA", str(tmp_path))
    monkeypatch.setenv("TOOL_TOKEN", "tool-secret")
    sys.modules.pop("server", None)
    server = importlib.import_module("server")

    class Resp:
        def __init__(self, body):
            self.body = body

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return self.body

    with StarletteClient(server.app, base_url="http://immortal-obj:8080") as tc:
        def dashboard_store():                                  # the dashboard's own key: app:dapp
            def opener(req, timeout):
                headers = {k: v for k, v in req.header_items() if k.lower() != "authorization"}
                headers.update({"Authorization": "Bearer tool-secret", "x-dapp-caller": "app:dapp"})
                return Resp(tc.post("/mcp", content=req.data, headers=headers).content)
            return dapp_objects.Client("http://gw/mcp/engineering/immortal-obj", "k", opener=opener)

        fake = FakePlatform()
        c = signed_in_with_fakes(monkeypatch, platform_factory=lambda s, t=None: fake,
                                 app_objects_factory=dashboard_store)
        assert c.get("/request-access", params={"app": "Bad Name"}).status_code == 404

        page = c.get("/request-access", params={"app": "chatbot"})
        assert page.status_code == 200 and "Send request" in page.text
        csrf = re.search(r'name="csrf" value="([^"]+)"', page.text).group(1)

        def post(url, /, **form):
            return c.post(url, data={"csrf": csrf, **form})

        sent = post("/request-access", app="chatbot", note="I am on  the sales team")
        assert "Request sent" in sent.text and "waiting" in sent.text and "I am on the sales team" in sent.text
        post("/request-access", app="chatbot", note="updated note")                 # asking twice keeps one request
        assert post("/request-access", app="Bad Name").status_code == 404

        tab = c.get("/apps/chatbot", params={"tab": "access"}).text
        assert tab.count("/approve") == 1 and "updated note" in tab and "Access requests" in tab
        home = c.get("/").text
        assert "waiting for you" in home and "1 request" in home                    # badge on the home page
        rid = re.search(r"/requests/([0-9a-f]+)/approve", tab).group(1)

        assert "Allowed alice" in post(f"/apps/chatbot/requests/{rid}/approve").text
        assert fake.calls[-1] == ("allow", "chatbot", ["alice"])                    # same as `dapp access allow`
        again = post(f"/apps/chatbot/requests/{rid}/approve").text
        assert "no longer open" in again and fake.calls.count(fake.calls[-1]) == 1  # no double approval
        assert "Nobody is waiting" in c.get("/apps/chatbot", params={"tab": "access"}).text
        assert "approved" in c.get("/request-access", params={"app": "chatbot"}).text

        post("/request-access", app="chatbot", note="again please")
        tab = c.get("/apps/chatbot", params={"tab": "access"}).text
        rid2 = re.search(r"/requests/([0-9a-f]+)/deny", tab).group(1)
        assert rid2 != rid and "Declined the request from alice" in post(f"/apps/chatbot/requests/{rid2}/deny").text
        assert "declined" in c.get("/request-access", params={"app": "chatbot"}).text
        assert [x for x in fake.calls if x[0] == "allow"] == [("allow", "chatbot", ["alice"])]   # a decline grants nothing

        post("/request-access", app="chatbot", note="third")                        # only an owner may decide
        rid3 = re.search(r"/requests/([0-9a-f]+)/approve",
                         c.get("/apps/chatbot", params={"tab": "access"}).text).group(1)
        fake.owners = ["bob"]
        assert "Access requests" not in c.get("/apps/chatbot", params={"tab": "access"}).text
        refused = post(f"/apps/chatbot/requests/{rid3}/approve")
        assert refused.status_code == 403 and [x for x in fake.calls if x[0] == "allow"] == [("allow", "chatbot", ["alice"])]


def test_access_requests_need_storage(monkeypatch):
    monkeypatch.delenv("DAPP_OBJECTS_URL", raising=False)
    monkeypatch.delenv("DAPP_LLM_KEY", raising=False)
    c = signed_in_with_fakes(monkeypatch)
    page = c.get("/request-access", params={"app": "chatbot"})
    assert page.status_code == 200 and "switched on for this dashboard" in page.text
    assert "waiting for you" not in c.get("/").text                                       # no badge without storage


def test_models_tab_lists_deploys_customizes_and_deletes(monkeypatch):
    import re
    import time
    from urllib.parse import urlparse

    import dapp_dashboard.app as dash
    from dapp import models as models_mod
    from dapp.models import CATALOG

    quants = {"Q4_K_M": 0.46, "Q5_K_M": 0.49, "Q8_0": 0.63}
    monkeypatch.setattr(dash, "hf_quants", lambda repo, **k: quants)
    monkeypatch.setattr(models_mod, "hf_quants", lambda repo, **k: quants)               # the check at deploy time
    fake = FakePlatform()
    c = signed_in_with_fakes(monkeypatch, platform_factory=lambda s, t=None: fake)

    home = c.get("/models")
    assert home.status_code == 200 and "Your models" in home.text and 'class="on"' in home.text
    for needle in ("qwen-test", "gateway config", "Qwen2.5 0.5B Instruct", "Gemma 3 4B Instruct", "Your own GGUF model", "Deploy"):
        assert needle in home.text, needle
    assert "deployed as" in home.text and "bobs-model" in home.text
    # one big sortable table: quality and speed bars, what each needs, and what it costs to keep running
    for needle in ("Which model should I pick?", "Just trying it, or simple tasks", 'id="catalog"', "click a column to sort", "data-sort",
                   "Quality", "Speed", "Memory", "Download", "$/day", "$/month", "Good for", "Licence", "words/s", "thinking mode",
                   "Deploy a custom model", "Cost estimates:", "starting…", ">ready<", "dapp model deploy ID", 'id="filter"'):
        assert needle in home.text, needle
    table = home.text[home.text.index('id="catalog"'):]
    assert table.count("Deploy</button>") == len(CATALOG) and 'class="card"' not in home.text      # a row per model, no boxes
    row = table[table.index("Qwen2.5 0.5B Instruct"):table.index("Llama 3.2 1B Instruct")]
    assert "1.5Gi" in row and "$0.61" in row and "0.46 GB" in row and "Apache-2.0" in row and "Fast &amp; light" in table
    page = c.get("/models/deploy", params={"id": "qwen2.5-0.5b"})                    # the Customize menu holds every setting
    assert page.status_code == 200 and "Customize" in page.text and "Reset to defaults" in page.text
    for label in ("Quantization", "Compute", "CPU request", "CPU limit", "Memory request", "Memory limit", "Disk (Gi)",
                  "Context size", "Parallel requests", "Threads", "Batch size", "Micro-batch size", "Flash attention",
                  "KV cache type (keys)", "KV cache type (values)", "Max tokens per reply", "Reasoning / thinking",
                  "Jinja chat template", "Context shift", "Continuous batching", "Request timeout", "Temperature", "Top-k",
                  "Top-p", "Min-p", "Repeat penalty"):
        assert label in page.text, label
    assert 'value="2"' in page.text and "Q8_0 (0.63 GB)" in page.text and 'name="name" type="text" value="qwen2.5-0.5b"' in page.text
    for field, value in (("ctx", "8192"), ("memory_limit", "1.5Gi"), ("temperature", "0.7"), ("top_k", "20"), ("top_p", "0.8"),
                         ("repeat_penalty", "1.1"), ("min_p", "")):                        # this model's own defaults are prefilled
        assert f'name="{field}" type="{"text" if field == "memory_limit" else "number"}" value="{value}"' in page.text, field
    assert "Defaults for Qwen2.5 0.5B Instruct" in page.text and "KV cache for a 8192-token context" in page.text
    assert "Estimated running cost" in page.text and 'id="e-day"' in page.text and "const E =" in page.text      # live cost panel
    assert '"Q8_0": 0.63' in page.text and "Quality is a rough guide" in page.text
    assert "Sampling from its generation_config.json" in page.text
    other = c.get("/models/deploy", params={"id": "phi3.5-mini"}).text                    # another model: other numbers and reasons
    assert 'name="ctx" type="number" value="4096"' in other and 'name="memory_limit" type="text" value="5Gi"' in other
    assert 'name="temperature" type="number" value=""' in other and "llama.cpp&#39;s own defaults" in other
    gemma = c.get("/models/deploy", params={"id": "gemma3-4b"}).text
    assert 'name="temperature" type="number" value="1.0"' in gemma and 'name="top_k" type="number" value="64"' in gemma
    custom = c.get("/models/deploy", params={"id": "custom"}).text
    assert 'name="temperature" type="number" value=""' in custom and "general defaults" in custom
    assert c.get("/models/deploy", params={"id": "nope"}).status_code == 404
    assert 'name="hf"' in c.get("/models/deploy", params={"id": "custom"}).text
    csrf = re.search(r'name="csrf" value="([^"]+)"', page.text).group(1)

    form = {"csrf": csrf, "model_id": "qwen2.5-0.5b", "name": "my-qwen", "quant": "Q8_0", "compute": "engineering-compute",
            "cpu": "1", "cpu_limit": "3", "memory": "1Gi", "memory_limit": "2Gi", "disk_gi": "3", "ctx": "8192", "parallel": "2",
            "threads": "3", "batch": "1024", "ubatch": "256", "flash_attn": "on", "cache_type_k": "q8_0", "cache_type_v": "q8_0",
            "max_tokens": "300", "reasoning": "off", "jinja": "on", "context_shift": "on", "cont_batching": "on",
            "timeout_s": "900", "temperature": "0.3", "top_k": "", "top_p": "0.9", "min_p": "", "repeat_penalty": "1.1"}
    done = c.post("/models/deploy", data=form)
    assert done.status_code == 200 and "Deploying my-qwen" in done.text and "Use it" in done.text
    assert "Starting." in done.text and "location.reload()" in done.text                    # a new model is "starting" until ready
    assert 'href="https://x.dataos.cloud/chatbot/?model=my-qwen"' in done.text and "Try it in the DataOS chatbot" in done.text
    call = next(x for x in fake.calls if x[0] == "deploy_model")
    _, name, source, compute, dataplane, env, cpu_limit, mem_limit = call
    assert (name, source, compute, dataplane) == ("my-qwen", "Qwen/Qwen2.5-0.5B-Instruct-GGUF:Q8_0", "engineering-compute", "poldp01")
    assert (cpu_limit, mem_limit) == ("3", "2Gi")
    assert env["LLAMA_ARG_CTX_SIZE"] == "8192" and env["LLAMA_ARG_N_PARALLEL"] == "2" and env["LLAMA_ARG_FLASH_ATTN"] == "on"
    assert env["LLAMA_ARG_CACHE_TYPE_K"] == "q8_0" and env["LLAMA_ARG_N_PREDICT"] == "300" and env["LLAMA_ARG_REASONING"] == "off"
    assert env["LLAMA_ARG_TEMPERATURE"] == "0.3" and env["LLAMA_ARG_TOP_P"] == "0.9" and env["LLAMA_ARG_REPEAT_PENALTY"] == "1.1"
    assert "LLAMA_ARG_TOP_K" not in env and "LLAMA_ARG_MIN_P" not in env                  # empty boxes stay llama.cpp's own

    for bad, text in (({"name": "gpt-4o"}, "provider"), ({"memory_limit": "300Mi", "memory": "256Mi"}, "too small to load"),
                      ({"quant": "Q2_Z"}, "offers: Q4_K_M, Q5_K_M, Q8_0"), ({"ctx": "many"}, "not a number"),
                      ({"cpu_limit": "20"}, "8 cores"), ({"name": "taken"}, "already exists")):
        r = c.post("/models/deploy", data={**form, **bad})
        assert r.status_code == 400 and text in r.text, (bad, r.text[:200])
        assert 'name="ctx"' in r.text and "Customize" in r.text and "<details class=\"customize\" open>" in r.text   # form kept, menu open
    assert c.post("/models/deploy", data={**form, "csrf": "bad"}).status_code == 403

    detail = c.get("/models/qwen-test")                                                   # use it: key, curl, python, usage
    assert detail.status_code == 200
    for needle in ("Create a dapp key for me", 'curl -s https://x.dataos.cloud/dapp-gateway/openai/v1/chat/completions',
                   '"model": "qwen-test"', "OpenAI(base_url=", "Usage by app", "Delete model", "Server settings", "Ready.",
                   'Bearer $DAPP_KEY', 'api_key=os.environ["DAPP_KEY"]', 'href="https://x.dataos.cloud/chatbot/?model=qwen-test"',
                   "Try it in the DataOS chatbot", "reserved, up to"):
        assert needle in detail.text, needle
    assert "OPENAI_API_KEY" not in detail.text and "location.reload()" not in detail.text      # a ready model doesn't reload
    assert "Starting." in c.get("/models/bobs-model").text and "location.reload()" in c.get("/models/bobs-model").text
    assert "Delete model" not in c.get("/models/bobs-model").text and "Delete model" not in c.get("/models/small").text
    assert "gateway's own configuration" in c.get("/models/small").text
    assert c.get("/models/nothing").status_code == 404

    made = c.post("/models/qwen-test/key", data={"csrf": csrf}, follow_redirects=False)
    created = urlparse(made.headers["location"]).query
    shown = c.get("/models/qwen-test?" + created)
    assert "dapp_PERSONALKEY" in shown.text and "export DAPP_KEY=dapp_PERSONALKEY" in shown.text
    assert "dapp_PERSONALKEY" not in c.get("/models/qwen-test?" + created).text           # shown once

    assert "Type qwen-test to confirm" in c.post("/models/qwen-test/delete", data={"csrf": csrf, "confirm": "no"}).text
    gone = c.post("/models/qwen-test/delete", data={"csrf": csrf, "confirm": "qwen-test"})
    assert "Deleted qwen-test" in gone.text
    time.sleep(0.2)                                                                      # the Volume goes in the background
    assert ("delete_model", "qwen-test") in fake.calls and ("delete_volume", "model-qwen-test-data") in fake.calls


def test_claude_code_tab_deploy_token_stop_and_delete(monkeypatch):
    import re

    fake = FakePlatform()
    c = signed_in_with_fakes(monkeypatch, platform_factory=lambda s, t=None: fake)
    page = c.get("/claude")                                                  # nothing deployed yet: the deploy form
    assert page.status_code == 200 and 'class="on"' in page.text and ">Claude Code<" in page.text
    for needle in ("Deploy your Claude Code", "Deploy my Claude Code", "claude-alice", "Customize", "CPU request", "Memory limit", "Compute",
                   "Estimated running cost", 'id="e-day"', "claude setup-token", "Stay signed in to Claude", "Everyone's Claude Code",
                   "claude-bob", "only its owner can open it", "250m", "Stop</b> it when you're not using it"):
        assert needle in page.text, needle
    assert "Stop (keep my files)" not in page.text and "Delete model" not in page.text
    csrf = re.search(r'name="csrf" value="([^"]+)"', page.text).group(1)

    token = "sk-ant-oat01-" + "A1b2C3d4" * 7
    form = {"csrf": csrf, "compute": "engineering-compute", "cpu": "500m", "cpu_limit": "3", "memory": "1Gi", "memory_limit": "3Gi",
            "token": token + "\n" + "Zz9-_Yy8"}
    bad = c.post("/claude/deploy", data={**form, "memory": "100Mi", "memory_limit": "300Mi"})   # bad resources: the form comes back with the reason
    assert bad.status_code == 400 and "at least 512Mi" in bad.text and "<details class=\"customize\" open>" in bad.text
    assert c.post("/claude/deploy", data={**form, "token": "short"}).status_code == 400
    assert c.post("/claude/deploy", data={**form, "csrf": "bad"}).status_code == 403

    done = c.post("/claude/deploy", data=form)
    assert "Deploying your Claude Code" in done.text and fake.calls[-1] == ("deploy_claude", "500m", "3", "1Gi", "3Gi", "engineering-compute", True)
    assert token not in done.text and "Zz9-_Yy8" not in done.text                          # the token is never echoed back
    assert "starting…" in done.text and "location.reload()" in done.text and "signed in to Claude" in done.text
    assert "Stop (keep my files)" in done.text and "Delete" in done.text

    fake.claude["state"] = "running"
    run = c.get("/claude").text
    assert 'href="https://x.dataos.cloud/claude-alice/"' in run and "Open Claude Code" in run and "location.reload()" not in run

    saved = c.post("/claude/token", data={"csrf": csrf, "token": token})
    assert "Token saved" in saved.text and "restarts" in saved.text and token not in saved.text and fake.calls[-1] == ("claude_token", True)
    assert "paste the token" in c.post("/claude/token", data={"csrf": csrf, "token": ""}).text or "Claude token" in c.post("/claude/token", data={"csrf": csrf, "token": "x"}).text
    assert c.post("/claude/stop", data={"csrf": csrf}).status_code == 200 and fake.calls[-1] == ("stop_claude",)
    assert "Deploy my Claude Code" in c.get("/claude").text and "already stored" in c.get("/claude").text                 # stopped: deploy again
    fake.claude = {"exists": True, "state": "running", "token": True}
    assert "Type claude-alice to confirm" in c.post("/claude/delete", data={"csrf": csrf, "confirm": "no"}).text
    assert "Deleted, with its token" in c.post("/claude/delete", data={"csrf": csrf, "confirm": "claude-alice"}).text
    assert fake.calls[-1] == ("delete_claude",)


def test_the_nav_marks_the_current_section_with_or_without_the_root_path():
    from fastapi import Request

    from dapp_dashboard.app import TEMPLATES

    def on(path, root="/dapp"):
        scope = {"type": "http", "method": "GET", "path": path, "headers": [], "query_string": b"", "root_path": root}
        page = TEMPLATES.env.from_string('{% extends "base.html" %}{% block body %}x{% endblock %}').render(
            request=Request(scope), s=type("S", (), {"user_id": "alice", "tenant": "t", "csrf": "t"})(), fqdn="x", flash=None, error=None)
        import re
        return re.findall(r'<a href="[^"]*" class="on">([^<]+)</a>', page)

    for path in ("/dapp/claude", "/claude"):                       # the server may or may not put the root path in front
        assert on(path) == ["Claude Code"], path
    assert on("/dapp/models/qwen-test") == ["Models"] and on("/models") == ["Models"]
    assert on("/dapp/") == ["Apps"] and on("/dapp") == ["Apps"] and on("/") == ["Apps"]
    assert on("/dapp/apps/claude-code") == ["Apps"] and on("/dapp/apps/models-bot?tab=llm") == ["Apps"]     # an app named like a section
    assert on("/storage/object", root="") == ["Storage"] and on("/keys", root="") == ["Keys"]
