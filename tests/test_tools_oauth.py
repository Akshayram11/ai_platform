"""OAuth MCP servers: each person signs in once; the gateway keeps their tokens (encrypted), adds them to their own calls
(and to calls an app makes for them), refreshes them, and says "connect it" when there is none."""
import base64
import hashlib
import json
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

from dapp.auth import Identity
from dapp_gateway import oauth
from dapp_gateway.app import create_app
from dapp_gateway.store import Store
from dapp_gateway.tools import Vault

ALICE, BOB, CAROL = {"apikey": "alice-key"}, {"apikey": "bob-key"}, {"apikey": "carol-key"}
MCP = "https://mcp.example.com/mcp"
REDIRECT = "https://x.dataos.cloud/dapp/tools/oauth/callback"


def streamed(status: int, body: dict | bytes = b"", **headers) -> httpx.Response:
    """Like a real upstream: the body arrives as a stream (the gateway relays it chunk by chunk)."""
    data = body if isinstance(body, bytes) else json.dumps(body).encode()
    return httpx.Response(status, headers={"content-type": "application/json", **headers}, stream=httpx.ByteStream(data))


class FakeHeimdall:
    def identify(self, token):
        return {"alice-key": Identity("alice"), "bob-key": Identity("bob"), "carol-key": Identity("carol")}.get(token)


class Server:
    """An MCP server behind OAuth (like Notion's): metadata, dynamic registration, a token endpoint that checks PKCE,
    and an MCP endpoint that takes only the tokens it issued."""

    def __init__(self):
        self.valid = {}          # access token -> person
        self.challenges = {}     # code -> (challenge, person)
        self.mcp_seen = []
        self.issued = 0
        self.expires_in = 3600
        self.metadata_host = "https://auth.example.com"
        self.needs_sign_in = True

    def handle(self, req: httpx.Request) -> httpx.Response:
        url = str(req.url)
        if url.startswith(MCP):
            self.mcp_seen.append(req)
            auth = req.headers.get("authorization", "")
            if not self.needs_sign_in or auth.removeprefix("Bearer ") in self.valid:
                msg = json.loads(req.content)
                body = {"jsonrpc": "2.0", "id": msg.get("id"), "result": {"tools": [{"name": "search"}],
                                                                          "who": self.valid.get(auth[7:], "")}}
                return streamed(200, body)
            return streamed(401, b"", **{"www-authenticate": 'Bearer resource_metadata="https://mcp.example.com/'
                                                           '.well-known/oauth-protected-resource/mcp"'})
        if url == "https://mcp.example.com/.well-known/oauth-protected-resource/mcp":
            return httpx.Response(200, json={"resource": MCP, "authorization_servers": [self.metadata_host],
                                             "scopes_supported": ["read"]})
        if url.startswith("https://auth.example.com/.well-known/oauth-authorization-server"):
            return httpx.Response(200, json={"issuer": "https://auth.example.com",
                                             "authorization_endpoint": "https://auth.example.com/authorize",
                                             "token_endpoint": "https://auth.example.com/token",
                                             "registration_endpoint": "https://auth.example.com/register",
                                             "code_challenge_methods_supported": ["S256"]})
        if url == "https://auth.example.com/register":
            body = json.loads(req.content)
            assert body["redirect_uris"] == [REDIRECT] and body["token_endpoint_auth_method"] == "none"
            return httpx.Response(201, json={"client_id": "cid"})
        if url == "https://auth.example.com/token":
            form = {k: v[0] for k, v in parse_qs(req.content.decode()).items()}
            assert form["client_id"] == "cid" and form["resource"] == MCP
            if form["grant_type"] == "authorization_code":
                challenge, person = self.challenges.pop(form["code"])
                digest = base64.urlsafe_b64encode(hashlib.sha256(form["code_verifier"].encode()).digest()).rstrip(b"=")
                if digest.decode() != challenge or form["redirect_uri"] != REDIRECT:
                    return httpx.Response(400, json={"error": "invalid_grant"})
            elif form["refresh_token"].startswith("refresh-"):
                person = form["refresh_token"].removeprefix("refresh-")
            else:
                return httpx.Response(400, json={"error": "invalid_grant"})
            self.issued += 1
            token = f"access-{person}-{self.issued}"
            self.valid[token] = person
            return httpx.Response(200, json={"access_token": token, "refresh_token": f"refresh-{person}",
                                             "expires_in": self.expires_in, "token_type": "Bearer", "scope": "read"})
        return httpx.Response(404)

    def sign_in(self, authorize_url: str, person: str) -> tuple[str, str]:
        """What the person's browser does at the server: approve, and come back with a code and the state."""
        q = {k: v[0] for k, v in parse_qs(urlsplit(authorize_url).query).items()}
        assert q["client_id"] == "cid" and q["code_challenge_method"] == "S256" and q["resource"] == MCP
        assert q["redirect_uri"] == REDIRECT and q["scope"] == "read"
        code = f"code-{person}-{len(self.challenges)}"
        self.challenges[code] = (q["code_challenge"], person)
        return code, q["state"]


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("DATAOS_FQDN", "x.dataos.cloud")
    server = Server()
    transport = httpx.MockTransport(server.handle)
    store = Store(f"sqlite:///{tmp_path}/g.db")
    app = create_app(store=store, heimdall=FakeHeimdall(), upstream_transport=transport, oauth_transport=transport,
                     dataos_transport=httpx.MockTransport(lambda r: httpx.Response(404, json={})),
                     vault=Vault(Fernet.generate_key().decode()))
    c = TestClient(app)
    assert c.post("/admin/tools", json={"tenant": "ai", "name": "notion", "url": MCP, "description": "pages",
                                        "allow": ["*"]}, headers=ALICE).status_code == 200
    return c, server, store


