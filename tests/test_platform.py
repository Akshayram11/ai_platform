import json

import httpx

from dapp.client import DataOS
from dapp.config import Context
from dapp.ctl import ApiWriter
from dapp.platform import Platform
from dapp.spec import AppSpec

CTX = Context("t", "x.dataos.cloud", "ai", "alice", "key123", None)
BASE = "/poros/api/v2/tenants/ai"



def plain(data):
    """Secret values as the API writer sends them (base64, like dataos-ctl) back to the text the app will see."""
    import base64
    return {k: base64.b64decode(v).decode() for k, v in data.items()}

class FakePoros:
    def __init__(self):
        self.store: dict[tuple[str, str], dict] = {}
        self.calls: list[tuple[str, str]] = []

    def __call__(self, req: httpx.Request) -> httpx.Response:
        assert req.headers["apikey"] == "key123"
        path = req.url.path
        self.calls.append((req.method, path))
        parts = path[len(BASE):].strip("/").split("/")
        if req.method == "GET" and parts == ["resources", "app"]:
            owner = req.url.params.get("owner")
            rows = [v for (t, _), v in self.store.items() if t == "app" and (not owner or v.get("owner") == owner)]
            return httpx.Response(200, json={"resources": rows})
        if req.method == "GET" and len(parts) == 3:
            r = self.store.get((parts[1], parts[2]))
            return httpx.Response(200, json=r) if r else httpx.Response(404, json={"message": "nf"})
        if req.method == "GET" and len(parts) == 4 and parts[3] == "runtime":
            return httpx.Response(200, json={"nodeDetails": [{"nodeName": "chatbotsvc-abc", "phase": "Running"}]})
        if req.method in ("POST", "PUT"):
            body = json.loads(req.content)
            body.setdefault("owner", "alice")
            self.store[(body["type"], body["name"])] = body
            return httpx.Response(200, json=body)
        if req.method == "DELETE":
            return httpx.Response(200 if self.store.pop((parts[1], parts[2]), None) else 404, json={})
        return httpx.Response(400, json={"path": path})


def make():
    fake = FakePoros()
    client = DataOS(CTX, transport=httpx.MockTransport(fake))
    return Platform(client, writer=ApiWriter(client)), fake


def test_deploy_list_status_scale_delete():
    p, fake = make()
    spec = AppSpec.model_validate({"name": "chatbot", "description": "d", "image": "img", "compute": "c", "command": ["run"]})
    res = p.deploy(spec)
    assert res["url"] == "https://x.dataos.cloud/chatbot/"
    assert ("POST", f"{BASE}/resources") in fake.calls

    apps = p.list_apps()
    assert [a["name"] for a in apps] == ["chatbot"] and apps[0]["managed_by_dapp"]

    st = p.status("chatbot")
    assert st["service"] == "chatbotsvc" and st["runtime_detail"]["nodeDetails"][0]["phase"] == "Running"

    p.scale("chatbot", 3)
    assert fake.store[("app", "chatbot")]["spec"]["replicas"] == 3

    p.set_secret("chatbot", "openai", {"OPENAI_API_KEY": "sk-x"})
    assert plain(fake.store[("secret", "dapp-chatbot-openai")]["spec"]["data"]) == {"OPENAI_API_KEY": "sk-x"}
    assert {"id": "dapp-chatbot-openai"} in fake.store[("app", "chatbot")]["spec"]["service"]["secrets"]

    p.delete("chatbot")
    assert ("app", "chatbot") not in fake.store


def test_grant_goes_to_api_under_spec():
    p, fake = make()
    fake.store[("app", "chatbot")] = {"name": "chatbot", "type": "app", "version": "v1alpha", "spec": {}}
    out = p.grant_access("chatbot", "bob", use_case="view-app")
    g = fake.store[("grant", out["grant"])]
    assert g["spec"]["subjects"] == ["users:id:bob"] and "grant" not in g


def test_conflict_is_retried_other_errors_are_not(monkeypatch):
    import pytest

    from dapp import ctl

    monkeypatch.setattr(ctl.time, "sleep", lambda s: None)
    calls = {"n": 0}

    def flaky():
        calls["n"] += 1
        if calls["n"] < 3:
            raise ctl.WriteError("Operation cannot be fulfilled: the object has been modified; apply again")
        return "ok"

    assert ctl.with_conflict_retry(flaky) == "ok" and calls["n"] == 3

    def forbidden():
        calls["n"] += 1
        raise ctl.WriteError("Forbidden: not-allowed")

    calls["n"] = 0
    with pytest.raises(ctl.WriteError):
        ctl.with_conflict_retry(forbidden)
    assert calls["n"] == 1


