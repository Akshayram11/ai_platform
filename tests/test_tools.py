import json

import httpx
import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

from dapp.auth import Identity
from dapp_gateway.app import create_app
from dapp_gateway.store import Store
from dapp_gateway.tools import Vault, check_tool_url, jsonrpc_requests, response_errors

ALICE, BOB, CAROL = {"apikey": "alice-key"}, {"apikey": "bob-key"}, {"apikey": "carol-key"}
seen: list[httpx.Request] = []


class FakeHeimdall:
    def identify(self, token):
        return {"alice-key": Identity("alice"), "bob-key": Identity("bob"), "carol-key": Identity("carol")}.get(token)


def streamed(status: int, body: bytes = b"", **headers) -> httpx.Response:
    """Like a real upstream: the body arrives as a stream (MockTransport's content= is already read)."""
    return httpx.Response(status, headers=headers, stream=httpx.ByteStream(body))


def tool_server(req: httpx.Request) -> httpx.Response:
    """A tiny MCP server: JSON replies for tools/list, SSE for tools/call, an error for the 'boom' function."""
    seen.append(req)
    if req.headers.get("authorization") != "Bearer tool-secret":
        return streamed(401, b'{"error": "no"}')
    if req.method != "POST":
        return streamed(405)
    msg = json.loads(req.content)
    if "id" not in msg:
        return streamed(202)
    sid = {"mcp-session-id": "s-1"}
    if msg["method"] == "tools/call":
        name = msg["params"]["name"]
        reply = {"jsonrpc": "2.0", "id": msg["id"],
                 "result": {"content": [{"type": "text", "text": "4"}], "isError": name == "boom"}}
        return streamed(200, f"event: message\ndata: {json.dumps(reply)}\n\n".encode(),
                        **{"content-type": "text/event-stream", **sid})
    body = {"jsonrpc": "2.0", "id": msg["id"], "result": {"tools": [{"name": "add"}]}}
    return streamed(200, json.dumps(body).encode(), **{"content-type": "application/json", **sid})


def dataos(req: httpx.Request) -> httpx.Response:
    return httpx.Response(404, json={})


@pytest.fixture
def c(tmp_path, monkeypatch):
    seen.clear()
    monkeypatch.setenv("DATAOS_FQDN", "x.dataos.cloud")
    monkeypatch.setenv("DAPP_TOOL_INTERNAL_HOSTS", "calc-tool")
    app = create_app(store=Store(f"sqlite:///{tmp_path}/g.db"), heimdall=FakeHeimdall(),
                     upstream_transport=httpx.MockTransport(tool_server),
                     dataos_transport=httpx.MockTransport(dataos), vault=Vault(Fernet.generate_key().decode()))
    return TestClient(app)


def register(c, **extra):
    body = {"tenant": "ai", "name": "calc", "url": "http://calc-tool:8080/mcp", "description": "math",
            "auth_header": "Authorization", "auth_value": "Bearer tool-secret", **extra}
    return c.post("/admin/tools", json=body, headers=ALICE)


def rpc(c, headers, method="tools/list", rid=1, **params):
    return c.post("/mcp/ai/calc", json={"jsonrpc": "2.0", "id": rid, "method": method, "params": params},
                  headers={"accept": "application/json, text/event-stream", **headers})


def test_register_use_and_record(c):
    r = register(c)
    assert r.status_code == 200, r.text
    view = r.json()
    assert view["has_auth"] and "auth_value" not in view and "Bearer" not in json.dumps(view)  # never echoed
    assert view["owner"] == "alice" and view["allow"] == [] and view["endpoint"] == "/mcp/ai/calc"

    assert rpc(c, {}).status_code == 401                      # no DataOS identity
    assert rpc(c, BOB).status_code == 403                     # not on the allow list
    r = rpc(c, ALICE)                                         # the owner always may
    assert r.status_code == 200 and r.json()["result"]["tools"] == [{"name": "add"}]
    assert r.headers["mcp-session-id"] == "s-1"
    up = seen[-1]
    assert up.headers["authorization"] == "Bearer tool-secret"   # credential injected by the gateway...
    assert "apikey" not in up.headers and up.headers["x-dapp-user"] == "alice"  # ...the caller's own never forwarded
    assert up.headers["x-dapp-caller"] == "alice" and "x-dapp-end-user" not in up.headers
    rpc(c, {**ALICE, "x-dapp-caller": "app:evil"})            # a client can't claim to be someone else
    assert seen[-1].headers["x-dapp-caller"] == "alice"

    assert c.post("/admin/tools/ai/calc/access", json={"allow": ["bob"]}, headers=BOB).status_code == 403
    assert c.post("/admin/tools/ai/calc/access", json={"allow": ["bob"]}, headers=ALICE).json()["allow"] == ["bob"]
    r = rpc(c, BOB, "tools/call", 2, name="add", arguments={"a": 2, "b": 2})
    assert r.status_code == 200 and '"text": "4"' in r.text
    rpc(c, BOB, "tools/call", 3, name="boom")
    c.post("/mcp/ai/calc", json={"jsonrpc": "2.0", "method": "notifications/initialized"}, headers=BOB)  # not counted

    u = c.get("/admin/tool-usage?group_by=caller,tool_name", headers=ALICE).json()
    rows = {(r["caller"], r["tool_name"]): r for r in u["rows"]}
    assert rows[("alice", "")]["calls"] == 2
    assert rows[("bob", "add")]["calls"] == 1 and rows[("bob", "add")]["errors"] == 0
    assert rows[("bob", "boom")]["errors"] == 1                # isError in an SSE reply counts as a failed call
    assert u["total"] == {"calls": 4, "errors": 1}
    assert c.get("/admin/tool-usage", headers=BOB).json()["rows"] == []   # usage is the owner's to see

    # bob can see the tool exists (he may use it) but not its url, credential header or allow list
    listed = c.get("/admin/tools", headers=BOB).json()
    assert [t["name"] for t in listed] == ["calc"] and "url" not in listed[0] and "allow" not in listed[0]
    assert c.get("/admin/tools", headers=CAROL).json() == []
    assert c.get("/admin/tools/ai/calc", headers=CAROL).status_code == 404

    assert c.post("/admin/tools/ai/calc/access", json={"deny": ["bob"]}, headers=ALICE).json()["allow"] == []
    assert rpc(c, BOB).status_code == 403


