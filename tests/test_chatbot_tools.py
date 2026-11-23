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
