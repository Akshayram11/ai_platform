"""The chatbot example's MCP client and tool loop, without OpenAI or a cluster."""

import json
import sys
import types
from pathlib import Path
from types import SimpleNamespace as NS

import httpx
import pytest

CHATBOT = Path(__file__).resolve().parents[1] / "examples" / "chatbot"


def mcp_server(seen: list):
    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(req)
        msg = json.loads(req.content)
        if "id" not in msg:
            return httpx.Response(202)
        if req.headers.get("authorization") != "Bearer dapp_appkey":
            return httpx.Response(401, json={"error": "no"})
        if msg["method"] == "initialize":
            return httpx.Response(200, headers={"mcp-session-id": "s1"}, json={
                "jsonrpc": "2.0", "id": msg["id"],
                "result": {"protocolVersion": "2025-06-18", "instructions": "Use search first."}})
        if msg["method"] == "tools/list":
            return httpx.Response(200, json={"jsonrpc": "2.0", "id": msg["id"], "result": {"tools": [
                {"name": "search", "description": "find data products",
                 "inputSchema": {"type": "object", "properties": {"q": {"type": "string"}}}}]}})
        args = msg["params"]["arguments"]
        reply = {"jsonrpc": "2.0", "id": msg["id"],
                 "result": {"content": [{"type": "text", "text": f"found: sales ({args['q']})"}]}}
        return httpx.Response(200, headers={"content-type": "text/event-stream"},
                              content=f"data: {json.dumps(reply)}\n\n".encode())
    return handler


@pytest.fixture
def chatbot(monkeypatch):
    monkeypatch.syspath_prepend(str(CHATBOT))
    sys.modules.pop("mcp_tools", None)
    import mcp_tools
    return mcp_tools


def test_toolbox_lists_and_calls_through_the_gateway(chatbot):
    seen: list[httpx.Request] = []
    box = chatbot.Toolbox(["dataproduct-mcp"], "http://dapp-gateway:8080/mcp/engineering", "dapp_appkey",
                          transport=httpx.MockTransport(mcp_server(seen)))
    specs = box.openai_tools("carol")
    assert [s["function"]["name"] for s in specs] == ["dataproduct_mcp__search"]
    assert "Use search first." in box.instructions()
    out = box.run("dataproduct_mcp__search", '{"q": "revenue"}', "carol")
    assert out == "found: sales (revenue)"                                   # SSE reply unwrapped
    assert seen[-1].url.path == "/mcp/engineering/dataproduct-mcp"
    assert seen[-1].headers["x-dapp-user"] == "carol" and seen[-1].headers["mcp-session-id"] == "s1"
    box.run("dataproduct_mcp__search", '{"q": "cost"}', "carol")
    assert sum(json.loads(r.content).get("method") == "initialize" for r in seen) == 1   # one handshake, not per call
    assert box.run("nope__x", "{}", "carol").startswith("unknown tool")
    assert box.run("dataproduct_mcp__search", "{bad", "carol") == "the arguments were not valid JSON"


def test_toolbox_reports_a_gateway_refusal(chatbot):
    box = chatbot.Toolbox(["dataproduct-mcp"], "http://gw/mcp/engineering", "dapp_wrong",
                          transport=httpx.MockTransport(mcp_server([])))
    assert box.openai_tools("carol") == [] and "HTTP 401" in box.problems[0]


