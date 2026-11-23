"""The dapp LLM gateway.

Apps call it instead of the provider (dapp sets ANTHROPIC_BASE_URL / OPENAI_BASE_URL for llm: true apps):

    /anthropic/<path>   ->  ANTHROPIC_UPSTREAM/<path>   (e.g. /anthropic/v1/messages)
    /openai/<path>      ->  OPENAI_UPSTREAM/<path>      (e.g. /openai/v1/chat/completions)

The app authenticates with its dapp key (x-api-key or Bearer); the gateway swaps in the real provider key
(from a DataOS secret) and records tokens per tenant/app/owner/model.

MCP tools: /mcp/<tenant>/<tool> proxies a registered MCP server (see tools.py).

Admin API (callers authenticate with their DataOS token/API key, verified by Heimdall):
    POST   /admin/keys                 {app, tenant, rotate}  -> {key}   issue an app key
    DELETE /admin/keys/{tenant}/{app}                                    revoke an app's keys
    GET    /admin/keys                                                   your app keys (no secrets)
    GET    /admin/usage?app=&since_hours=&group_by=app,model,day,end_user   token usage (+ cost if priced)
"""

from __future__ import annotations

import json
import logging
import os
import time
from typing import Any

import httpx
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response, StreamingResponse
from pydantic import BaseModel
from starlette.background import BackgroundTask

from dapp.auth import Heimdall, Identity, IdentityUnavailable, token_from_headers
from dapp.client import DataOS, DataOSError
from dapp.config import Context

from .meter import Meter
from .store import GROUPABLE, AppKey, Pricing, Store
from .tools import Vault, mount_tools

log = logging.getLogger("dapp.gateway")

PROVIDERS = {
    "anthropic": {
        "upstream": lambda: os.environ.get("ANTHROPIC_UPSTREAM", "https://api.anthropic.com"),
        "key_env": "ANTHROPIC_API_KEY",
        "auth": lambda key: {"x-api-key": key},
    },
    "openai": {
        "upstream": lambda: os.environ.get("OPENAI_UPSTREAM", "https://api.openai.com"),
        "key_env": "OPENAI_API_KEY",
        "auth": lambda key: {"authorization": f"Bearer {key}"},
    },
}
# Never forward the caller's credentials or hop-by-hop headers upstream.
DROP_REQ = {"x-dapp-user", "host", "authorization", "x-api-key", "apikey", "content-length", "connection", "accept-encoding",
            "cookie", "x-forwarded-for", "x-forwarded-host", "x-forwarded-proto", "x-real-ip"}
DROP_RESP = {"content-length", "content-encoding", "transfer-encoding", "connection"}


def clean_user(raw: str | None) -> str:
    """The app tells us who is using it (x-dapp-user, set by dapp_login). Never stored raw: trimmed, no control
    characters, at most 128 characters. It is attribution by the app, not proof: only the app's own key can set it."""
    return "".join(ch for ch in (raw or "").strip() if ch.isprintable())[:128]


class KeyRequest(BaseModel):
    app: str
    tenant: str
    rotate: bool = False


