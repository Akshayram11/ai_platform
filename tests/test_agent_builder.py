"""Builder routes, published source, and the gateway tool loop."""

import importlib.util
import json
import re
import shutil
import sqlite3
from types import SimpleNamespace

import httpx
from fastapi.testclient import TestClient
from test_dashboard import FakeLogin

from dapp.agent_runtime import engine
from dapp.agent_runtime.engine import AgentConfig, Chat, run_agent
from dapp.auth import Identity
from dapp.client import DataOSError
from dapp_dashboard.app import create_app

DRAFT = {"title": "Log Auditor", "model": "gpt-6-luna", "instructions": "Use logs and cite evidence.",
         "tools": ["dapp-monitor"]}


class RegistryDB:
    url = "http://objects.test"
    key = "test-only"

    def __init__(self):
        self.db = sqlite3.connect(":memory:", check_same_thread=False)
        self.db.row_factory = sqlite3.Row

    def open(self, name):
        assert name == "agent-builder"
        return self

    def sql(self, query, params=None):
        with self.db:
            return [dict(row) for row in self.db.execute(query, params or [])]


def test_real_dashboard_routes_save_preview_publish_and_rollback(monkeypatch, tmp_path):
    monkeypatch.setenv("DATAOS_TENANT_ID", "engineering")
    monkeypatch.setenv("DATAOS_FQDN", "test.invalid")
    monkeypatch.setenv("DAPP_LLM_KEY", "preview-key")
    monkeypatch.setenv("DAPP_AGENT_MODELS", "gpt-6-luna")
    registry = RegistryDB()
    deployed = []
    live = {}
    views = []
    failing_deployment = False

    def resource(kind, name):
        assert kind == "app"
        return live.get(name)

    def deploy(spec):
        assert spec.llm and spec.metrics and spec.uses_login
        assert spec.runtime == "python-3.12" and spec.owners == ["alice"]
        assert spec.tools == ["dapp-monitor"]
        if failing_deployment:
            raise DataOSError("test deployment failed")
        live[spec.name] = {"owner": "alice", "spec": {"service": {"envs": spec.env}}}
        path = tmp_path / f"publication-{len(deployed)}"
        shutil.copytree(spec.code, path)
        config = json.loads((path / "agent.json").read_text())
        deployed.append((spec.name, config))
        return {"url": f"https://test.invalid/{spec.name}/", "notes": []}

    gateway = SimpleNamespace(
        list_models=lambda tenant: [{"name": "small", "ready": True}, {"name": "starting", "ready": False}],
        list_tools=lambda tenant: [{"name": "dapp-monitor"}],
    )

    def run(config, chat, **details):
        views.append((config, chat, details))
        return {"answer": "Checked the logs.", "steps": [{"server": "dapp-monitor", "tool": "search_logs", "ok": True}]}

    monkeypatch.setattr(engine, "run_agent", run)
    identity = SimpleNamespace(identify=lambda token: Identity(
        {"oidc-access": "alice", "bob-token": "bob"}[token]
    ))
    app = create_app(heimdall=identity, login=FakeLogin(), gateway_factory=lambda s: gateway,
                     app_objects_factory=lambda: registry,
                     platform_factory=lambda s, t=None: SimpleNamespace(
                         client=SimpleNamespace(get_resource=resource), deploy=deploy))
    c = TestClient(app, base_url="https://testserver", root_path="/dapp")
    assert c.get("/dapp/agents", follow_redirects=False).status_code == 303
    c.get("/dapp/login", follow_redirects=False)
    c.get("/dapp/_dapp/callback?code=good-code&state=st1", follow_redirects=False)
    page = c.get("/dapp/agents")
    assert page.status_code == 200 and 'href="/dapp/agents" aria-current="page"' in page.text
    assert c.get("/dapp/assets/agent-chat.js").status_code == 200
    assert c.get("/dapp/assets/agent.css").status_code == 200
    csrf = re.search(r'const csrf = "([^"]+)";', page.text)[1]
    headers = {"X-CSRF-Token": csrf}

    url = "/dapp/api/agents"
    assert c.get(url).json()["models"] == ["gpt-6-luna", "small"]
    # The picker sees every model, grouped, and which hosted ones are still starting; /models alone is what it polls.
    assert c.get(url + "/models").json()["model_options"] == [
        {"name": "gpt-6-luna", "kind": "gateway", "ready": True},
        {"name": "small", "kind": "dataos", "ready": True, "description": ""},
        {"name": "starting", "kind": "dataos", "ready": False, "description": ""}]
    assert 'id="host-model"' in page.text and "/dapp/models" in page.text
    assert c.post(url, json=DRAFT).status_code == 403
    record = c.post(url, json=DRAFT, headers=headers).json()
    agent_url = url + "/" + record["id"]
    assert record["applied"] is None
    assert c.get(url).json()["agents"][0]["title"] == DRAFT["title"]
    assert TestClient(app, base_url="https://testserver", root_path="/dapp",
                      headers={"Authorization": "Bearer bob-token"}).get(agent_url).status_code == 404

    message = [{"role": "user", "content": "What failed?"}]
    preview = c.post(url + "/preview", headers=headers, json={"config": DRAFT, "messages": message})
    assert preview.json()["answer"] == "Checked the logs."
    assert views[0][2]["key"] == "preview-key" and views[0][2]["tool_token"] == "oidc-access"
    assert c.post(url + "/preview", headers=headers, json={
        "config": {**DRAFT, "tools": ["someone-elses-tool"]}, "messages": message,
    }).status_code == 403
    assert c.post(url + "/preview", headers=headers, json={
        "config": DRAFT, "messages": [{"role": "system", "content": "invalid"}],
    }).status_code == 422

    response = c.post(agent_url + "/publish", headers=headers, json={})
    assert response.status_code == 200, response.text
    first = response.json()
    assert first["agent"]["applied"] == 1
    assert deployed[0] == (first["agent"]["app"], {"version": 1, "config": DRAFT})

    bundle = tmp_path / "publication-0"
    monkeypatch.syspath_prepend(str(bundle))
    monkeypatch.setenv("ROOT_PATH", f"/{first['agent']['app']}")
    monkeypatch.setenv("OPENAI_BASE_URL", "http://gateway.test/openai/v1")
    spec = importlib.util.spec_from_file_location("agent_published_test", bundle / "published.py")
    actual = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(actual)
    published_app = TestClient(actual.app)
    assert published_app.get("/").status_code == 401
    assert "Version 1" in published_app.get("/", headers={"x-dapp-user": "alice"}).text
    assert published_app.get("/agent.css").status_code == 200
    assert published_app.get("/chat.js").status_code == 200
    assert published_app.get("/healthz").json() == {"ok": True, "version": 1}

    monkeypatch.setenv("DAPP_LLM_KEY", "published-key")
    monkeypatch.setattr(actual, "run_agent", run)
    assert published_app.post("/api/chat", json={"messages": message}).status_code == 401
    assert published_app.post("/api/chat", headers={"x-dapp-user": "alice"},
                              json={"messages": message}).json()["answer"] == "Checked the logs."
    assert views[-1][2]["key"] == "published-key" and "tool_token" not in views[-1][2]

    c.put(agent_url, headers=headers, json={**DRAFT, "instructions": "New draft"})
    failing_deployment = True
    assert c.post(agent_url + "/publish", headers=headers, json={}).status_code == 502
    assert c.get(agent_url).json()["applied"] == 1
    failing_deployment = False
    second = c.post(agent_url + "/publish", headers=headers, json={}).json()
    assert second["agent"]["applied"] == 2 and second["url"] == first["url"]
    rolled_back = c.post(agent_url + "/publish", headers=headers, json={"version": 1}).json()
    assert rolled_back["agent"]["applied"] == 1
    assert rolled_back["agent"]["draft"]["instructions"] == "New draft"
    assert deployed[-1] == deployed[0]


