"""Watching apps: dapp.logsearch (stored logs over any time range) and the dapp-monitor MCP server behind the gateway."""
import asyncio
import importlib
import json
import sys
from pathlib import Path

import httpx
import pytest
from starlette.testclient import TestClient

from dapp import logsearch
from dapp.logsearch import LogSearchError, build_params, search, shape, to_epoch

ROOT = Path(__file__).resolve().parents[1]
NOW = 1_795_697_600                                 # 2026-11-26T12:53:20Z


def test_the_server_ships_the_same_logsearch():
    assert (ROOT / "src/dapp/logsearch.py").read_text() == (ROOT / "apps/dapp-monitor/app/logsearch.py").read_text()


def test_times_are_read_the_way_people_write_them():
    assert to_epoch("2026-11-26T12:53:20Z") == NOW
    assert to_epoch("2026-11-26 12:53:20") == NOW                          # no zone: UTC
    assert to_epoch("2026-11-26T18:23:20+05:30") == NOW
    assert to_epoch(str(NOW)) == NOW and to_epoch(NOW) == NOW
    assert to_epoch("2h ago", now=NOW) == NOW - 7200 and to_epoch("30 minutes ago", now=NOW) == NOW - 1800
    assert to_epoch("now", now=NOW) == NOW
    with pytest.raises(LogSearchError, match="can't read the time"):
        to_epoch("yesterday-ish")


def test_query_parameters_are_checked():
    assert build_params() == {"q": "*", "window": "1h", "limit": 100, "offset": 0}
    p = build_params(query="error", start="2h ago", end="1h ago", stream="stderr", pod="p-0", limit=5, now=NOW)
    assert p == {"q": "error", "start_timestamp": NOW - 7200, "end_timestamp": NOW - 3600, "stream": "stderr", "cgroup": "p-0",
                 "limit": 5, "offset": 0}
    assert build_params(start="2h ago", now=NOW)["end_timestamp"] == NOW
    for bad, msg in [({"since": "1h", "start": "2h ago"}, "not both"), ({"since": "a week"}, "number and a unit"),
                     ({"start": "1h ago", "end": "2h ago"}, "after start"), ({"since": "40d"}, "31 days"),
                     ({"stream": "both"}, "stdout or stderr"), ({"limit": 5000}, "1 to 1000")]:
        with pytest.raises(LogSearchError, match=msg):
            build_params(now=NOW, **bad)


def test_credentials_in_log_lines_are_masked():
    from dapp.logsearch import redact
    assert redact("GET /cb?code=abc&state=def&x=1") == "GET /cb?code=•••&state=•••&x=1"
    assert redact("Authorization: Bearer abcdefghij123") == "Authorization: Bearer •••"
    assert redact('{"password": "hunter2"}') == '{"password": "•••"}'
    assert redact("eyJhbGciOiJIUzI1.eyJzdWIiOiIxMjM0.SflKxwRJSMeKKF2QT4") == "•••jwt•••"
    assert redact("error 500 in handler") == "error 500 in handler"


def test_both_answer_shapes_become_lines_oldest_first():
    v2 = {"data": {"total_hits": 2, "offset": 0, "limit": 100, "results": [
        {"name": "pod-b", "containerName": "app", "time": "2026-11-26T10:00:02Z", "stream": "stderr", "message": "boom"},
        {"name": "pod-a", "containerName": "app", "time": "2026-11-26T10:00:01Z", "stream": "stdout", "message": "ok"}]}}
    out = shape(v2)
    assert out["total"] == 2 and [x["message"] for x in out["lines"]] == ["ok", "boom"]
    v1 = {"data": {"total_hits": 1, "results": {"pod-a/app": {"name": "pod-a", "containerName": "app",
                                                              "logs": [{"time": "t", "stream": "stdout", "message": "x" * 9000}]}}}}
    line = shape(v1)["lines"][0]
    assert line["pod"] == "pod-a" and len(line["message"]) == logsearch.MAX_MESSAGE


