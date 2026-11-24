import asyncio

import httpx
import pytest
from test_login import settings

import dapp_login
import dapp_proxy


def ws_call(app, path, headers=(), subprotocols=(), frames=()):
    """Drive one websocket through an ASGI app: connect, send frames, then disconnect. Returns what it sent."""
    sent = []
    inbox = [{"type": "websocket.connect"}, *({"type": "websocket.receive", "text": f} for f in frames)]

    async def receive():
        if inbox:
            return inbox.pop(0)
        await asyncio.sleep(0.05)
        return {"type": "websocket.disconnect", "code": 1000}

    async def send(msg):
        sent.append(msg)

    scope = {"type": "websocket", "path": path, "query_string": b"", "subprotocols": list(subprotocols),
             "headers": [(k.encode(), v.encode()) for k, v in headers]}
    asyncio.run(app(scope, receive, send))
    return sent


class FakeUpstreamWS:
    """Stands in for websockets' client connection to ttyd."""

    def __init__(self, record):
        self.record, self.subprotocol, self.sent = record, "tty", []

    def __aiter__(self):
        async def gen():
            yield b"\x00hello from the terminal"
        return gen()

    async def send(self, data):
        self.sent.append(data)
        self.record["sent"] = self.sent

    async def close(self):
        self.record["closed"] = True


def make(record, only=""):
    async def connect(url, **kw):
        record.update(url=url, **kw)
        return FakeUpstreamWS(record)

    def upstream(req: httpx.Request) -> httpx.Response:
        record["http"] = req
        return httpx.Response(200, headers={"content-type": "text/html"}, stream=httpx.ByteStream(b"<h1>ttyd</h1>"))

    import os
    os.environ["DAPP_PROXY_ONLY"] = only
    try:
        return dapp_proxy.make_proxy("http://127.0.0.1:7681", transport=httpx.MockTransport(upstream),
                                     ws_connect=connect)
    finally:
        os.environ.pop("DAPP_PROXY_ONLY", None)


def http_call(app, path, headers=()):
    sent = []

    async def receive():
        return {"type": "http.request", "body": b""}

    async def send(msg):
        sent.append(msg)

    scope = {"type": "http", "method": "GET", "path": path, "raw_path": path.encode(), "query_string": b"q=1",
             "headers": [(k.encode(), v.encode()) for k, v in headers]}
    asyncio.run(app(scope, receive, send))
    return sent[0]["status"], b"".join(m.get("body", b"") for m in sent[1:])


def test_http_and_websocket_pass_through():
    rec = {}
    app = make(rec)
    assert http_call(app, "/claude-code/", [("host", "x"), ("cookie", "c=1")]) == (200, b"<h1>ttyd</h1>")
    assert str(rec["http"].url) == "http://127.0.0.1:7681/claude-code/?q=1"
    sent = ws_call(app, "/claude-code/ws", [("x-dapp-user", "alice"), ("cookie", "dapp_auth=secret")],
                   subprotocols=["tty"], frames=["1ls\r"])
    assert sent[0] == {"type": "websocket.accept", "subprotocol": "tty"}
    assert {"type": "websocket.send", "bytes": b"\x00hello from the terminal"} in sent
    assert rec["url"] == "ws://127.0.0.1:7681/claude-code/ws" and rec["subprotocols"] == ["tty"]
    assert "cookie" not in dict(rec["additional_headers"])          # the session cookie stays with the proxy
    assert rec["closed"] is True


def test_only_the_owner_gets_through():
    rec = {}
    app = make(rec, only="alice")
    assert http_call(app, "/claude-code/", [("x-dapp-user", "bob")])[0] == 403
    assert ws_call(app, "/claude-code/ws", [("x-dapp-user", "bob")])[-1] == {"type": "websocket.close", "code": 4403}
    assert "url" not in rec                                          # never reached the terminal
    assert http_call(app, "/claude-code/", [("x-dapp-user", "alice")])[0] == 200


def test_login_now_guards_websockets():
    s = settings(DAPP_PUBLIC_URL="https://x/claude-code", DAPP_ALLOWED_USERS="alice")
    rec = {}
    app = dapp_login.protect(make(rec), settings=s)
    assert ws_call(app, "/claude-code/ws")[-1] == {"type": "websocket.close", "code": 4401}   # no cookie
    lg = dapp_login.Login(s)
    bob = lg.seal({"id": "bob", "tags": []})
    assert ws_call(app, "/claude-code/ws", [("cookie", f"dapp_auth={bob}")])[-1]["code"] == 4403
    alice = lg.seal({"id": "alice", "tags": []})
    sent = ws_call(app, "/claude-code/ws", [("cookie", f"dapp_auth={alice}"), ("x-dapp-user", "mallory")])
    assert sent[0]["type"] == "websocket.accept" and "url" in rec


def test_only_without_login_is_refused(monkeypatch):
    monkeypatch.setenv("DAPP_PROXY_ONLY", "alice")
    monkeypatch.delenv("DAPP_AUTH", raising=False)
    with pytest.raises(RuntimeError, match="needs DataOS sign-in"):
        dapp_proxy.build()