def rpc(c, headers):
    return c.post("/mcp/ai/notion", json={"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}},
                  headers=headers)


def connect(c, server, who, person):
    url = c.post("/admin/tools/ai/notion/oauth/start", json={"back": "/agents"}, headers=who).json()["authorize_url"]
    code, state = server.sign_in(url, person)
    return c.post("/admin/oauth/finish", json={"state": state, "code": code}, headers=who)


def test_setup_connect_and_each_person_uses_their_own_account(env):
    c, server, _ = env
    assert c.post("/admin/tools/ai/notion/oauth/setup", json={"redirect_uri": REDIRECT}, headers=BOB).status_code == 403
    r = c.post("/admin/tools/ai/notion/oauth/setup", json={"redirect_uri": REDIRECT}, headers=ALICE)
    assert r.json() == {"tool": "ai/notion", "auth_mode": "oauth", "needs_sign_in": True,
                        "issuer": "https://auth.example.com", "registered": True}

    r = rpc(c, BOB)                                                  # not connected yet: a clear way to fix it
    assert r.status_code == 401 and r.json()["error"]["type"] == "tool_not_connected"
    assert r.json()["error"]["connect_url"] == "https://x.dataos.cloud/dapp/tools?connect=notion"

    done = connect(c, server, BOB, "bob")
    assert done.json() == {"tool": "notion", "tenant": "ai", "connected": True, "back": "/agents"}
    assert rpc(c, BOB).json()["result"]["who"] == "bob"              # bob's own token, added by the gateway
    assert server.mcp_seen[-1].headers["authorization"].startswith("Bearer access-bob-")
    assert rpc(c, CAROL).status_code == 401                          # carol hasn't connected: not bob's account

    listed = {t["name"]: t for t in c.get("/admin/tools", headers=BOB).json()}
    assert listed["notion"]["auth_mode"] == "oauth" and listed["notion"]["connected"] is True
    assert c.get("/admin/tools", headers=ALICE).json()[0]["connected_users"] == 1
    assert c.get("/admin/tools/ai/notion/oauth/me", headers=CAROL).json()["connected"] is False
    assert "access-bob" not in json.dumps(c.get("/admin/tools", headers=ALICE).json())     # tokens never shown

    assert c.delete("/admin/tools/ai/notion/oauth/me", headers=BOB).json()["disconnected"] is True
    assert rpc(c, BOB).status_code == 401


def test_a_sign_in_is_used_once_and_only_by_who_started_it(env):
    c, server, _ = env
    c.post("/admin/tools/ai/notion/oauth/setup", json={"redirect_uri": REDIRECT}, headers=ALICE)
    url = c.post("/admin/tools/ai/notion/oauth/start", json={}, headers=BOB).json()["authorize_url"]
    code, state = server.sign_in(url, "bob")
    assert c.post("/admin/oauth/finish", json={"state": state, "code": code}, headers=CAROL).status_code == 400
    assert c.post("/admin/oauth/finish", json={"state": state, "code": code}, headers=BOB).status_code == 400  # gone
    assert connect(c, server, BOB, "bob").status_code == 200


def test_apps_act_for_their_user_only_when_named(env):
    c, server, _ = env
    c.post("/admin/tools/ai/notion/oauth/setup", json={"redirect_uri": REDIRECT}, headers=ALICE)
    connect(c, server, BOB, "bob")
    key = c.post("/admin/keys", json={"app": "agent-x", "tenant": "ai"}, headers=ALICE).json()["key"]
    app_h = {"authorization": f"Bearer {key}", "x-dapp-user": "bob"}
    c.post("/admin/tools/ai/notion/access", json={"allow": ["app:*"]}, headers=ALICE)
    assert rpc(c, app_h).status_code == 403                          # app:* can't borrow people's accounts
    c.post("/admin/tools/ai/notion/access", json={"allow": ["app:agent-x"]}, headers=ALICE)
    assert rpc(c, app_h).json()["result"]["who"] == "bob"            # the agent, for bob, with bob's account
    r = rpc(c, {**app_h, "x-dapp-user": "carol"})
    assert r.status_code == 401 and "carol has not connected notion" in r.json()["error"]["message"]


def test_refresh_and_a_revoked_token(env):
    c, server, store = env
    c.post("/admin/tools/ai/notion/oauth/setup", json={"redirect_uri": REDIRECT}, headers=ALICE)
    server.expires_in = 30                                           # runs out within the minute: refresh first
    connect(c, server, BOB, "bob")
    first = server.issued
    server.expires_in = 3600                                         # the refreshed token lasts
    assert rpc(c, BOB).status_code == 200 and server.issued == first + 1
    assert server.mcp_seen[-1].headers["authorization"] == f"Bearer access-bob-{first + 1}"
    server.valid.clear()                                             # the server revoked it
    assert rpc(c, BOB).status_code == 401
    assert store.get_token("ai", "notion", "bob") is None            # forgotten: bob connects again


def test_setup_refuses_private_addresses_and_knows_open_servers(env):
    c, server, _ = env
    server.metadata_host = "https://10.0.0.5"
    r = c.post("/admin/tools/ai/notion/oauth/setup", json={"redirect_uri": REDIRECT}, headers=ALICE)
    assert r.status_code == 422 and "not a public address" in r.text
    server.needs_sign_in = False
    r = c.post("/admin/tools/ai/notion/oauth/setup", json={"redirect_uri": REDIRECT}, headers=ALICE)
    assert r.json()["needs_sign_in"] is False and r.json()["auth_mode"] == "key"


def test_pkce_pair():
    verifier, challenge = oauth.pkce()
    assert len(verifier) >= 43 and "=" not in challenge
    assert challenge == base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()


def test_everyone_uses_the_owners_account_when_the_owner_shares_it(env):
    c, server, _ = env
    c.post("/admin/tools/ai/notion/oauth/setup", json={"redirect_uri": REDIRECT}, headers=ALICE)
    act = "/admin/tools/ai/notion/act-as"
    assert c.post(act, json={"mode": "owner"}, headers=ALICE).status_code == 409           # alice is not connected yet
    connect(c, server, ALICE, "alice")
    assert c.post(act, json={"mode": "owner"}, headers=BOB).status_code == 403              # only the owner chooses
    assert c.post(act, json={"mode": "everyone"}, headers=ALICE).status_code == 422
    assert c.post(act, json={"mode": "owner"}, headers=ALICE).json() == {"tool": "ai/notion", "act_as": "owner"}

    assert rpc(c, BOB).json()["result"]["who"] == "alice"           # bob never signed in: the tool sees alice's account
    assert server.mcp_seen[-1].headers["x-dapp-user"] == "bob"       # but the tool still learns who is asking
    key = c.post("/admin/keys", json={"app": "agent-x", "tenant": "ai"}, headers=ALICE).json()["key"]
    c.post("/admin/tools/ai/notion/access", json={"allow": ["app:*"]}, headers=ALICE)
    assert rpc(c, {"authorization": f"Bearer {key}", "x-dapp-user": "carol"}).json()["result"]["who"] == "alice"
    rows = {t["name"]: t for t in c.get("/admin/tools", headers=BOB).json()}
    assert rows["notion"]["act_as"] == "owner" and rows["notion"]["owner_connected"] is True

    c.delete("/admin/tools/ai/notion/oauth/me", headers=ALICE)         # alice signs out: the shared account is gone
    r = rpc(c, BOB)
    assert r.status_code == 401 and "shared through alice's account" in r.json()["error"]["message"]
    assert c.post(act, json={"mode": "user"}, headers=ALICE).json()["act_as"] == "user"


def test_the_owner_sees_how_the_tool_last_went(env):
    c, server, _ = env
    c.post("/admin/tools/ai/notion/oauth/setup", json={"redirect_uri": REDIRECT}, headers=ALICE)
    row = lambda who: {t["name"]: t for t in c.get("/admin/tools", headers=who).json()}["notion"]
    assert row(ALICE)["last_call"] is None and "last_call" not in row(BOB)          # only the owner sees it
    rpc(c, BOB)                                                                      # not connected: a failed call
    assert row(ALICE)["last_call"]["error"] is True and row(ALICE)["last_call"]["status"] == 401
    connect(c, server, BOB, "bob")
    rpc(c, BOB)
    assert row(ALICE)["last_call"]["error"] is False