def create_app(store: Store | None = None, heimdall: Heimdall | None = None,
               upstream_transport: httpx.AsyncBaseTransport | None = None,
               dataos_transport: httpx.BaseTransport | None = None, vault: Vault | None = None) -> FastAPI:
    fqdn = os.environ.get("DATAOS_FQDN", "")
    store = store or Store()
    heimdall = heimdall or (Heimdall(f"https://{fqdn}") if fqdn else None)
    pricing = Pricing()
    admins = {a.strip() for a in os.environ.get("DAPP_GATEWAY_ADMINS", "").split(",") if a.strip()}
    upstream = httpx.AsyncClient(timeout=httpx.Timeout(600, connect=10), transport=upstream_transport)

    api = FastAPI(title="dapp LLM gateway", version="0.1.0")

    # ------------------------------------------------------------ auth helpers
    def caller(request: Request) -> Identity:
        if heimdall is None:
            raise HTTPException(503, "DATAOS_FQDN not configured; admin API disabled")
        token = token_from_headers(request.headers)
        try:
            ident = heimdall.identify(token)
        except IdentityUnavailable as e:
            raise HTTPException(503, f"DataOS identity check unavailable: {e}")
        if ident is None:
            raise HTTPException(401, "a valid DataOS token or API key is required")
        return ident

    def app_owners(request: Request, tenant: str, app: str) -> list[str] | None:
        """Owners of the DataOS app, read *as the caller* (so Poros enforces read access). None = no app yet."""
        token = token_from_headers(request.headers) or ""
        is_jwt = token.count(".") == 2
        ctx = Context("gateway", fqdn, tenant, None, None if is_jwt else token, token if is_jwt else None)
        try:
            with DataOS(ctx, transport=dataos_transport) as c:
                res = c.get_resource("app", app)
        except DataOSError as e:
            raise HTTPException(e.status or 502, f"could not read app {tenant}/{app}: {e}")
        if res is None:
            return None
        owners = ((res.get("spec") or {}).get("app") or {}).get("owners") or []
        return [*owners, res.get("owner")] if res.get("owner") else owners

    def require_owner(request: Request, ident: Identity, tenant: str, app: str) -> None:
        if ident.user_id in admins:
            return
        owners = app_owners(request, tenant, app)
        if owners is None:
            holder = store.key_owner(tenant, app)
            if holder and holder != ident.user_id:
                raise HTTPException(409, f"{tenant}/{app} keys belong to {holder}")
            return
        if ident.user_id not in owners:
            raise HTTPException(403, f"{ident.user_id} is not an owner of app {tenant}/{app}")

    # ------------------------------------------------------------ admin
    @api.get("/", include_in_schema=False)
    def root(request: Request):
        providers = {p: bool(os.environ.get(c["key_env"])) for p, c in PROVIDERS.items()}
        base = (request.scope.get("root_path") or "").rstrip("/")
        info = {
            "service": "dapp LLM gateway",
            "ok": True,
            "providers": providers,
            "endpoints": {
                "health": f"{base}/healthz",
                "openai": f"{base}/openai/v1/...  (apps use this via OPENAI_BASE_URL, with their dapp key)",
                "anthropic": f"{base}/anthropic/v1/...  (apps use this via ANTHROPIC_BASE_URL)",
                "usage": f"{base}/admin/usage  (DataOS sign-in token or API key)",
                "keys": f"{base}/admin/keys  (DataOS sign-in token or API key)",
                "tools": f"{base}/mcp/<tenant>/<tool>  (MCP tools: DataOS token/API key or a dapp app key)",
            },
            "dashboard": f"{base.rsplit('/', 1)[0]}/dapp/",
        }
        if "text/html" in request.headers.get("accept", ""):
            rows = "".join(f"<tr><td><code>{k}</code></td><td>{v}</td></tr>" for k, v in info["endpoints"].items())
            prov = ", ".join(f"{k}: {'configured' if v else 'not configured'}" for k, v in providers.items())
            return HTMLResponse(
                "<!doctype html><meta charset=utf-8><meta name=viewport content='width=device-width,initial-scale=1'>"
                "<title>dapp LLM gateway</title><style>body{font:15px system-ui;max-width:760px;margin:40px auto;padding:0 16px;"
                "color:#1c1c1a;background:#f7f7f5}@media(prefers-color-scheme:dark){body{color:#ecebe6;background:#141413}}"
                "td{padding:6px 10px;border-bottom:1px solid #8884;vertical-align:top}code{font-size:13px}</style>"
                f"<h1>dapp LLM gateway</h1><p>Running. Providers — {prov}.</p><table>{rows}</table>"
                f"<p><a href='{info['dashboard']}'>Open the dashboard</a></p>")
        return info

    @api.get("/healthz")
    def healthz() -> dict[str, Any]:
        return {"ok": True, "providers": {p: bool(os.environ.get(c["key_env"])) for p, c in PROVIDERS.items()}}

    @api.post("/admin/keys")
    def issue_key(body: KeyRequest, request: Request, ident: Identity = Depends(caller)) -> dict[str, Any]:
        require_owner(request, ident, body.tenant, body.app)
        key = store.issue_key(tenant=body.tenant, app=body.app, owner=ident.user_id, rotate=body.rotate)
        log.info("issued key for %s/%s to %s (rotate=%s)", body.tenant, body.app, ident.user_id, body.rotate)
        return {"app": body.app, "tenant": body.tenant, "key": key, "prefix": key[:12]}

    @api.delete("/admin/keys/{tenant}/{app}")
    def revoke(tenant: str, app: str, request: Request, ident: Identity = Depends(caller)) -> dict[str, Any]:
        require_owner(request, ident, tenant, app)
        return {"revoked": store.revoke(tenant, app)}

    @api.delete("/admin/keys/{tenant}/{app}/{prefix}")
    def revoke_one(tenant: str, app: str, prefix: str, request: Request,
                   ident: Identity = Depends(caller)) -> dict[str, Any]:
        require_owner(request, ident, tenant, app)
        n = store.revoke_prefix(tenant, app, prefix)
        if not n:
            raise HTTPException(404, f"no active key {prefix}… for {tenant}/{app}")
        log.info("revoked key %s for %s/%s by %s", prefix, tenant, app, ident.user_id)
        return {"revoked": n, "prefix": prefix}

    @api.get("/admin/keys")
    def list_keys(ident: Identity = Depends(caller)) -> list[dict]:
        return store.list_keys(None if ident.user_id in admins else ident.user_id)

    @api.get("/admin/usage")
    def usage(app: str | None = None, tenant: str | None = None, since_hours: int = 720,
              group_by: str = "app,model", all_users: bool = False,
              ident: Identity = Depends(caller)) -> dict[str, Any]:
        owner = None if (all_users and ident.user_id in admins) else ident.user_id
        wanted = [g for g in (x.strip() for x in group_by.split(",")) if g in GROUPABLE] or ["app"]
        # A price belongs to a model, so always fetch per model, price each row, then roll up to the grouping asked for.
        fine = store.summary(owner=owner, app=app, tenant=tenant, since_hours=since_hours,
                             group_by=wanted if "model" in wanted else [*wanted, "model"])
        tokens = ("requests", "input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens", "errors")
        merged: dict[tuple, dict[str, Any]] = {}
        total = {"requests": 0, "input_tokens": 0, "output_tokens": 0, "cost_usd": None}
        unpriced_any = False
        for r in fine:
            row = merged.setdefault(tuple(r[c] for c in wanted), {
                **{c: r[c] for c in wanted}, **dict.fromkeys(tokens, 0), "cost_usd": None, "_unpriced": False})
            for f in tokens:
                row[f] += r[f]
            cost = pricing.cost(r)
            if cost is None:
                row["_unpriced"] = unpriced_any = True
            else:
                row["cost_usd"] = round((row["cost_usd"] or 0) + cost, 8)
                total["cost_usd"] = round((total["cost_usd"] or 0) + cost, 8)
            total["requests"] += r["requests"]
            total["input_tokens"] += r["input_tokens"]
            total["output_tokens"] += r["output_tokens"]
        rows = list(merged.values())
        for row in rows:  # "cost_partial": some of this row's usage is on a model with no configured price
            if row.pop("_unpriced") and row["cost_usd"] is not None:
                row["cost_partial"] = True
        if unpriced_any and total["cost_usd"] is not None:
            total["cost_partial"] = True
        return {"owner": owner or "*", "since_hours": since_hours, "rows": rows, "total": total}

    # ------------------------------------------------------------ proxy
    def app_key(request: Request) -> AppKey:
        token = request.headers.get("x-api-key") or token_from_headers(request.headers)
        k = store.lookup(token or "")
        if k is None:
            raise HTTPException(401, "invalid or revoked dapp app key")
        return k

    async def proxy(provider: str, path: str, request: Request, key: AppKey) -> Response:
        conf = PROVIDERS[provider]
        real_key = os.environ.get(conf["key_env"])
        if not real_key:
            raise HTTPException(503, f"{provider} is not configured on the gateway ({conf['key_env']})")
        body = await request.body()
        req_json: dict | None = None
        if body and "json" in request.headers.get("content-type", ""):
            try:
                req_json = json.loads(body)
            except ValueError:
                req_json = None
        streaming = bool(req_json and req_json.get("stream"))
        if provider == "openai" and streaming and path.endswith("chat/completions") and req_json is not None:
            req_json.setdefault("stream_options", {})["include_usage"] = True  # otherwise no usage in stream
            body = json.dumps(req_json).encode()

        headers = {k: v for k, v in request.headers.items() if k.lower() not in DROP_REQ}
        headers.update(conf["auth"](real_key))
        url = f"{conf['upstream']().rstrip('/')}/{path}"
        meter = Meter(model=(req_json or {}).get("model", "") if isinstance(req_json, dict) else "")
        started = time.monotonic()

        end_user = clean_user(request.headers.get("x-dapp-user"))

        def record(status: int) -> None:
            try:
                store.record(tenant=key.tenant, app=key.app, owner=key.owner, end_user=end_user, provider=provider,
                             path=path[:128],
                             status=status, latency_ms=int((time.monotonic() - started) * 1000), **meter.usage)
            except Exception:  # metering must never break the app's call
                log.exception("failed to record usage")

        up_req = upstream.build_request(request.method, url, params=request.query_params, headers=headers,
                                        content=body)
        up = await upstream.send(up_req, stream=True)
        resp_headers = {k: v for k, v in up.headers.items() if k.lower() not in DROP_RESP}
        is_sse = "text/event-stream" in up.headers.get("content-type", "")

        if is_sse:
            async def relay():
                async for chunk in up.aiter_raw():
                    meter.feed_sse(chunk)
                    yield chunk

            async def finish():
                await up.aclose()
                record(up.status_code)

            return StreamingResponse(relay(), status_code=up.status_code, headers=resp_headers,
                                     background=BackgroundTask(finish))

        content = await up.aread()
        await up.aclose()
        try:
            meter.feed_json(json.loads(content))
        except ValueError:
            pass
        record(up.status_code)
        return Response(content, status_code=up.status_code, headers=resp_headers)

    @api.api_route("/anthropic/{path:path}", methods=["GET", "POST", "PUT", "DELETE"])
    async def anthropic(path: str, request: Request, key: AppKey = Depends(app_key)) -> Response:
        return await proxy("anthropic", path, request, key)

    @api.api_route("/openai/{path:path}", methods=["GET", "POST", "PUT", "DELETE"])
    async def openai(path: str, request: Request, key: AppKey = Depends(app_key)) -> Response:
        return await proxy("openai", path, request, key)

    mount_tools(api, store=store, heimdall=heimdall, caller=caller, admins=admins, upstream=upstream,
                clean_user=clean_user, vault=vault)

    @api.exception_handler(HTTPException)
    async def _http_exc(_: Request, exc: HTTPException) -> JSONResponse:
        # Provider-shaped error so SDKs surface the message.
        return JSONResponse({"type": "error", "error": {"type": "dapp_gateway_error", "message": exc.detail}},
                            status_code=exc.status_code)

    return api


def main() -> None:
    import uvicorn

    logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO").upper())
    root = os.environ.get("ROOT_PATH", "")  # e.g. /dapp-gateway behind the DataOS ingress
    import dapp_metrics

    dapp_metrics.start()
    uvicorn.run(dapp_metrics.instrument(create_app()), host="0.0.0.0", port=int(os.environ.get("PORT", "8080")),
                root_path=root, proxy_headers=True)


if __name__ == "__main__":
    main()
