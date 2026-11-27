"""Tools page: add a tool (a preset or a custom MCP server), edit it, check that it answers, and go back to the agent."""
import re
from typing import ClassVar
from urllib.parse import parse_qs, urlsplit

import pytest
import test_dashboard
from test_dashboard import FakeGateway, FakePlatform, signed_in_with_fakes

import dapp_dashboard.app as dash
from dapp.agent_runtime.mcp_transport import ToolError
from dapp.usage import GatewayError

SAVED = []
ANSWERS = {"demo-tools": {"tools": [{"name": "calculate"}, {"name": "time_in"}]}}


class ToolPlatform(FakePlatform):
    def save_tool(self, name, **kw):
        SAVED.append((name, kw))
        return {"name": name}


class FakeServer:
    """Stands in for the gateway MCP call the dashboard makes as the signed-in person."""
    seen: ClassVar[list] = []

    def __init__(self, name, url, key, http, retry_seconds=None):
        self.name = name
        FakeServer.seen.append((name, url, key, retry_seconds))

    def call(self, method, params, user):
        assert method == "tools/list"
        if self.name not in ANSWERS:
            raise ToolError(f"{self.name}: HTTP 502 upstream refused")
        return ANSWERS[self.name]


class OAuthGateway(FakeGateway):
    """The gateway side of sign-in: does the server ask people to sign in? Where does the browser go? Who is connected?"""
    needs_sign_in = False
    calls: ClassVar[list] = []

    def oauth_setup(self, tenant, name, redirect_uri, **kw):
        OAuthGateway.calls.append(("setup", tenant, name, redirect_uri))
        return {"tool": f"{tenant}/{name}", "needs_sign_in": OAuthGateway.needs_sign_in}

    def oauth_start(self, tenant, name, back=""):
        OAuthGateway.calls.append(("start", tenant, name, back))
        return {"authorize_url": f"https://auth.example.com/authorize?state=s1&tool={name}"}

    def oauth_finish(self, state, code):
        OAuthGateway.calls.append(("finish", state, code))
        if code == "bad":
            raise GatewayError("this sign-in expired; connect again")
        return {"tool": "notion", "tenant": "engineering", "connected": True, "back": "/agents"}

    def tool_act_as(self, tenant, name, mode):
        OAuthGateway.calls.append(("act_as", tenant, name, mode))
        return {"act_as": mode}

    def oauth_disconnect(self, tenant, name):
        OAuthGateway.calls.append(("disconnect", tenant, name))
        return {"disconnected": True}

    def list_tools(self, tenant=None):
        return [*super().list_tools(tenant),
                {"name": "notion", "owner": "alice", "mine": True, "has_auth": False, "description": "pages",
                 "url": "https://mcp.example.com/mcp", "allow": ["*"], "auth_mode": "oauth", "connected": False,
                 "connected_users": 2, "endpoint": "/mcp/engineering/notion", "act_as": "user", "owner_connected": False,
                 "last_call": None}]


@pytest.fixture
def c(monkeypatch):
    SAVED.clear()
    FakeServer.seen.clear()
    OAuthGateway.calls = []
    OAuthGateway.needs_sign_in = False
    monkeypatch.setattr(dash, "Server", FakeServer)
    monkeypatch.setattr(test_dashboard, "FakeGateway", OAuthGateway)
    return signed_in_with_fakes(monkeypatch, platform_factory=lambda s, t=None: ToolPlatform())


def csrf(html):
    return re.search(r'name="csrf" value="([^"]+)"', html)[1]


def query(r):
    return {k: v[0] for k, v in parse_qs(urlsplit(r.headers["location"]).query).items()}


def test_the_add_form_offers_data_product_mcp(c):
    html = c.get("/tools?add=1").text
    assert '<details class="panel" id="add-tool" open>' in html
    assert 'value="dataproduct-mcp"' in html and 'data-url="https://x.dataos.cloud/dataproduct-mcp/api/v1"' in html
    assert "Custom MCP server" in html and 'name="auth_value" type="password"' in html
    assert "Check connection" in html and "Edit URL, description or key" in html          # on the tool you own
    assert '/tools/jira/edit' not in html                                                    # not on someone else's


def test_add_saves_checks_and_hands_back_to_the_agent(c):
    token = csrf(c.get("/tools").text)
    r = c.post("/tools/add", data={"name": "Demo-Tools", "url": "http://demo-tools:8080/mcp", "description": "math",
                                   "auth_header": "Authorization", "auth_value": "abc", "everyone": "1",
                                   "return": "/agents", "csrf": token}, follow_redirects=False)
    assert SAVED[-1] == ("demo-tools", {"url": "http://demo-tools:8080/mcp", "description": "math",
                                        "auth_header": "Authorization", "auth_value": "abc", "allow": ["*"]})
    _, url, key, retry = FakeServer.seen[-1]
    assert url.endswith("/mcp/engineering/demo-tools") and key and retry == 0     # as the person, no long retry
    q = query(r)
    assert q["msg"] == "demo-tools: connected · 2 tools: calculate, time_in" and q["added"] == "demo-tools"
    page = c.get(r.headers["location"]).text
    assert "Back to agent" in page and 'href="/agents"' in page and '"demo-tools"' in page     # handed over to the tab