def test_llm_secret_holds_only_the_dapp_key(monkeypatch):
    p, fake = make()
    monkeypatch.setattr(p, "_gateway", lambda: type("G", (), {"issue_key": lambda s, app, tenant, rotate=False: {"key": "dapp_x"}})())
    spec = AppSpec.model_validate({"name": "bot", "description": "d", "image": "i", "compute": "c",
                                   "command": ["x"], "public": True, "llm": True})
    p.deploy(spec)
    secret = fake.store[("secret", "dapp-bot-llm")]
    assert plain(secret["spec"]["data"]) == {"DAPP_LLM_KEY": "dapp_x"}   # no OPENAI_API_KEY / ANTHROPIC_API_KEY copies
    env = fake.store[("app", "bot")]["spec"]["service"]["envs"]
    assert env["OPENAI_BASE_URL"].endswith("/openai/v1") and "OPENAI_API_KEY" not in env


def test_secret_apply_fills_vars_and_refuses_unsafe_files(tmp_path):
    import pytest

    from dapp.platform import PlatformError

    good = tmp_path / "s.yaml"
    good.write_text("# note ${NOT_NEEDED}\nname: dapp-gw\nversion: v2alpha\ntype: secret\ntenant: ai\n"
                    "spec:\n  type: key-value\n  data:\n    OPENAI_API_KEY: ${OPENAI_API_KEY}\n"
                    "    # ANTHROPIC_API_KEY: ${ANTHROPIC_API_KEY}\n")
    p, fake = make()
    r = p.apply_secret_file(str(good), {"OPENAI_API_KEY": "sk-test"})
    assert plain(fake.store[("secret", "dapp-gw")]["spec"]["data"]) == {"OPENAI_API_KEY": "sk-test"}
    assert r["secrets"] == [{"name": "dapp-gw", "tenant": "ai", "keys": ["OPENAI_API_KEY"]}] and "sk-test" not in str(r)

    with pytest.raises(PlatformError, match="unset or empty"):      # would have written "" over a working key
        p.apply_secret_file(str(good), {"OPENAI_API_KEY": ""})
    shape = tmp_path / "shape.yaml"
    shape.write_text("name: dapp-x-llm\nversion: v2alpha\ntype: secret\ntenant: ai\nspec:\n  type: key-value\n"
                     "  data:\n    DAPP_LLM_KEY: <issued by the gateway on deploy>\n")
    with pytest.raises(PlatformError, match="placeholder"):
        p.apply_secret_file(str(shape), {})
    app = tmp_path / "app.yaml"
    app.write_text("name: a\nversion: v1alpha\ntype: app\ntenant: ai\nspec: {}\n")
    with pytest.raises(PlatformError, match="only type: secret"):
        p.apply_secret_file(str(app), {})
    fake.store.pop(("secret", "dapp-gw"))
    p.apply_secret_file(str(good), {"OPENAI_API_KEY": "sk-test"}, dry_run=True)
    assert ("secret", "dapp-gw") not in fake.store


def test_secret_yamls_in_the_repo_match_what_dapp_creates():
    from pathlib import Path

    import yaml

    from dapp.spec import render_secret

    root = Path(__file__).resolve().parent.parent / "deploy" / "secrets"
    for f in root.glob("*.yaml"):
        doc = yaml.safe_load(f.read_text())
        assert doc["type"] == "secret" and doc["version"] == "v2alpha" and doc["spec"]["type"] == "key-value", f.name
    gen = render_secret("dapp-<app>-llm", tenant="engineering", app="<app>", description="x", data={"DAPP_LLM_KEY": "k"})
    shape = yaml.safe_load((root / "app-llm.generated.yaml").read_text())
    assert shape["spec"]["data"].keys() == gen["spec"]["data"].keys() == {"DAPP_LLM_KEY"}
    assert shape["spec"]["type"] == gen["spec"]["type"] and shape["version"] == gen["version"]
    oidc = yaml.safe_load((root / "app-oidc.generated.yaml").read_text())
    assert set(oidc["spec"]["data"]) == {"clientSecret", "DAPP_SESSION_KEY"}
    # nothing in these files may be a real-looking key
    for f in root.glob("*.yaml"):
        assert "sk-" not in f.read_text() and "dapp_" + "x" * 5 not in f.read_text(), f.name


def test_api_key_create_sends_user_id_like_dataos_ctl():
    import json

    import httpx

    from dapp.client import DataOS

    seen = {}

    def handler(req: httpx.Request) -> httpx.Response:
        seen["body"] = json.loads(req.content)
        return httpx.Response(200, json={"name": "k", "data": {"apikey": "secret"}})

    c = DataOS(CTX, transport=httpx.MockTransport(handler))
    c.create_apikey("k", duration="24h")
    assert seen["body"] == {"type": "apikey", "name": "k", "duration": "24h", "description": "", "user_id": "alice"}