def test_chat_loop_runs_tools_then_answers(chatbot, monkeypatch):
    seen: list[httpx.Request] = []
    for name in ("openai",):  # the example's own dependency; not installed for the platform's tests
        fake = types.ModuleType(name)
        fake.OpenAI = object
        monkeypatch.setitem(sys.modules, name, fake)
    monkeypatch.setenv("DAPP_TOOLS", "dataproduct-mcp")
    monkeypatch.setenv("DAPP_TOOLS_URL", "http://gw/mcp/engineering")
    monkeypatch.setenv("DAPP_LLM_KEY", "dapp_appkey")
    monkeypatch.delenv("DAPP_AUTH", raising=False)
    monkeypatch.setenv("DAPP_METRICS", "0")
    sys.modules.pop("app", None)
    import app as bot
    bot.TOOLS = chatbot.Toolbox(["dataproduct-mcp"], "http://gw/mcp/engineering", "dapp_appkey",
                                transport=httpx.MockTransport(mcp_server(seen)))

    sent = []
    call = NS(id="c1", function=NS(name="dataproduct_mcp__search", arguments='{"q": "revenue"}'),
              model_dump=lambda: {"id": "c1", "type": "function",
                                  "function": {"name": "dataproduct_mcp__search", "arguments": '{"q": "revenue"}'}})
    replies = iter([NS(tool_calls=[call], content=None), NS(tool_calls=None, content="Revenue lives in sales.")])

    class FakeClient:
        class chat:
            class completions:
                @staticmethod
                def create(**kw):
                    sent.append(kw)
                    return NS(choices=[NS(message=next(replies))])

    extra = {"extra_body": {"reasoning_effort": "low"}}
    out = "".join(bot.with_tools(FakeClient(), [{"role": "user", "content": "where is revenue?"}], extra, "carol"))
    assert sent and all(k["extra_body"]["reasoning_effort"] == "none" for k in sent)   # luna: no reasoning + tools
    assert extra["extra_body"]["reasoning_effort"] == "low"                       # the caller's dict untouched
    assert out == '› search({"q": "revenue"})\nRevenue lives in sales.'
    assert sent[0]["tools"][0]["function"]["name"] == "dataproduct_mcp__search"
    assert "Use search first." in sent[0]["messages"][0]["content"]              # the server's own guidance
    assert sent[1]["messages"][-1] == {"role": "tool", "tool_call_id": "c1", "content": "found: sales (revenue)"}


def test_spec_tools_need_llm_and_reach_the_env():
    from dapp.config import Context
    from dapp.platform import Platform
    from dapp.spec import AppSpec

    base = {"name": "data-bot", "description": "d", "image": "i:1", "command": ["x"], "compute": "c"}
    with pytest.raises(ValueError, match="needs llm: true"):
        AppSpec(**base, tools=["dataproduct-mcp"])
    with pytest.raises(ValueError, match="lower-case"):
        AppSpec(**base, llm=True, tools=["Bad Tool"])
    p = Platform.__new__(Platform)
    p.ctx = Context("t", "x.dataos.cloud", "engineering", "alice", "k")
    envs = p.plan(AppSpec(**base, llm=True, public=True, tools=["dataproduct-mcp"])).manifests[0][
        "spec"]["service"]["envs"]
    assert envs["DAPP_TOOLS"] == "dataproduct-mcp"
    assert envs["DAPP_TOOLS_URL"] == "http://dapp-gateway:8080/mcp/engineering"


def test_model_switch(chatbot, monkeypatch):
    """The page may pick another listed model; it gets a plain chat (no reasoning setting, no tools)."""
    fake = types.ModuleType("openai")
    fake.OpenAI = object
    monkeypatch.setitem(sys.modules, "openai", fake)    # the example's own dependency; not installed here
    monkeypatch.delenv("DAPP_AUTH", raising=False)
    monkeypatch.delenv("DAPP_TOOLS", raising=False)
    monkeypatch.setenv("DAPP_METRICS", "0")
    sys.modules.pop("app", None)
    import app as chatbot
    calls = []

    class Fake:
        class chat:
            class completions:
                @staticmethod
                def create(**kw):
                    calls.append(kw)
                    return iter([])

    monkeypatch.setattr(chatbot, "MODELS", [chatbot.MODEL, "small"])
    monkeypatch.setattr(chatbot, "REASONING_EFFORT", "low")
    monkeypatch.setattr(chatbot, "TOOLS", None)
    monkeypatch.setattr(chatbot, "_client", Fake())
    from fastapi.testclient import TestClient
    c = TestClient(chatbot.app)
    assert c.get("/api/models").json() == {"default": chatbot.MODEL, "models": [chatbot.MODEL, "small"]}
    c.post("/api/chat", json={"messages": [{"role": "user", "content": "hi"}], "model": "small"})
    assert calls[-1]["model"] == "small" and "extra_body" not in calls[-1]
    c.post("/api/chat", json={"messages": [{"role": "user", "content": "hi"}]})
    assert calls[-1]["model"] == chatbot.MODEL and calls[-1]["extra_body"] == {"reasoning_effort": "low"}
    assert c.post("/api/chat", json={"messages": [], "model": "gpt-5-pro"}).status_code == 400