def test_search_calls_platform_api_as_the_caller():
    seen = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(req)
        if "missing" in req.url.path:
            return httpx.Response(404, json={})
        if "secret" in req.url.path:
            return httpx.Response(403, json={})
        return httpx.Response(200, json={"data": {"total_hits": 0, "results": []}})

    c = httpx.Client(transport=httpx.MockTransport(handler))
    out = search("https://x.dataos.cloud", "engineering", {"apikey": "k"}, "chatbot", client=c, query="error", since="30m")
    r = seen[-1]
    assert r.url.path == "/platform-api/v2/tenants/engineering/resources/app/v1alpha/chatbot/logs/search"
    assert dict(r.url.params) == {"q": "error", "window": "30m", "limit": "100", "offset": "0"}
    assert r.headers["apikey"] == "k" and out["resource"] == "app:v1alpha:chatbot" and out["total"] == 0
    search("https://x", "t", {}, "flow", kind="nilus", client=c)
    assert "/resources/nilus/v1alpha/flow/" in seen[-1].url.path
    with pytest.raises(LogSearchError, match="no app missing"):
        search("https://x", "t", {}, "missing", client=c)
    with pytest.raises(LogSearchError, match="no permission"):
        search("https://x", "t", {}, "secret", client=c)
    with pytest.raises(LogSearchError, match="not a resource name"):
        search("https://x", "t", {}, "../etc", client=c)


# ------------------------------------------------------------------ the MCP server
@pytest.fixture
def server(monkeypatch):
    monkeypatch.setenv("TOOL_TOKEN", "tool-secret")
    monkeypatch.setenv("DATAOS_FQDN", "x.dataos.cloud")
    monkeypatch.setenv("DATAOS_TENANT_ID", "engineering")
    monkeypatch.setenv("DATAOS_RUN_AS_USER", "alice")
    monkeypatch.setenv("DATAOS_RUN_AS_APIKEY", "owner-key")
    monkeypatch.setenv("MONITOR_ALLOW", "*")
    monkeypatch.delenv("LOGS_APPS", raising=False)
    sys.path.insert(0, str(ROOT / "apps" / "dapp-monitor" / "app"))
    for m in ("server", "logsearch"):
        sys.modules.pop(m, None)
    mod = importlib.import_module("server")
    calls = []

    resources = {("app", "chatbot"): "alice", ("app", "dapp"): "alice", ("service", "orders-api"): "alice",
                 ("service", "their-svc"): "bob", ("lakehouse", "lake1"): "rohit"}

    def poros(req: httpx.Request) -> httpx.Response:
        calls.append(req)
        assert req.headers["apikey"] == "owner-key"
        p = req.url.path
        if "/logs/search" in p:
            q = req.url.params["q"]
            rows = [{"name": "pod-1", "containerName": "main", "time": "2026-11-26T10:00:00Z", "stream": "stderr",
                     "message": f"line for {q}"}]
            if "container" not in req.url.params:                   # no filter: the log shipper's lines come too
                rows.append({"name": "pod-1", "containerName": "main-0-sc", "time": "2026-11-26T10:00:01Z", "stream": "stderr",
                             "message": "vector noise"})
            return httpx.Response(200, json={"data": {"total_hits": {"*": 40, "error": 3}.get(q, 0), "results": rows}})
        parts = p.split("/resources/", 1)[1].split("/")
        kind = parts[0]
        if len(parts) == 1:                                               # a list, ?owner=
            owner = req.url.params.get("owner")
            return httpx.Response(200, json={"resources": [{"name": n, "owner": o, "status": "active"}
                                                           for (k, n), o in resources.items()
                                                           if k == kind and (not owner or o == owner)]})
        name = parts[1]
        if (kind, name) not in resources:
            return httpx.Response(404, json={})
        if len(parts) == 2:
            return httpx.Response(200, json={"name": name, "owner": resources[(kind, name)], "status": {
                "aggregateStatus": "active", "runtimeState": {"status": f"{kind}-service:running:1"}}})
        if parts[2] == "runtime" and kind == "lakehouse":            # several containers: no single filter
            return httpx.Response(200, json={"nodeDetails": [
                {"name": "lake-ms", "phase": "running", "dataplane": "poldp01", "containers": ["ms"]},
                {"name": "lake-sc", "phase": "running", "dataplane": "poldp01", "containers": ["sc"]}]})
        if parts[2] == "runtime":
            return httpx.Response(200, json={"nodeDetails": [
                {"name": "pod-1", "phase": "running", "type": "cgroup-stateless", "dataplane": "poldp01", "containers": ["main"],
                 "sideCars": ["main-0-sc"]},
                {"name": f"{name}app-0", "phase": "running", "type": "cgroup-stateful", "dataplane": "hub",
                 "containers": [f"{name}app", "tunnel-access"]}]})
        if parts[2] == "log":
            return httpx.Response(200, json={"nodeLogs": [{"name": "pod-1", "logs": [
                {"name": "main", "log": ["a", "b", "GET /cb?code=abc123&state=xyz HTTP/1.1"]},
                {"name": "main-0-sc", "log": ["vector noise"]}]}]})
        return httpx.Response(404, json={})

    transport = httpx.MockTransport(poros)
    real_get, real_client = httpx.get, httpx.Client
    monkeypatch.setattr(mod.httpx, "get", lambda url, **kw: real_client(transport=transport).get(url, **kw))
    monkeypatch.setattr(mod, "search", lambda *a, **kw: logsearch.search(*a, client=real_client(transport=transport), **kw))
    return mod, calls, real_get


