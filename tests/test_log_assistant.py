"""examples/log-assistant: the Logs page's data and the AI chat, with the gateway tool and the model faked."""
import importlib
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

APP = Path(__file__).resolve().parents[1] / "examples" / "log-assistant" / "app"


class FakeServer:
    def __init__(self):
        self.calls = []

    def call(self, method, params, user):
        self.calls.append((method, params, user))
        name, args = params["name"], params["arguments"]
        data = {
            "list_resources": [{"name": "chatbot", "kind": args.get("kind", "app"), "status": "active", "description": "bot"}],
            "status": {"name": args.get("name"), "state": "active", "runtime": "app-service:running:1",
                       "pods": [{"pod": "chatbot-pod-1", "phase": "running", "dataplane": "poldp01"},
                                {"pod": "chatbotapp-0", "phase": "running", "dataplane": "hub"}]},
            "recent_logs": {"containers": [{"pod": "chatbot-pod-1", "container": "chtbtsvc", "lines": ["GET / 200", "GET /x 500"]}]},
            "search_logs": {"total": 3, "returned": 1, "container": "chtbtsvc", "query": {"q": args.get("query")},
                            "lines": [{"time": "2026-11-26T10:00:00Z", "pod": "chatbot-pod-1", "container": "chtbtsvc",
                                       "stream": "stderr", "message": "ERROR boom"}]},
        }
        if name == "boom":
            return {"isError": True, "content": [{"type": "text", "text": "no app nope"}]}
        return {"content": [{"type": "text", "text": json.dumps(data[name])}], "structuredContent": None}


class FakeToolbox:
    def __init__(self):
        self.servers = {"dapp-monitor": FakeServer()}
        self.problems = []
        self.ran = []

    def openai_tools(self, user):
        return [{"type": "function", "function": {"name": f"dapp_monitor__{n}", "description": n, "parameters": {}}}
                for n in ("search_logs", "log_summary", "status")] + [
            {"type": "function", "function": {"name": "other__x", "description": "", "parameters": {}}}]

    def instructions(self):
        return "[dapp-monitor]\nwatch apps"

    def run(self, fn, arguments, user):
        self.ran.append((fn, json.loads(arguments), user))
        return json.dumps({"total": 3, "returned": 1, "lines": [{"message": "ERROR boom"}]})


class FakeOpenAI:
    """First asks for search_logs, then answers."""

    def __init__(self):
        self.requests = []
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self.create))

    def create(self, **kw):
        self.requests.append(kw)
        if len(self.requests) == 1:
            tc = SimpleNamespace(id="c1", function=SimpleNamespace(name="dapp_monitor__search_logs",
                                                                   arguments='{"name": "chatbot", "query": "error", "since": "24h"}'),
                                 model_dump=lambda: {"id": "c1", "type": "function", "function": {
                                     "name": "dapp_monitor__search_logs", "arguments": "{}"}})
            return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content="", tool_calls=[tc]))])
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(
            content="Haan: 3 errors, jaise `ERROR boom` (pod chatbot-pod-1).", tool_calls=None))])


@pytest.fixture
def la(monkeypatch):
    monkeypatch.setenv("ROOT_PATH", "/log-assistant")
    monkeypatch.setenv("DAPP_LLM_KEY", "app-key")
    sys.path.insert(0, str(APP))
    for m in ("app", "mcp_tools"):
        sys.modules.pop(m, None)
    mod = importlib.import_module("app")
    tb, ai = FakeToolbox(), FakeOpenAI()
    monkeypatch.setattr(mod, "TOOLS", tb)
    monkeypatch.setattr(mod, "_client", ai)
    c = TestClient(mod.app, headers={"x-dapp-user": "alice"})
    yield mod, c, tb, ai
    sys.path.remove(str(APP))
    sys.modules.pop("app", None)