class FakeObjects:
    """What dapp_objects.objects does, on in-memory SQLite, one database per (name, person)."""

    def __init__(self):
        import sqlite3
        self.sqlite3, self.dbs, self.shared = sqlite3, {}, []

    def open(self, name, for_user=None):
        db = self.dbs.setdefault((name, for_user), self.sqlite3.connect(":memory:", check_same_thread=False))
        shared = self.shared

        class Obj:
            def sql(self, query, params=None):
                cur = db.execute(query, params or [])
                cols = [d[0] for d in cur.description or []]
                rows = [dict(zip(cols, r, strict=True)) for r in cur.fetchall()]
                db.commit()
                return rows

            def share(self, who, mode="read"):
                shared.append((name, for_user, who))
        return Obj()


def history_bot(monkeypatch, objs):
    fake = types.ModuleType("openai")
    fake.OpenAI = object
    monkeypatch.setitem(sys.modules, "openai", fake)
    for v in ("DAPP_AUTH", "DAPP_TOOLS", "DAPP_LLM_KEY"):
        monkeypatch.delenv(v, raising=False)
    monkeypatch.setenv("DAPP_METRICS", "0")
    sys.modules.pop("app", None)
    import app as bot

    class Fake:
        class chat:
            class completions:
                @staticmethod
                def create(**kw):
                    return iter([NS(choices=[NS(delta=NS(content="Hi!"))])])

    monkeypatch.setattr(bot, "objects", objs)
    monkeypatch.setattr(bot, "HISTORY", objs is not None)
    monkeypatch.setattr(bot, "TOOLS", None)
    monkeypatch.setattr(bot, "_client", Fake())
    bot._ready.clear()
    from fastapi.testclient import TestClient
    return TestClient(bot.app)


def test_history_is_saved_per_person(chatbot, monkeypatch):
    objs = FakeObjects()
    c = history_bot(monkeypatch, objs)
    carol = {"x-dapp-user": "carol"}
    first = [{"role": "user", "content": "hello  there"}]
    r = c.post("/api/chat", json={"messages": first}, headers=carol)
    cid = r.headers["x-chat-id"]
    assert r.text == "Hi!"
    again = [*first, {"role": "assistant", "content": "Hi!"}, {"role": "user", "content": "again"}]
    assert c.post("/api/chat", json={"messages": again, "chat_id": cid}, headers=carol).headers["x-chat-id"] == cid

    listed = c.get("/api/history", headers=carol).json()
    assert listed["enabled"] and [x["title"] for x in listed["chats"]] == ["hello there"]   # one chat, titled by its start
    got = c.get(f"/api/history/{cid}", headers=carol).json()["messages"]
    assert [(m["role"], m["content"]) for m in got] == [
        ("user", "hello  there"), ("assistant", "Hi!"), ("user", "again"), ("assistant", "Hi!")]
    assert c.get("/api/history", headers={"x-dapp-user": "dave"}).json()["chats"] == []      # nobody else's
    assert ("chat", "carol", "carol") in objs.shared                    # shared back, so carol can open it in Storage
    assert c.delete(f"/api/history/{cid}", headers=carol).json() == {"deleted": cid}
    assert c.get("/api/history", headers=carol).json()["chats"] == []


def test_chat_still_works_when_history_cannot_be_saved(chatbot, monkeypatch):
    class Broken:
        def open(self, *a, **k):
            from dapp_objects import ObjectsError
            raise ObjectsError("immortal-obj: HTTP 502")

    c = history_bot(monkeypatch, Broken())
    r = c.post("/api/chat", json={"messages": [{"role": "user", "content": "hi"}]}, headers={"x-dapp-user": "carol"})
    assert r.status_code == 200 and r.text == "Hi!" and "x-chat-id" not in r.headers
    assert c.get("/api/history", headers={"x-dapp-user": "carol"}).json()["enabled"] is False
    off = history_bot(monkeypatch, None)
    assert off.get("/api/history").json() == {"enabled": False, "chats": []}
    assert off.get("/api/history/x").status_code == 404
