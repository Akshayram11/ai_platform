import json

import httpx
from fastapi.testclient import TestClient

from dapp.auth import Identity
from dapp_gateway.app import create_app
from dapp_gateway.meter import Meter
from dapp_gateway.store import Store


class FakeHeimdall:
    def identify(self, token):
        return {"alice-key": Identity("alice"), "bob-key": Identity("bob")}.get(token)


def upstream(req: httpx.Request) -> httpx.Response:
    assert req.headers["authorization"] == "Bearer sk-real"  # real key swapped in, app key dropped
    assert "x-dapp-user" not in req.headers  # who the app's user is stays between the app and the gateway
    body = json.loads(req.content)
    if body.get("stream"):
        assert body["stream_options"]["include_usage"] is True
        sse = (b'data: {"model":"gpt-4o-mini","choices":[{"delta":{"content":"hi"}}]}\n\n'
               b'data: {"model":"gpt-4o-mini","choices":[],"usage":{"prompt_tokens":7,"completion_tokens":2}}\n\n'
               b"data: [DONE]\n\n")
        return httpx.Response(200, stream=httpx.ByteStream(sse), headers={"content-type": "text/event-stream"})
    return httpx.Response(200, json={"model": "gpt-4o-mini", "choices": [],
                                     "usage": {"prompt_tokens": 10, "completion_tokens": 5}})


def dataos(req: httpx.Request) -> httpx.Response:
    if req.url.path.endswith("/resources/app/chatbot"):
        return httpx.Response(200, json={"name": "chatbot", "owner": "alice", "spec": {"app": {"owners": ["alice"]}}})
    return httpx.Response(404, json={})