def test_engine_calls_only_selected_tools_and_reports_its_steps():
    calls = []

    def respond(request):
        payload = json.loads(request.content)
        path = request.url.path
        if path.startswith("/mcp/"):
            assert request.headers["Authorization"] == "Bearer dataos-login"
            assert path == "/mcp/engineering/dapp-monitor"
            method = payload["method"]
            if method == "notifications/initialized":
                return httpx.Response(202)
            if method == "initialize":
                result = {"protocolVersion": "2025-06-18"}
            elif method == "tools/list":
                result = {"tools": [{"name": "search_logs", "description": "Find logs"}]}
            else:
                calls.append(payload["params"]["name"])
                result = {"structuredContent": {"logs": ["ERROR upstream unavailable"]}}
            return httpx.Response(200, json={"jsonrpc": "2.0", "id": payload["id"], "result": result})

        assert path == "/openai/v1/chat/completions"
        assert request.headers["Authorization"] == "Bearer app-key"
        assert request.headers["x-dapp-user"] == "alice"
        assert payload["model"] == "gpt-6-luna" and payload["reasoning_effort"] == "none"
        if payload["messages"][-1]["role"] == "user":
            name = payload["tools"][0]["function"]["name"]
            message = {"tool_calls": [
                {"id": "real", "type": "function", "function": {"name": name, "arguments": "{}"}},
                {"id": "unapproved", "type": "function", "function": {"name": "delete_app", "arguments": "{}"}},
            ]}
        else:
            assert "ERROR upstream unavailable" in payload["messages"][-2]["content"]
            assert "not available" in payload["messages"][-1]["content"]
            message = {"content": "The upstream service was unavailable."}
        return httpx.Response(200, json={"choices": [{"message": message}]})

    result = run_agent(AgentConfig.model_validate(DRAFT), Chat(messages=[{"role": "user", "content": "Check logs"}]),
                       gateway="http://gateway.test", key="app-key", tenant="engineering", user="alice",
                       tool_token="dataos-login", transport=httpx.MockTransport(respond))
    assert result["answer"] == "The upstream service was unavailable."
    assert [step["ok"] for step in result["steps"]] == [True, False]
    assert calls == ["search_logs"]


def test_action_is_never_replayed_after_an_uncertain_gateway_error():
    counts = {"calls": 0}

    def respond(request):
        payload = json.loads(request.content)
        if payload["method"] == "initialize":
            return httpx.Response(200, json={"id": payload["id"], "result": {"protocolVersion": "2025-06-18"}})
        if payload["method"] == "notifications/initialized":
            return httpx.Response(202)
        counts["calls"] += 1
        return httpx.Response(502, text="upstream disconnected")

    import pytest

    from dapp.agent_runtime.mcp_transport import Server, ToolError

    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        server = Server("demo", "http://gateway.test/mcp/engineering/demo", "key", client)
        with pytest.raises(ToolError, match="HTTP 502"):
            server.call("tools/call", {"name": "change_state", "arguments": {}}, "alice")
        assert counts["calls"] == 1