def test_system_keys_are_flagged_and_never_leak_key_material():
    import httpx

    from dapp.client import DataOS

    rows = [{"name": "ultimately_proud_ostrich_1", "description": "poros apikey for runnable runas",
             "data": {"apikey": "SECRET"}}, {"name": "mine", "description": "ci", "data": {"apikey": "SECRET"}}]
    c = DataOS(CTX, transport=httpx.MockTransport(lambda r: httpx.Response(200, json=rows)))
    keys = c.list_apikeys()
    assert [k["system"] for k in keys] == [True, False] and "SECRET" not in str(keys)


def test_llm_rotation_keeps_old_key_until_the_pod_rolls_over(monkeypatch):
    from dapp import platform as plat

    p, fake = make()
    fake.store[("app", "bot")] = {"name": "bot", "type": "app", "version": "v1alpha",
                                  "spec": {"service": {"secrets": [{"id": "dapp-bot-llm"}]}}}
    keys = [{"app": "bot", "tenant": "ai", "prefix": "dapp_OLD", "revoked": False}]
    calls = []

    class G:
        def list_keys(self): return keys
        def issue_key(self, app, tenant, rotate=False):
            calls.append(("issue", rotate)); return {"key": "dapp_NEWKEYVALUE", "prefix": "dapp_NEWKEYV"}
        def revoke_key(self, app, tenant, prefix): calls.append(("revoke", prefix)); return {"revoked": 1, "prefix": prefix}

    monkeypatch.setattr(p, "_gateway", lambda: G())
    pods = iter([{"old-pod"}, {"old-pod"}, {"new-pod"}])  # before; still old; rolled over
    monkeypatch.setattr(p, "_app_pods", lambda app: next(pods))
    monkeypatch.setattr(plat.time, "sleep", lambda s: None)
    r = p.rotate_llm_key("bot")
    assert calls == [("issue", False), ("revoke", "dapp_OLD")]       # old key only revoked after the new pod is up
    assert r["rolled_over"] and r["revoked"] == ["dapp_OLD"] and r["new_key_prefix"] == "dapp_NEWKEYV"
    assert plain(fake.store[("secret", "dapp-bot-llm")]["spec"]["data"]) == {"DAPP_LLM_KEY": "dapp_NEWKEYVALUE"}


def test_llm_rotation_leaves_old_keys_if_the_app_never_restarts(monkeypatch):
    from dapp import platform as plat

    p, fake = make()
    fake.store[("app", "bot")] = {"name": "bot", "type": "app", "version": "v1alpha",
                                  "spec": {"service": {"secrets": [{"id": "dapp-bot-llm"}]}}}
    revoked = []
    G = type("G", (), {"list_keys": lambda s: [{"app": "bot", "tenant": "ai", "prefix": "dapp_OLD", "revoked": False}],
                       "issue_key": lambda s, a, t, rotate=False: {"key": "k", "prefix": "dapp_NEW"},
                       "revoke_key": lambda s, a, t, x: revoked.append(x)})
    monkeypatch.setattr(p, "_gateway", lambda: G())
    monkeypatch.setattr(p, "_app_pods", lambda app: {"old-pod"})  # never changes
    clock = iter(range(0, 1000, 60))
    monkeypatch.setattr(plat.time, "monotonic", lambda: next(clock))
    monkeypatch.setattr(plat.time, "sleep", lambda s: None)
    r = p.rotate_llm_key("bot", timeout=100)
    assert not r["rolled_over"] and r["still_active"] == ["dapp_OLD"] and revoked == []


def test_rotate_refuses_apps_that_dont_use_the_gateway():
    import pytest

    from dapp.platform import PlatformError

    p, fake = make()
    fake.store[("app", "plain")] = {"name": "plain", "type": "app", "version": "v1alpha", "spec": {"service": {}}}
    with pytest.raises(PlatformError, match="llm: true"):
        p.rotate_llm_key("plain")


def test_api_key_name_rule_matches_heimdall():
    import httpx
    import pytest

    from dapp.client import DataOS, DataOSError

    c = DataOS(CTX, transport=httpx.MockTransport(lambda r: httpx.Response(200, json={"name": "x"})))
    for ok in ("my_agent_key", "MyKey1", "ci:prod_1"):
        c.create_apikey(ok)
    for bad in ("my-agent-key", "has space", "", "a/b"):
        with pytest.raises(DataOSError, match="invalid"):
            c.create_apikey(bad)