def client(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-real")
    monkeypatch.setenv("DATAOS_FQDN", "x.dataos.cloud")
    app = create_app(store=Store(f"sqlite:///{tmp_path}/g.db"), heimdall=FakeHeimdall(),
                     upstream_transport=httpx.MockTransport(upstream),
                     dataos_transport=httpx.MockTransport(dataos))
    return TestClient(app)


def test_key_proxy_and_usage(tmp_path, monkeypatch):
    c = client(tmp_path, monkeypatch)
    assert c.post("/admin/keys", json={"app": "chatbot", "tenant": "ai"}).status_code == 401
    assert c.post("/admin/keys", json={"app": "chatbot", "tenant": "ai"},
                  headers={"apikey": "bob-key"}).status_code == 403
    r = c.post("/admin/keys", json={"app": "chatbot", "tenant": "ai"}, headers={"apikey": "alice-key"})
    key = r.json()["key"]

    assert c.post("/openai/v1/chat/completions", json={"model": "m"}).status_code == 401
    r = c.post("/openai/v1/chat/completions", json={"model": "gpt-4o-mini", "messages": []},
               headers={"authorization": f"Bearer {key}"})
    assert r.status_code == 200
    r = c.post("/openai/v1/chat/completions", json={"model": "gpt-4o-mini", "messages": [], "stream": True},
               headers={"authorization": f"Bearer {key}"})
    assert b"[DONE]" in r.content

    u = c.get("/admin/usage", headers={"apikey": "alice-key"}).json()
    assert u["total"] == {"requests": 2, "input_tokens": 17, "output_tokens": 7, "cost_usd": None}
    assert c.get("/admin/usage", headers={"apikey": "bob-key"}).json()["total"]["requests"] == 0

    c.delete("/admin/keys/ai/chatbot", headers={"apikey": "alice-key"})
    assert c.post("/openai/v1/chat/completions", json={},
                  headers={"authorization": f"Bearer {key}"}).status_code == 401


def test_meter_anthropic_stream():
    m = Meter()
    m.feed_sse(b'event: message_start\ndata: {"type":"message_start","message":{"model":"claude-x",'
               b'"usage":{"input_tokens":12,"cache_read_input_tokens":3}}}\n\n'
               b'data: {"type":"message_delta","usage":{"output_tokens":9}}\n')
    assert m.usage == {"model": "claude-x", "input_tokens": 12, "output_tokens": 9,
                       "cache_read_tokens": 3, "cache_write_tokens": 0}


def test_root_page(tmp_path, monkeypatch):
    c = client(tmp_path, monkeypatch)
    j = c.get("/").json()
    assert j["service"] == "dapp LLM gateway" and j["providers"]["openai"] is True
    h = c.get("/", headers={"accept": "text/html"})
    assert "text/html" in h.headers["content-type"] and "dapp LLM gateway" in h.text
    assert c.get("/nope").status_code == 404


def test_openai_cached_tokens_are_not_charged_twice():
    m = Meter()
    m.feed_json({"model": "gpt-6-luna", "usage": {"prompt_tokens": 1000, "completion_tokens": 50,
                                                 "prompt_tokens_details": {"cached_tokens": 800}}})
    assert m.usage["input_tokens"] == 200 and m.usage["cache_read_tokens"] == 800
    a = Meter()  # Anthropic reports cache reads separately: input is already the uncached part
    a.feed_json({"usage": {"input_tokens": 200, "cache_read_input_tokens": 800, "output_tokens": 5}})
    assert a.usage["input_tokens"] == 200 and a.usage["cache_read_tokens"] == 800


def test_pricing_matches_exact_or_dated_model_only():
    import json

    from dapp_gateway.store import Pricing
    p = Pricing(json.dumps({"gpt-6-luna": {"input": 0.10, "output": 0.50, "cache_read": 0.01},
                            "gpt-4o": {"input": 2.5, "output": 10}}))
    row = {"input_tokens": 200, "output_tokens": 50, "cache_read_tokens": 800}
    assert p.cost({**row, "model": "gpt-6-luna-2026-09-22"}) == round((200 * 0.10 + 50 * 0.50 + 800 * 0.01) / 1e6, 6)
    assert p.cost({**row, "model": "gpt-6-luna"}) is not None
    assert p.cost({**row, "model": "gpt-6-luna-pro"}) is None     # different model, different price
    assert p.cost({**row, "model": "gpt-4o-mini-2024-07-18"}) is None  # not gpt-4o
    assert p.cost({**row, "model": "gpt-4o-2024-08-06"}) is not None


def test_revoke_one_key_by_prefix(tmp_path, monkeypatch):
    c = client(tmp_path, monkeypatch)
    alice = {"apikey": "alice-key"}
    k1 = c.post("/admin/keys", json={"app": "chatbot", "tenant": "ai"}, headers=alice).json()
    k2 = c.post("/admin/keys", json={"app": "chatbot", "tenant": "ai"}, headers=alice).json()  # two active keys
    call = lambda k: c.post("/openai/v1/chat/completions", json={"model": "m", "messages": []},
                            headers={"authorization": f"Bearer {k['key']}"}).status_code
    assert call(k1) == 200 and call(k2) == 200
    assert c.delete(f"/admin/keys/ai/chatbot/{k1['prefix']}", headers={"apikey": "bob-key"}).status_code == 403
    assert c.delete(f"/admin/keys/ai/chatbot/{k1['prefix']}", headers=alice).json() == {"revoked": 1, "prefix": k1["prefix"]}
    assert call(k1) == 401 and call(k2) == 200          # only that one key is dead
    assert c.delete(f"/admin/keys/ai/chatbot/{k1['prefix']}", headers=alice).status_code == 404  # already revoked


def test_usage_is_attributed_to_the_end_user(tmp_path, monkeypatch):
    c = client(tmp_path, monkeypatch)
    alice = {"apikey": "alice-key"}
    key = c.post("/admin/keys", json={"app": "chatbot", "tenant": "ai"}, headers=alice).json()["key"]

    def chat(user, stream=False):
        h = {"authorization": f"Bearer {key}"}
        if user is not None:
            h["x-dapp-user"] = user
        return c.post("/openai/v1/chat/completions", json={"model": "gpt-4o-mini", "messages": [], "stream": stream}, headers=h)

    assert chat("carol").status_code == 200 and chat("carol", stream=True).status_code == 200
    assert chat("dave").status_code == 200 and chat(None).status_code == 200
    assert chat("x" * 500 + "\n\x00evil").status_code == 200      # junk is trimmed, never breaks the call

    rows = c.get("/admin/usage?group_by=end_user", headers=alice).json()["rows"]
    by = {r["end_user"]: r for r in rows}
    assert by["carol"]["requests"] == 2 and by["carol"]["input_tokens"] == 10 + 7
    assert by["dave"]["requests"] == 1 and by[""]["requests"] == 1       # "" = the app didn't say who
    assert max(len(u) for u in by) == 128 and all(u.isprintable() for u in by)
    # the app owner sees all of their app's users; somebody else sees none of them
    assert c.get("/admin/usage?group_by=end_user", headers={"apikey": "bob-key"}).json()["rows"] == []
    # and the by-user view can be narrowed to one app and combined with the model
    both = c.get("/admin/usage?app=chatbot&group_by=end_user,model", headers=alice).json()["rows"]
    assert {r["model"] for r in both} <= {"gpt-4o-mini-2024-07-18", "gpt-4o-mini"} and len(both) >= 3


def test_existing_database_without_end_user_is_migrated(tmp_path):
    import sqlite3

    from dapp_gateway.store import Store

    db = tmp_path / "old.db"
    con = sqlite3.connect(db)
    con.execute("""CREATE TABLE usage (id INTEGER PRIMARY KEY, ts DATETIME, tenant VARCHAR(64), app VARCHAR(64),
                   owner VARCHAR(128), provider VARCHAR(16), model VARCHAR(128), path VARCHAR(128), status INTEGER,
                   input_tokens INTEGER, output_tokens INTEGER, cache_read_tokens INTEGER, cache_write_tokens INTEGER,
                   latency_ms INTEGER)""")
    con.execute("INSERT INTO usage VALUES (1, '2026-11-23 08:00:00', 'ai', 'chatbot', 'alice', 'openai', 'm', '/', 200, "
                "5, 6, 0, 0, 10)")
    con.commit(); con.close()
    s = Store(f"sqlite:///{db}")                    # must not fail, and must keep the old row
    s.record(tenant="ai", app="chatbot", owner="alice", end_user="carol", provider="openai", model="m", path="/",
             status=200, input_tokens=1, output_tokens=2, cache_read_tokens=0, cache_write_tokens=0, latency_ms=1)
    rows = {r["end_user"]: r for r in s.summary(owner="alice", app=None, tenant=None, since_hours=24 * 365 * 5,
                                                group_by=["end_user"])}
    assert rows["carol"]["requests"] == 1 and rows[""]["input_tokens"] == 5   # the pre-feature row is "not attributed"
    Store(f"sqlite:///{db}")                        # opening it again is a no-op


def test_cost_per_user_is_priced_per_model_then_rolled_up(tmp_path, monkeypatch):
    import json

    monkeypatch.setenv("DAPP_PRICING", json.dumps({"cheap": {"input": 1.0, "output": 2.0},
                                                   "pricey": {"input": 10.0, "output": 20.0}}))
    c = client(tmp_path, monkeypatch)                      # same sqlite file the app uses
    db = Store(f"sqlite:///{tmp_path}/g.db")
    base = {"tenant": "ai", "app": "chatbot", "owner": "alice", "provider": "openai", "path": "/", "status": 200,
            "cache_read_tokens": 0, "cache_write_tokens": 0, "latency_ms": 1}
    db.record(**base, end_user="carol", model="cheap", input_tokens=1_000_000, output_tokens=500_000)    # 1.0 + 1.0
    db.record(**base, end_user="carol", model="pricey", input_tokens=100_000, output_tokens=50_000)      # 1.0 + 1.0
    db.record(**base, end_user="dave", model="cheap", input_tokens=2_000_000, output_tokens=0)           # 2.0
    alice = {"apikey": "alice-key"}

    by_user = {r["end_user"]: r for r in c.get("/admin/usage?group_by=end_user", headers=alice).json()["rows"]}
    assert by_user["carol"]["requests"] == 2 and by_user["carol"]["cost_usd"] == 4.0   # two models, one row, priced per model
    assert by_user["dave"]["cost_usd"] == 2.0 and "cost_partial" not in by_user["dave"]
    body = c.get("/admin/usage?group_by=app", headers=alice).json()
    assert body["rows"][0]["cost_usd"] == 6.0 == body["total"]["cost_usd"]          # per-user costs add up to the app's
    both = c.get("/admin/usage?group_by=end_user,model", headers=alice).json()["rows"]
    assert {(r["end_user"], r["model"]): r["cost_usd"] for r in both}[("carol", "pricey")] == 2.0

    db.record(**base, end_user="dave", model="mystery", input_tokens=5, output_tokens=5)               # no price configured
    dave = {r["end_user"]: r for r in c.get("/admin/usage?group_by=end_user", headers=alice).json()["rows"]}["dave"]
    assert dave["cost_usd"] == 2.0 and dave["cost_partial"] is True                   # a lower bound, flagged
    assert c.get("/admin/usage?group_by=end_user", headers=alice).json()["total"]["cost_partial"] is True


def test_local_model_goes_to_its_own_server(tmp_path, monkeypatch):
    """model "small" is served inside DataOS: same app key, same metering, but never sent to OpenAI."""
    seen = []

    def both(req: httpx.Request) -> httpx.Response:
        seen.append(req)
        if req.url.host == "llm-small":
            assert req.headers["authorization"] == "Bearer local-key"     # the local server's key, not OpenAI's
            body = json.loads(req.content)
            if body.get("stream"):
                assert body["stream_options"]["include_usage"] is True
                sse = (b'data: {"model":"small","choices":[{"delta":{"content":"hi"}}]}\n\n'
                       b'data: {"model":"small","choices":[],"usage":{"prompt_tokens":3,"completion_tokens":1}}\n\n'
                       b"data: [DONE]\n\n")
                return httpx.Response(200, stream=httpx.ByteStream(sse), headers={"content-type": "text/event-stream"})
            return httpx.Response(200, json={"model": "small", "choices": [],
                                             "usage": {"prompt_tokens": 4, "completion_tokens": 2}})
        return upstream(req)

    monkeypatch.setenv("OPENAI_API_KEY", "sk-real")
    monkeypatch.setenv("DATAOS_FQDN", "x.dataos.cloud")
    monkeypatch.setenv("DAPP_LOCAL_MODELS", '{"small": {"url": "http://llm-small:8080", "key_env": "LLM_SMALL_KEY"}}')
    app = create_app(store=Store(f"sqlite:///{tmp_path}/g.db"), heimdall=FakeHeimdall(),
                     upstream_transport=httpx.MockTransport(both), dataos_transport=httpx.MockTransport(dataos))
    c = TestClient(app)
    key = c.post("/admin/keys", json={"app": "chatbot", "tenant": "ai"}, headers={"apikey": "alice-key"}).json()["key"]
    h = {"authorization": f"Bearer {key}"}
    assert c.post("/openai/v1/chat/completions", json={"model": "small", "messages": []}, headers=h).status_code == 503
    monkeypatch.setenv("LLM_SMALL_KEY", "local-key")
    assert c.get("/healthz").json()["local_models"] == ["small"]
    assert c.post("/openai/v1/chat/completions", json={"model": "small", "messages": []}, headers=h).status_code == 200
    r = c.post("/openai/v1/chat/completions", json={"model": "small", "messages": [], "stream": True}, headers=h)
    assert b"[DONE]" in r.content
    assert str(seen[-1].url) == "http://llm-small:8080/v1/chat/completions"
    c.post("/openai/v1/chat/completions", json={"model": "gpt-4o-mini", "messages": []}, headers=h)
    assert seen[-1].url.host == "api.openai.com"                           # other models still go to OpenAI
    rows = c.get("/admin/usage?group_by=provider", headers={"apikey": "alice-key"}).json()["rows"]
    by = {r["provider"]: r for r in rows}
    assert by["local"]["requests"] == 2 and by["local"]["input_tokens"] == 7 and by["openai"]["requests"] == 1
    assert c.get("/healthz").json()["local_model_apps"] == {"small": "llm-small"}
    assert "model small" in c.get("/", headers={"accept": "text/html"}).text    # the landing page names it
    only = c.get("/admin/usage?model=small&group_by=app", headers={"apikey": "alice-key"}).json()
    assert only["rows"] == [{**only["rows"][0], "app": "chatbot", "requests": 2}] and only["total"]["requests"] == 2


def test_personal_key(tmp_path, monkeypatch):
    """A person's own key for laptop use: app user:<them>, only they can make or revoke it."""
    c = client(tmp_path, monkeypatch)
    assert c.post("/admin/keys/personal", json={"tenant": "ai"}).status_code == 401
    r = c.post("/admin/keys/personal", json={"tenant": "ai"}, headers={"apikey": "alice-key"}).json()
    assert r["app"] == "user:alice" and r["key"].startswith("dapp_")
    h = {"authorization": f"Bearer {r['key']}"}
    assert c.post("/openai/v1/chat/completions", json={"model": "gpt-4o-mini", "messages": []},
                  headers=h).status_code == 200
    rows = c.get("/admin/usage?group_by=app", headers={"apikey": "alice-key"}).json()["rows"]
    assert rows[0]["app"] == "user:alice" and rows[0]["requests"] == 1
    # nobody else can make, rotate or revoke alice's personal key through the app-key endpoints
    for body in ({"app": "user:alice", "tenant": "ai"}, {"app": "user:alice", "tenant": "ai", "rotate": True}):
        assert c.post("/admin/keys", json=body, headers={"apikey": "bob-key"}).status_code == 403
    assert c.delete("/admin/keys/ai/user:alice", headers={"apikey": "bob-key"}).status_code == 403
    bob = c.post("/admin/keys/personal", json={"tenant": "ai", "rotate": True}, headers={"apikey": "bob-key"}).json()
    assert bob["app"] == "user:bob"                                         # bob's rotate touches only bob's
    assert c.post("/openai/v1/chat/completions", json={"model": "gpt-4o-mini", "messages": []},
                  headers=h).status_code == 200
    assert c.delete(f"/admin/keys/ai/user:alice/{r['prefix']}", headers={"apikey": "alice-key"}).json()["revoked"] == 1
    assert c.post("/openai/v1/chat/completions", json={"model": "gpt-4o-mini", "messages": []},
                  headers=h).status_code == 401