def test_no_key_keeps_none_and_a_silent_tool_is_an_error(c):
    token = csrf(c.get("/tools").text)
    r = c.post("/tools/add", data={"name": "jira2", "url": "https://mcp.example.com/mcp", "csrf": token,
                                   "return": "//evil.example"}, follow_redirects=False)
    assert SAVED[-1][1]["auth_value"] is None and SAVED[-1][1]["allow"] is None
    q = query(r)
    assert q["err"].startswith("jira2: saved, but it did not answer through the gateway") and "return" not in q
    assert "Back to agent" not in c.get(r.headers["location"]).text                # never a link to another site


def test_edit_keeps_or_removes_the_key(c):
    token = csrf(c.get("/tools").text)
    c.post("/tools/demo-tools/edit", data={"url": "http://demo-tools:8080/mcp", "auth_header": "Authorization",
                                           "csrf": token}, follow_redirects=False)
    assert SAVED[-1][1]["auth_value"] is None                                       # empty field: keep it
    c.post("/tools/demo-tools/edit", data={"url": "http://demo-tools:8080/mcp", "remove_credential": "1",
                                           "auth_value": "ignored", "csrf": token}, follow_redirects=False)
    assert SAVED[-1][1]["auth_value"] == ""                                         # removed
    r = c.post("/tools/demo-tools/check", data={"csrf": token}, follow_redirects=False)
    assert query(r)["msg"].startswith("demo-tools: connected")
    assert c.post("/tools/add", data={"name": "x", "url": "https://a/mcp", "csrf": "nope"},
                  follow_redirects=False).status_code == 403


def test_a_server_that_asks_for_sign_in_sends_the_owner_to_sign_in(c):
    OAuthGateway.needs_sign_in = True
    token = csrf(c.get("/tools").text)
    r = c.post("/tools/add", data={"name": "notion", "url": "https://mcp.example.com/mcp", "return": "/agents",
                                   "csrf": token}, follow_redirects=False)
    assert SAVED[-1][1]["auth_value"] is None
    assert OAuthGateway.calls[0] == ("setup", "engineering", "notion", "https://x.dataos.cloud/tools/oauth/callback")
    assert r.status_code == 303 and r.headers["location"].startswith("https://auth.example.com/authorize")
    kind, _, name, back = OAuthGateway.calls[1]
    assert (kind, name) == ("start", "notion") and back.startswith("/tools?") and "return=%2Fagents" in back


def test_a_key_skips_the_sign_in_check(c):
    token = csrf(c.get("/tools").text)
    c.post("/tools/add", data={"name": "jira2", "url": "https://mcp.example.com/mcp", "auth_value": "abc",
                               "csrf": token}, follow_redirects=False)
    assert not [x for x in OAuthGateway.calls if x[0] == "setup"]


def test_connect_callback_and_disconnect(c):
    html = c.get("/tools?connect=notion").text
    token = csrf(html)
    assert "You have not connected it" in html and "oauth-focus" in html and "2 people have connected" in html
    assert "/tools/notion/check" not in html                                          # no key to check on this one
    r = c.post("/tools/notion/connect", data={"return": "/agents", "csrf": token}, follow_redirects=False)
    assert r.headers["location"].startswith("https://auth.example.com/authorize")
    assert OAuthGateway.calls[-1] == ("start", "engineering", "notion", "/agents")
    assert c.post("/tools/notion/connect", data={"csrf": "nope"}, follow_redirects=False).status_code == 403
    back = c.get("/tools/oauth/callback?code=abc&state=s1", follow_redirects=False)
    assert back.status_code == 303 and back.headers["location"].startswith("/agents?")
    assert query(back)["connected"] == "notion" and "connected+as+you" in back.headers["location"]
    bad = c.get("/tools/oauth/callback?code=bad&state=s1", follow_redirects=False)
    assert "expired" in query(bad)["err"] and bad.headers["location"].startswith("/tools?")
    declined = c.get("/tools/oauth/callback?error=access_denied&error_description=No", follow_redirects=False)
    assert "not completed: No" in query(declined)["err"]
    c.post("/tools/notion/disconnect", data={"csrf": token}, follow_redirects=False)
    assert OAuthGateway.calls[-1] == ("disconnect", "engineering", "notion")


def test_the_owner_sees_status_agents_and_the_shared_account_switch(c):
    html = c.get("/tools").text
    assert "Whose account does it use?" in html and 'name="mode" value="owner"' in html
    assert "Connect it with your account first" in html                              # owner_connected is not set here
    token = csrf(html)
    c.post("/tools/notion/acts-as", data={"mode": "owner", "csrf": token}, follow_redirects=False)
    assert OAuthGateway.calls[-1] == ("act_as", "engineering", "notion", "owner")
    r = c.post("/tools/notion/acts-as", data={"mode": "user", "csrf": token}, follow_redirects=False)
    assert "own+account+again" in r.headers["location"] or "own%20account" in r.headers["location"]
    assert c.post("/tools/notion/acts-as", data={"mode": "owner", "csrf": "nope"}, follow_redirects=False).status_code == 403
    assert "none of your agents use it" in html and "working" in html or "no calls yet" in html