def _platform_with_directory(users, status=200):
    import httpx

    from dapp.client import DataOS
    from dapp.ctl import ApiWriter
    from dapp.platform import Platform

    fake = FakePoros()
    fake.store[("app", "chatbot")] = {"name": "chatbot", "type": "app", "version": "v1alpha", "tenant": "ai",
                                      "spec": {"app": {"owners": ["alice"]}, "service": {"envs": {
                                          "DAPP_AUTH": "oidc", "DAPP_ALLOWED_USERS": "alice", "DAPP_ALLOWED_ROLES": ""}}}}

    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.path == "/heimdall/api/v1/users":
            return httpx.Response(status, json=users if status == 200 else {"message": "forbidden"})
        return fake(req)

    client = DataOS(CTX, transport=httpx.MockTransport(handler))
    return Platform(client, writer=ApiWriter(client)), fake


DIRECTORY = [
    {"id": "carolktmdcio", "name": "Carol K", "email": "carol.k@tmdc.io", "type": "person"},
    {"id": "chetangehlottmdcio", "name": "Chetan Gehlot", "email": "chetan.g@tmdc.io", "type": "person"},
    {"id": "deploy-bot", "name": "bot", "email": "", "type": "application"},
]


def test_adding_access_checks_the_directory_and_resolves_what_was_typed():
    import pytest

    from dapp.platform import PlatformError

    p, fake = _platform_with_directory(DIRECTORY)
    r = p.allow("chatbot", ["CarolKTMDCIO", "chetan.g@tmdc.io", "deploy-bot", "*"])
    assert r["resolved"] == {"CarolKTMDCIO": "carolktmdcio", "chetan.g@tmdc.io": "chetangehlottmdcio"}
    stored = fake.store[("app", "chatbot")]["spec"]["service"]["envs"]["DAPP_ALLOWED_USERS"]
    assert set(stored.replace('"', "").split(",")) == {"alice", "carolktmdcio", "chetangehlottmdcio", "deploy-bot", "*"}

    p, fake = _platform_with_directory(DIRECTORY)
    with pytest.raises(PlatformError, match=r"'chetangehlotmdcio' is not a DataOS user\. Did you mean: chetangehlottmdcio"):
        p.allow("chatbot", ["chetangehlotmdcio"])          # a one-letter typo is caught, with the right id suggested
    with pytest.raises(PlatformError, match="'nobody' is not a DataOS user"):
        p.allow("chatbot", ["carolktmdcio", "nobody"])      # all or nothing: carol is not added either
    assert fake.store[("app", "chatbot")]["spec"]["service"]["envs"]["DAPP_ALLOWED_USERS"] == "alice"


def test_adding_access_still_works_when_the_user_list_cant_be_read():
    p, _ = _platform_with_directory([], status=403)       # viewer may not list users: accept what was typed
    r = p.allow("chatbot", ["someone"])
    assert r["resolved"] == {} and "someone" in r["users"]


def test_removing_access_never_needs_the_user_to_exist():
    p, fake = _platform_with_directory(DIRECTORY)
    p.allow("chatbot", ["carolktmdcio"])
    fake.store[("app", "chatbot")]["spec"]["service"]["envs"]["DAPP_ALLOWED_USERS"] += ",left-company"
    r = p.deny("chatbot", ["left-company", "carolktmdcio"])   # someone deleted from DataOS can still be removed
    assert r["users"] == ["alice"]


def test_the_app_list_names_the_secrets_and_volumes_each_app_uses():
    p, fake = make()
    fake.store[("app", "bot")] = {"name": "bot", "type": "app", "owner": "alice", "tenant": "ai", "tags": ["dapp"], "spec": {"service": {
        "secrets": [{"id": "ai:dapp-bot-llm"}, {"id": "dapp-bot-oidc"}], "volumes": [{"id": "bot-data", "directory": "/data", "readOnly": False}]}}}
    fake.store[("app", "plain")] = {"name": "plain", "type": "app", "owner": "alice", "tenant": "ai", "tags": [], "spec": {"service": {
        "disk": {"size": "2Gi", "mountPath": "/d"}}}}
    rows = {a["name"]: a for a in p.list_apps(with_access=True)}
    assert rows["bot"]["secrets"] == ["dapp-bot-llm", "dapp-bot-oidc"]                          # names only, tenant prefix dropped
    assert rows["bot"]["volumes"] == [{"id": "bot-data", "directory": "/data", "readOnly": False}] and rows["bot"]["disk"] is None
    assert rows["plain"]["secrets"] == [] and rows["plain"]["volumes"] == [] and rows["plain"]["disk"] == "2Gi"
