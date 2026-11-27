"""Public front of the dapp gateway Service.

Browsers (Accept: text/html) asking for / get an information page about the gateway; every other request, and every other
path, is forwarded unchanged to the gateway Service inside the cluster (replies are streamed).
"""
import html
import os

import httpx
import uvicorn
from fastapi import FastAPI, Request
from starlette.background import BackgroundTask
from starlette.responses import HTMLResponse, StreamingResponse

UP = os.environ.get("UPSTREAM", "http://dapp-gateway:8080").rstrip("/")
FQDN = os.environ.get("DATAOS_FQDN", "poliks.instance.dataos.cloud")
HOP = {"host", "connection", "keep-alive", "transfer-encoding", "te", "upgrade", "proxy-authorization", "content-length"}
client = httpx.AsyncClient(timeout=httpx.Timeout(600, connect=10), follow_redirects=False)
app = FastAPI()

PAGE = """<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>dapp LLM gateway</title><style>
:root{{color-scheme:light dark;--bg:#fff;--fg:#1b1b1f;--mut:#6b6b76;--line:#e3e3e8;--ok:#14803c;--bad:#b42318}}
@media (prefers-color-scheme:dark){{:root{{--bg:#16161a;--fg:#ececf1;--mut:#9a9aa6;--line:#2c2c34;--ok:#4ade80;--bad:#f87171}}}}
body{{margin:0;background:var(--bg);color:var(--fg);font:15px/1.55 system-ui,sans-serif}}main{{max-width:760px;margin:0 auto;padding:32px 16px}}
h1{{font-size:24px;margin:0 0 4px}}h2{{font-size:15px;margin:28px 0 8px}}p{{margin:6px 0}}.m{{color:var(--mut)}}
code{{font:13px ui-monospace,monospace;background:color-mix(in srgb,var(--fg) 8%,transparent);padding:1px 5px;border-radius:4px}}
table{{border-collapse:collapse;width:100%}}td{{padding:7px 8px;border-top:1px solid var(--line);vertical-align:top}}td:first-child{{color:var(--mut);width:32%}}
.ok{{color:var(--ok)}}.bad{{color:var(--bad)}}</style></head><body><main>
<h1>dapp LLM gateway</h1><p class="m">LLM calls and MCP tools for DataOS apps, with per-app and per-user token metering and monthly budgets.</p>
<h2>Status</h2><table><tr><td>Gateway</td><td class="{cls}">{state}</td></tr><tr><td>Providers</td><td>{providers}</td></tr></table>
<h2>Where it runs</h2><table>
<tr><td>Kind</td><td>DataOS Service <code>dapp-gateway-core</code> (this page is its public front, the app <code>dapp-gateway-front</code>)</td></tr>
<tr><td>Inside DataOS</td><td><code>http://dapp-gateway:8080</code>: apps with <code>llm: true</code> use this; they need no change</td></tr>
<tr><td>Public</td><td><code>https://{fqdn}/dapp-gateway/</code></td></tr></table>
<h2>How apps use it</h2><table>
<tr><td>OpenAI SDK</td><td><code>OPENAI_BASE_URL</code> = <code>http://dapp-gateway:8080/openai/v1</code>, key = the app's <code>DAPP_LLM_KEY</code></td></tr>
<tr><td>Anthropic SDK</td><td><code>ANTHROPIC_BASE_URL</code> = <code>http://dapp-gateway:8080/anthropic</code></td></tr>
<tr><td>MCP tools</td><td><code>DAPP_TOOLS_URL</code> = <code>http://dapp-gateway:8080/mcp/&lt;tenant&gt;</code></td></tr>
<tr><td>From a terminal</td><td><code>dapp llm-key personal</code>, <code>dapp budget show APP</code>, <code>dapp usage APP</code>, <code>dapp tool ls</code></td></tr></table>
<p class="m" style="margin-top:24px">Manage keys, tools, budgets and usage in the DataOS Apps dashboard. This page only describes the gateway; it holds no data.</p>
</main></body></html>"""


async def info() -> HTMLResponse:
    state, cls, providers = "not reachable", "bad", "–"
    try:
        r = await client.get(f"{UP}/healthz", timeout=5)
        j = r.json()
        state, cls = ("ok", "ok") if r.status_code == 200 and j.get("ok") else ("unhealthy", "bad")
        providers = ", ".join(f"{k} {'on' if v else 'off'}" for k, v in (j.get("providers") or {}).items()) or "–"
    except (httpx.HTTPError, ValueError):
        pass
    return HTMLResponse(PAGE.format(state=html.escape(state), cls=cls, providers=html.escape(providers), fqdn=html.escape(FQDN)))


@app.api_route("/{path:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "HEAD"])
async def forward(path: str, request: Request):
    if path == "" and request.method == "GET" and "text/html" in request.headers.get("accept", ""):
        return await info()
    url = f"{UP}/{path}" + (f"?{request.url.query}" if request.url.query else "")
    headers = {k: v for k, v in request.headers.items() if k.lower() not in HOP}
    # The whole request body is read first and sent with a Content-Length: the gateway passes some headers on to OpenAI,
    # which refuses a chunked body (400). Requests are small (prompts); replies are still streamed.
    req = client.build_request(request.method, url, headers=headers, content=await request.body())
    try:
        r = await client.send(req, stream=True)
    except httpx.HTTPError as e:
        return StreamingResponse(iter([f'{{"error":"gateway unreachable: {e.__class__.__name__}"}}'.encode()]), status_code=502,
                                 media_type="application/json")
    out = {k: v for k, v in r.headers.items() if k.lower() not in HOP | {"content-encoding"}}
    return StreamingResponse(r.aiter_raw(), status_code=r.status_code, headers=out, background=BackgroundTask(r.aclose))


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", "8080")), proxy_headers=True)