def call(mod, tool, /, **args):
    return asyncio.run(mod.mcp.call_tool(tool, args))


def payload(result):
    """The tool's return value, from whatever call_tool gives back in this mcp version."""
    content = result[0] if isinstance(result, tuple) else result
    if isinstance(content, dict):
        return content.get("result", content)
    if hasattr(content, "structured_content") and content.structured_content is not None:
        sc = content.structured_content
        return sc.get("result", sc)
    items = getattr(content, "content", content)
    return json.loads(items[0].text)


def test_tools_read_as_the_owner_and_stay_inside_their_apps(server, monkeypatch):
    mod, calls, _ = server
    assert [a["name"] for a in payload(call(mod, "list_apps"))] == ["chatbot", "dapp"]
    st = payload(call(mod, "app_status", app="chatbot"))
    assert st["state"] == "active" and st["runtime"] == "app-service:running:1" and st["pods"][0]["pod"] == "pod-1"
    recent = payload(call(mod, "recent_logs", name="chatbot", tail=2))
    assert recent["containers"] == [{"pod": "pod-1", "container": "main",                       # no sidecar, masked
                                     "lines": ["b", "GET /cb?code=•••&state=••• HTTP/1.1"]}]
    assert calls[-1].url.path.endswith("/resources/app/chatbot/log") and dict(calls[-1].url.params) == {"tailLines": "2"}
    found = payload(call(mod, "search_logs", name="chatbot", query="error", since="30m"))
    assert found["total"] == 3 and found["lines"][0]["message"] == "line for error" and found["container"] == "main"
    assert calls[-1].url.params["container"] == "main"                    # the app's own container, not the shipper
    everything = payload(call(mod, "search_logs", name="chatbot", include_sidecars=True))
    assert everything["container"] == "all" and "container" not in calls[-1].url.params
    summary = payload(call(mod, "log_summary", name="chatbot", since="1h"))
    assert summary["total_lines"] == 40 and summary["matching"]["error"] == 3 and summary["pods_in_first_1000_lines"] == {
        "pod-1": 1}
    with pytest.raises(Exception, match="no app someone-elses"):
        call(mod, "search_logs", name="someone-elses")
    with pytest.raises(Exception, match="kind is one of"):
        call(mod, "status", name="chatbot", kind="depot")                  # a depot runs nothing: no logs


def test_services_too(server, monkeypatch):
    mod, calls, _ = server
    assert [r["name"] for r in payload(call(mod, "list_resources", kind="service"))] == ["orders-api"]   # only the owner's
    st = payload(call(mod, "status", name="orders-api", kind="service"))
    assert st["kind"] == "service" and st["runtime"] == "service-service:running:1"
    assert calls[-1].url.path.endswith("/resources/service/orders-api/runtime")
    recent = payload(call(mod, "recent_logs", name="orders-api", kind="service", tail=1))
    assert recent["containers"][0]["lines"] == ["GET /cb?code=•••&state=••• HTTP/1.1"]
    assert calls[-1].url.path.endswith("/resources/service/orders-api/log")
    payload(call(mod, "search_logs", name="orders-api", kind="service", query="error"))
    assert "/resources/service/v2alpha/orders-api/logs/search" in calls[-1].url.path
    with pytest.raises(Exception, match="not a resource of alice"):
        call(mod, "recent_logs", name="their-svc", kind="service")          # bob's service: not with "*"
    monkeypatch.setenv("MONITOR_ALLOW", "dapp, service:their-svc")         # listed by name: allowed if DataOS lets us read
    assert [r["name"] for r in payload(call(mod, "list_resources", kind="service"))] == ["their-svc"]
    assert payload(call(mod, "recent_logs", name="their-svc", kind="service"))["containers"]
    assert [a["name"] for a in payload(call(mod, "list_apps"))] == ["dapp"]
    with pytest.raises(Exception, match="not one of the resources this server may watch: app:dapp, service:their-svc"):
        call(mod, "recent_logs", name="chatbot")