def test_the_page_and_its_data(la):
    _, c, tb, _ = la
    page = c.get("/")
    assert page.status_code == 200 and 'const ROOT = "/log-assistant"' in page.text and 'id="aibtn"' in page.text
    assert c.get("/api/resources", params={"kind": "service"}).json()[0]["kind"] == "service"
    st = c.get("/api/status", params={"name": "chatbot"}).json()
    assert st["state"] == "active" and len(st["pods"]) == 2
    live = c.get("/api/logs", params={"name": "chatbot", "mode": "live"}).json()
    assert live["mode"] == "live" and [l["message"] for l in live["lines"]] == ["GET / 200", "GET /x 500"]
    assert tb.servers["dapp-monitor"].calls[-1][1] == {"name": "recent_logs", "arguments": {
        "name": "chatbot", "kind": "app", "tail": 300}}
    hist = c.get("/api/logs", params={"name": "chatbot", "q": "error", "since": "24h"}).json()
    assert hist["total"] == 3 and hist["container"] == "chtbtsvc" and hist["lines"][0]["message"] == "ERROR boom"
    _, params, user = tb.servers["dapp-monitor"].calls[-1]
    assert params == {"name": "search_logs", "arguments": {"name": "chatbot", "kind": "app", "query": "error",
                                                         "since": "24h", "limit": 300}} and user == "alice"
    rng = c.get("/api/logs", params={"name": "chatbot", "start": "2026-11-26T16:00:00Z", "end": "2026-11-26T17:00:00Z"})
    args = tb.servers["dapp-monitor"].calls[-1][1]["arguments"]
    assert rng.status_code == 200 and args["start"] == "2026-11-26T16:00:00Z" and "since" not in args


def test_tool_errors_reach_the_page(la, monkeypatch):
    _, c, tb, _ = la
    real = tb.servers["dapp-monitor"].call
    monkeypatch.setattr(tb.servers["dapp-monitor"], "call", lambda m, p, u: real(m, {**p, "name": "boom"}, u))
    r = c.get("/api/status", params={"name": "nope"})
    assert r.status_code == 502 and r.json()["detail"] == "no app nope"


def test_the_chat_uses_the_tool_and_knows_the_page(la):
    _, c, tb, ai = la
    r = c.post("/api/chat", json={"messages": [{"role": "user", "content": "kal koi error aaya?"}],
                                  "context": {"name": "chatbot", "kind": "app", "mode": "historical", "since": "24h",
                                              "q": "error"}})
    d = r.json()
    assert r.status_code == 200 and d["answer"].startswith("Haan: 3 errors") and d["model"] == "gpt-6-luna"
    assert d["steps"] == [{"tool": "search_logs", "args": {"name": "chatbot", "query": "error", "since": "24h"},
                           "result": "3 matching lines, showing 1"}]
    assert tb.ran == [("dapp_monitor__search_logs", {"name": "chatbot", "query": "error", "since": "24h"}, "alice")]
    first = ai.requests[0]
    system = first["messages"][0]["content"]
    assert "historical logs of app 'chatbot'" in system and "filtered by 'error'" in system and "the last 24h" in system
    assert "Now it is " in system and "[dapp-monitor]" in system
    assert [t["function"]["name"] for t in first["tools"]] == [
        "dapp_monitor__search_logs", "dapp_monitor__log_summary", "dapp_monitor__status"]   # only the monitor's functions
    assert first["extra_body"] == {"reasoning_effort": "none"} and first["extra_headers"] == {"x-dapp-user": "alice"}
    assert ai.requests[1]["messages"][-1]["role"] == "tool"


def test_gists_say_what_a_tool_found(la):
    mod, *_ = la
    assert mod._gist(json.dumps({"total": 5, "returned": 5, "lines": []})) == "5 matching lines"
    assert mod._gist(json.dumps({"total_lines": 9, "matching": {"error": 1, "warn": 2, "exception": 0}})) == \
        "9 lines: 1 error, 2 warn, 0 exception"
    assert mod._gist(json.dumps({"state": "active", "runtime": "r", "pods": [1, 2]})) == "active · r · 2 pods"
    assert mod._gist("tool error: nope") == "tool error: nope"
    cut = json.dumps({"total": 75, "returned": 75, "lines": [{"message": "x" * 50}] * 400}, indent=2)[:12000] + "\n…[cut]"
    assert mod._gist(cut) == "75 matching lines"                       # a result the toolbox cut short


def test_a_used_up_budget_is_said_plainly(la, monkeypatch):
    import httpx
    from openai import RateLimitError
    _mod, c, _, ai = la
    msg = "alice has used their monthly token budget in app log-assistant (1,200 of 1,000 tokens); it resets on 2026-12-01 (UTC)."
    body = {"error": {"type": "budget_exceeded", "message": msg}}
    resp = httpx.Response(429, json=body, request=httpx.Request("POST", "https://gw/openai/v1/chat/completions"))

    def refuse(**kw):
        raise RateLimitError("Error code: 429", response=resp, body=body)
    monkeypatch.setattr(ai.chat.completions, "create", refuse)
    r = c.post("/api/chat", json={"messages": [{"role": "user", "content": "hi"}]}).json()
    assert r["answer"] == f"Token budget used up. {msg}" and r["error"] == "budget_exceeded"
