import asyncio

import dapp_login


def settings(**env):
    base = {"DAPP_OIDC_ISSUER": "https://x/oidc", "DAPP_OIDC_CLIENT_ID": "c", "clientSecret": "s",
            "DAPP_PUBLIC_URL": "https://x/chatbot", "DAPP_HEIMDALL_URL": "https://x/heimdall",
            "DAPP_SESSION_KEY": "k", "DAPP_ALLOWED_USERS": "alice", "DAPP_ALLOWED_ROLES": "ops"}
    base.update(env)
    import os
    old = dict(os.environ)
    os.environ.update(base)
    try:
        return dapp_login.Settings()
    finally:
        os.environ.clear()
        os.environ.update(old)


async def inner(scope, receive, send):
    user = dict(scope["headers"]).get(b"x-dapp-user", b"").decode()
    await send({"type": "http.response.start", "status": 200, "headers": []})
    await send({"type": "http.response.body", "body": f"hi {user}".encode()})


def call(app, path, headers=()):
    sent = []

    async def send(msg):
        sent.append(msg)

    async def receive():
        return {"type": "http.request"}

    scope = {"type": "http", "path": path, "query_string": b"", "headers": [(k.encode(), v.encode()) for k, v in headers]}
    asyncio.run(app(scope, receive, send))
    start = sent[0]
    return start["status"], dict(start["headers"]), b"".join(m.get("body", b"") for m in sent[1:])


def test_cookie_roundtrip_and_tamper():
    lg = dapp_login.Login(settings())
    c = lg.seal({"id": "alice", "tags": []})
    assert lg.unseal(c) == {"id": "alice", "tags": []}
    assert lg.unseal(c[:-2] + "xx") is None


def test_allow_rules():
    lg = dapp_login.Login(settings())
    assert lg.allowed({"id": "alice", "tags": []})
    assert lg.allowed({"id": "bob", "tags": ["roles:id:ops"]})
    assert not lg.allowed({"id": "bob", "tags": ["roles:id:user"]})


def test_middleware_flow():
    s = settings()
    app = dapp_login.protect(inner, settings=s)
    lg = dapp_login.Login(s)
    assert call(app, "/chatbot/healthz")[0] == 200
    status, hdrs, _ = call(app, "/chatbot/", [("accept", "text/html")])
    assert status == 302 and hdrs[b"location"].startswith(b"/chatbot/_dapp/login")
    assert call(app, "/chatbot/api/chat")[0] == 401
    ok = f"dapp_auth={lg.seal({'id': 'alice', 'tags': []})}"
    status, _, body = call(app, "/chatbot/", [("cookie", ok), ("x-dapp-user", "spoofed")])
    assert status == 200 and body == b"hi alice"
    bob = f"dapp_auth={lg.seal({'id': 'bob', 'tags': []})}"
    assert call(app, "/chatbot/", [("cookie", bob)])[0] == 403