def test_tenant_mode_the_owner_sees_all_others_only_their_own(server, monkeypatch):
    mod, calls, _ = server
    monkeypatch.setenv("MONITOR_ALLOW", "tenant")
    assert [r["name"] for r in payload(call(mod, "list_resources", kind="service"))] == ["orders-api", "their-svc"]
    assert payload(call(mod, "recent_logs", name="their-svc", kind="service"))["containers"]     # bob's, read by the owner
    lake = payload(call(mod, "search_logs", name="lake1", kind="lakehouse", query="error"))
    assert "/resources/lakehouse/v1alpha/lake1/logs/search" in calls[-1].url.path
    assert [x["container"] for x in lake["lines"]] == ["main"] and "log shipper left out" in lake["note"]
    assert payload(call(mod, "status", name="lake1", kind="lakehouse"))["owner"] == "rohit"

    token = mod._viewer.set("bob")                                         # bob opens the Log Assistant
    try:
        assert [r["name"] for r in payload(call(mod, "list_resources", kind="service"))] == ["their-svc"]
        assert payload(call(mod, "list_resources", kind="lakehouse")) == []
        with pytest.raises(Exception, match="belongs to alice; you can watch your own"):
            call(mod, "recent_logs", name="orders-api", kind="service")
        with pytest.raises(Exception, match="belongs to rohit"):
            call(mod, "search_logs", name="lake1", kind="lakehouse")      # the owner's admin rights are not lent to bob
        monkeypatch.setenv("MONITOR_SHARE", "service:orders-api")
        assert payload(call(mod, "recent_logs", name="orders-api", kind="service"))["containers"]
        assert [r["name"] for r in payload(call(mod, "list_resources", kind="service"))] == ["their-svc", "orders-api"]
    finally:
        mod._viewer.reset(token)


def test_only_the_gateway_gets_in(server):
    mod, _, _ = server
    with TestClient(mod.app, base_url="http://dapp-monitor:8080") as c:
        assert c.get("/healthz").json() == {"ok": True}
        assert c.post("/mcp", json={}).status_code == 401
        assert c.post("/mcp", json={}, headers={"authorization": "Bearer wrong"}).status_code == 401
        init = {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
            "protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "t", "version": "1"}}}
        r = c.post("/mcp", json=init, headers={"authorization": "Bearer tool-secret", "x-dapp-caller": "user:alice",
                                                "accept": "application/json, text/event-stream"})
        assert r.status_code == 200 and r.json()["result"]["serverInfo"]["name"] == "dapp-monitor"


def test_dapp_cli_and_stdio_mcp_search(monkeypatch):
    from typer.testing import CliRunner

    from dapp import cli
    from dapp.cli import app as cli_app
    from dapp.mcp_server import build_server

    class P:
        def search_logs(self, name, **kw):
            assert name == "chatbot" and kw["query"] == "error" and kw["since"] == "6h" and kw["limit"] == 3
            return {"total": 1, "returned": 1, "lines": [{"time": "t", "stream": "stderr", "pod": "pod-1", "message": "boom"}]}

    monkeypatch.setattr(cli, "_platform", lambda: P())
    r = CliRunner().invoke(cli_app, ["--json", "logs", "chatbot", "--search", "error", "--since", "6h", "-n", "3"])
    assert r.exit_code == 0 and json.loads(r.stdout)["lines"][0]["message"] == "boom"
    tools = [t.name for t in asyncio.run(build_server().list_tools())]
    assert "search_logs" in tools and "app_logs" in tools