def test_everyone_and_apps(c):
    register(c, allow=["*"])
    assert rpc(c, CAROL).status_code == 200                   # "*" = any signed-in DataOS user
    key = c.post("/admin/keys", json={"app": "chatbot", "tenant": "ai"}, headers=ALICE).json()["key"]
    app_h = {"authorization": f"Bearer {key}", "x-dapp-user": "dave"}
    assert rpc(c, app_h).status_code == 403                   # "*" is people; an app must be named
    c.post("/admin/tools/ai/calc/access", json={"allow": ["app:chatbot"]}, headers=ALICE)
    assert rpc(c, app_h).status_code == 200
    assert seen[-1].headers["x-dapp-user"] == "dave"          # the tool learns which person used the app
    assert seen[-1].headers["x-dapp-caller"] == "app:chatbot" and seen[-1].headers["x-dapp-end-user"] == "dave"
    c.post("/admin/tools/ai/calc/access", json={"deny": ["app:chatbot"], "allow": ["app:*"]}, headers=ALICE)
    other = c.post("/admin/keys", json={"app": "other", "tenant": "ai"}, headers=ALICE).json()["key"]
    assert rpc(c, {"authorization": f"Bearer {other}"}).status_code == 200   # app:* = every app
    rows = c.get("/admin/tool-usage?group_by=caller,end_user", headers=ALICE).json()["rows"]
    assert {"caller": "app:chatbot", "end_user": "dave"}.items() <= next(
        r for r in rows if r["caller"] == "app:chatbot").items()
    assert rpc(c, {"authorization": "Bearer dapp_revoked"}).status_code == 401
    mine = c.post("/admin/keys/personal", json={"tenant": "ai"}, headers=ALICE).json()["key"]
    assert rpc(c, {"authorization": f"Bearer {mine}"}).status_code == 403  # personal keys are for LLM calls only


def test_only_the_owner_changes_or_deletes(c):
    register(c)
    hijack = {"tenant": "ai", "name": "calc", "url": "https://evil.example.com/mcp"}
    assert c.post("/admin/tools", json=hijack, headers=BOB).status_code == 403
    assert c.delete("/admin/tools/ai/calc", headers=BOB).status_code == 403
    # updating without auth_value keeps the stored credential
    r = c.post("/admin/tools", json={"tenant": "ai", "name": "calc", "url": "http://calc-tool:8080/mcp",
                                     "auth_header": "Authorization", "description": "v2"}, headers=ALICE)
    assert r.json()["has_auth"] and r.json()["description"] == "v2"
    assert rpc(c, ALICE).status_code == 200
    r = c.post("/admin/tools", json={"tenant": "ai", "name": "calc", "url": "http://calc-tool:8080/mcp"},
               headers=ALICE)                                  # no header given either: still kept
    assert r.json()["auth_header"] == "Authorization" and rpc(c, ALICE).status_code == 200
    assert c.delete("/admin/tools/ai/calc", headers=ALICE).json()["deleted"] == 1
    assert rpc(c, ALICE).status_code == 404


