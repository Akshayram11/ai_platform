"""Tools page: add a tool (a preset or a custom MCP server), edit it, check that it answers, and go back to the agent."""
import re
from typing import ClassVar
from urllib.parse import parse_qs, urlsplit

import pytest
from test_dashboard import FakePlatform, signed_in_with_fakes

import dapp_dashboard.app as dash
from dapp.agent_runtime.mcp_transport import ToolError

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


@pytest.fixture
def c(monkeypatch):
    SAVED.clear()
    FakeServer.seen.clear()
    monkeypatch.setattr(dash, "Server", FakeServer)
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
