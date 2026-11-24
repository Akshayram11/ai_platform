"""Put DataOS sign-in in front of any web server, including ones that aren't Python (a terminal, Streamlit, Node).

    DAPP_UPSTREAM=http://127.0.0.1:7681 uvicorn dapp_proxy:app --port 8080

Every request and websocket goes through dapp_login (when the app is deployed private, DAPP_AUTH=oidc) and is then
passed to the upstream server on localhost, unchanged except for hop-by-hop headers. The upstream sees the signed-in
DataOS user in x-dapp-user.

DAPP_PROXY_ONLY=<user ids>  narrows who may get through further, whatever the app's access list says. The Claude
Code app sets it to its owner: its terminal is a shell running with the owner's DataOS key.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os

import httpx

log = logging.getLogger("dapp.proxy")
HOP = {"connection", "keep-alive", "proxy-authenticate", "proxy-authorization", "te", "trailers",
       "transfer-encoding", "upgrade", "host", "content-length"}
WS_SKIP = HOP | {"sec-websocket-key", "sec-websocket-version", "sec-websocket-extensions", "sec-websocket-protocol",
                 "cookie", "authorization", "apikey"}


def _only() -> set[str]:
    return {u.strip() for u in os.environ.get("DAPP_PROXY_ONLY", "").split(",") if u.strip()}


def _user(scope) -> str:
    return next((v.decode() for k, v in scope.get("headers", []) if k.lower() == b"x-dapp-user"), "")


def make_proxy(upstream: str, *, transport: httpx.AsyncBaseTransport | None = None, ws_connect=None):
    upstream = upstream.rstrip("/")
    client = httpx.AsyncClient(base_url=upstream, timeout=httpx.Timeout(None, connect=10), transport=transport)
    only = _only()

    async def deny(scope, receive, send):
        if scope["type"] == "websocket":
            await receive()
            return await send({"type": "websocket.close", "code": 4403})
        body = b"This app is only for its owner."
        await send({"type": "http.response.start", "status": 403,
                    "headers": [(b"content-type", b"text/plain"), (b"content-length", str(len(body)).encode())]})
        await send({"type": "http.response.body", "body": body})

    async def http(scope, receive, send):
        body = bytearray()
        while True:
            msg = await receive()
            body += msg.get("body", b"")
            if not msg.get("more_body"):
                break
        headers = [(k.decode(), v.decode()) for k, v in scope["headers"] if k.decode().lower() not in HOP]
        path = scope.get("raw_path") or scope["path"].encode()
        url = path.decode() + ("?" + scope["query_string"].decode() if scope.get("query_string") else "")
        try:
            req = client.build_request(scope["method"], url, headers=headers, content=bytes(body))
            resp = await client.send(req, stream=True)
        except httpx.HTTPError as e:
            msg = f"upstream not reachable: {e.__class__.__name__}".encode()
            await send({"type": "http.response.start", "status": 502, "headers": [(b"content-type", b"text/plain")]})
            return await send({"type": "http.response.body", "body": msg})
        out = [(k.encode(), v.encode()) for k, v in resp.headers.multi_items() if k.lower() not in HOP]
        await send({"type": "http.response.start", "status": resp.status_code, "headers": out})
        try:
            async for chunk in resp.aiter_raw():
                await send({"type": "http.response.body", "body": chunk, "more_body": True})
        finally:
            await resp.aclose()
        await send({"type": "http.response.body", "body": b""})

    async def websocket(scope, receive, send):
        if ws_connect is None:
            from websockets.asyncio.client import connect
        else:
            connect = ws_connect
        await receive()  # websocket.connect
        protos = list(scope.get("subprotocols") or [])
        url = upstream.replace("http", "ws", 1) + scope["path"] + (
            "?" + scope["query_string"].decode() if scope.get("query_string") else "")
        headers = [(k.decode(), v.decode()) for k, v in scope["headers"] if k.decode().lower() not in WS_SKIP]
        try:
            up = await connect(url, subprotocols=protos or None, additional_headers=headers, max_size=None,
                               open_timeout=10, ping_interval=None)
        except Exception as e:
            log.warning("upstream websocket failed: %s", e)
            return await send({"type": "websocket.close", "code": 1011})
        await send({"type": "websocket.accept", "subprotocol": up.subprotocol})

        async def client_to_up():
            while True:
                msg = await receive()
                if msg["type"] == "websocket.disconnect":
                    return
                await up.send(msg["bytes"] if msg.get("bytes") is not None else msg.get("text", ""))

        async def up_to_client():
            async for data in up:
                await send({"type": "websocket.send", **({"bytes": data} if isinstance(data, bytes) else {"text": data})})

        tasks = [asyncio.ensure_future(client_to_up()), asyncio.ensure_future(up_to_client())]
        try:
            await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for t in tasks:
                t.cancel()
            await up.close()
            with contextlib.suppress(Exception):  # the browser may already have gone away
                await send({"type": "websocket.close", "code": 1000})

    async def app(scope, receive, send):
        if scope["type"] == "lifespan":
            while True:
                msg = await receive()
                if msg["type"] == "lifespan.startup":
                    await send({"type": "lifespan.startup.complete"})
                elif msg["type"] == "lifespan.shutdown":
                    await client.aclose()
                    return await send({"type": "lifespan.shutdown.complete"})
        if scope["type"] not in ("http", "websocket"):
            return None
        if scope["type"] == "http" and scope["path"].endswith("/healthz"):
            body = b'{"ok": true}'
            await send({"type": "http.response.start", "status": 200, "headers": [(b"content-type", b"application/json")]})
            return await send({"type": "http.response.body", "body": body})
        if only and _user(scope) not in only:
            return await deny(scope, receive, send)
        return await (websocket if scope["type"] == "websocket" else http)(scope, receive, send)

    return app


def build():
    app = make_proxy(os.environ.get("DAPP_UPSTREAM", "http://127.0.0.1:8081"))
    if os.environ.get("DAPP_AUTH") == "oidc":
        from dapp_login import protect

        app = protect(app, public_paths=("/healthz",))
    elif _only():
        raise RuntimeError("DAPP_PROXY_ONLY needs DataOS sign-in (deploy the app private)")
    if os.environ.get("DAPP_METRICS", "1") == "1":
        import dapp_metrics

        dapp_metrics.start()
        app = dapp_metrics.instrument(app)
    return app


def __getattr__(name):  # `uvicorn dapp_proxy:app` builds it on first use, so importing this module stays cheap
    if name == "app":
        globals()["app"] = build()
        return globals()["app"]
    raise AttributeError(name)