def test_bad_input_is_refused(c):
    def save(**kw):
        return c.post("/admin/tools", json={"tenant": "ai", "name": "t", "url": "https://tools.example.com/mcp", **kw},
                      headers=ALICE).status_code
    assert save(name="Bad Name") == 422
    assert save(auth_header="X: y") == 422
    assert save(auth_value="secret") == 422                   # a value without a header
    assert save(allow=["bob", "no spaces!"]) == 422
    assert save(url="http://169.254.169.254/latest") == 422   # cloud metadata
    assert save(url="https://10.0.0.5/mcp") == 422
    assert save(url="https://localhost/mcp") == 422
    assert save(url="http://tools.example.com/mcp") == 422    # http only for listed internal hosts
    assert save(url="https://u:p@tools.example.com/mcp") == 422
    assert save() == 200


def test_credentials_need_the_gateway_key(tmp_path, monkeypatch):
    monkeypatch.setenv("DATAOS_FQDN", "x.dataos.cloud")
    monkeypatch.delenv("DAPP_GATEWAY_SECRET_KEY", raising=False)
    c = TestClient(create_app(store=Store(f"sqlite:///{tmp_path}/g.db"), heimdall=FakeHeimdall(),
                              upstream_transport=httpx.MockTransport(tool_server), vault=Vault("")))
    body = {"tenant": "ai", "name": "t", "url": "https://tools.example.com/mcp"}
    assert c.post("/admin/tools", json={**body, "auth_header": "Authorization", "auth_value": "x"},
                  headers=ALICE).status_code == 503
    assert c.post("/admin/tools", json=body, headers=ALICE).status_code == 200   # no credential: fine


def test_helpers():
    assert [m["id"] for m in jsonrpc_requests(b'[{"id":1,"method":"a"},{"method":"n"}]')] == [1]
    assert jsonrpc_requests(b"not json") == []
    sse = b'data: {"id": 1, "result": {"isError": true}}\n\ndata: {"id": 2, "result": {}}\n\n'
    assert response_errors("text/event-stream", sse) == {"1"}
    assert response_errors("application/json", b'{"id": [1], "error": {}}') == {"[1]"}   # odd ids don't crash
    assert check_tool_url("https://tools.example.com/mcp") == "https://tools.example.com/mcp"


def test_bridge_speaks_stdio(tmp_path):
    import io

    from dapp.bridge import run

    calls = {"n": 0}

    def creds():
        calls["n"] += 1
        if calls["n"] == 4:
            raise RuntimeError("Your DataOS login expired 3 minutes ago. Run `dataos-ctl login`")
        return {"authorization": "Bearer tool-secret"}   # the mock tool checks this header

    stdin = io.StringIO("\n".join([
        json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}),
        json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}),
        json.dumps({"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "add"}}),
        json.dumps({"jsonrpc": "2.0", "id": 3, "method": "tools/list"}),     # credentials fail for this one
        "",
    ]) + "\n")
    out = io.StringIO()
    seen.clear()
    run("https://gw/mcp/ai/calc", creds, stdin=stdin, stdout=out, transport=httpx.MockTransport(tool_server))
    lines = [json.loads(x) for x in out.getvalue().splitlines()]
    assert [m["id"] for m in lines] == [1, 2, 3]                       # one line per reply, notification silent
    assert lines[1]["result"]["content"][0]["text"] == "4"           # SSE reply unwrapped
    assert "dataos-ctl login" in lines[2]["error"]["message"]         # a clear error, bridge keeps running
    assert seen[-1].headers["mcp-session-id"] == "s-1"                # session carried after initialize


def test_platform_tool_rules():
    from dapp.config import Context
    from dapp.platform import Platform, PlatformError

    sent = {}

    class FakeGateway:
        def save_tool(self, body):
            sent.update(body)
            return {"name": body["name"]}

        def tool_access(self, tenant, name, allow, deny):
            return {"allow": allow, "deny": deny}

    class FakeClient:
        ctx = Context("t", "x.dataos.cloud", "ai", "alice", "k")

        def list_users(self):
            return [{"id": "bob", "email": "bob@corp.io"}]

    p = Platform.__new__(Platform)
    p.client, p.ctx = FakeClient(), FakeClient.ctx
    p._gateway = lambda: FakeGateway()
    r = p.save_tool("calc", url="https://t.example.com/mcp", auth_value="tok")
    assert sent["auth_header"] == "Authorization" and sent["auth_value"] == "Bearer tok"
    assert r["mcp_url"].endswith("/dapp-gateway/mcp/ai/calc")
    p.save_tool("calc", url="https://t.example.com/mcp", auth_header="X-Key", auth_value="tok")
    assert sent["auth_header"] == "X-Key" and sent["auth_value"] == "tok"     # custom header: sent as given
    p.save_tool("calc", url="https://t.example.com/mcp")
    assert sent["auth_value"] is None and sent["auth_header"] == ""           # keep whatever is stored
    assert p.tool_allow("calc", ["BOB@corp.io", "*", "app:chatbot"])["allow"] == ["*", "app:chatbot", "bob"]
    with pytest.raises(PlatformError, match="not a DataOS user"):
        p.tool_allow("calc", ["bobb"])
