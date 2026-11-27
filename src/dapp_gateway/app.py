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
    GET    /admin/usage?app=&model=&since_hours=&group_by=app,model,day,end_user   token usage (+ cost if priced)
    GET    /admin/budgets/<tenant>/<app>                     monthly token budgets and spend (owner: all; others: their own)
    PUT    /admin/budgets/<tenant>/<app>                     {app_tokens, default_user_tokens, clear}   owner only
    PUT    /admin/budgets/<tenant>/<app>/users/<user>        {tokens}   one person's own budget          owner only
    DELETE /admin/budgets/<tenant>/<app>/users/<user>                                                  owner only
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
from typing import Any
from urllib.parse import urlsplit

import httpx
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response, StreamingResponse
from pydantic import BaseModel, Field
from starlette.background import BackgroundTask

from dapp.auth import Heimdall, Identity, IdentityUnavailable, token_from_headers
from dapp.client import DataOS, DataOSError
from dapp.config import Context

from . import landing
from .agent_traces import mount_agent_traces
from .meter import Meter
from .store import (
    APP_BUDGET,
    DEFAULT_USER_BUDGET,
    GROUPABLE,
    AppKey,
    LocalModel,
    Pricing,
    Store,
    month_of,
    next_month_start,
)
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


def local_models() -> dict[str, dict]:
    """Models served inside DataOS (e.g. llama.cpp), reached with the OpenAI API under their own name:
    DAPP_LOCAL_MODELS='{"small": {"url": "http://llm-small:8080", "key_env": "LLM_SMALL_KEY"}}'.
    An app asks for model "small" at /openai/v1/chat/completions and the gateway sends it there instead of OpenAI;
    metering, keys and budgets work the same, and the prompt never leaves DataOS."""
    raw = os.environ.get("DAPP_LOCAL_MODELS", "").strip()
    if not raw:
        return {}
    try:
        conf = json.loads(raw)
        return {str(k): v for k, v in conf.items() if isinstance(v, dict) and v.get("url")}
    except (ValueError, AttributeError):
        log.error("DAPP_LOCAL_MODELS is not valid JSON; local models are off")
        return {}


# Never forward the caller's credentials or hop-by-hop headers upstream.
DROP_REQ = {"x-dapp-user", "host", "authorization", "x-api-key", "apikey", "content-length", "connection", "accept-encoding",
            "cookie", "x-forwarded-for", "x-forwarded-host", "x-forwarded-proto", "x-real-ip"}
DROP_RESP = {"content-length", "content-encoding", "transfer-encoding", "connection", "x-dapp-model-backend"}


def clean_user(raw: str | None) -> str:
    """The app tells us who is using it (x-dapp-user, set by dapp_login). Never stored raw: trimmed, no control
    characters, at most 128 characters. It is attribution by the app, not proof: only the app's own key can set it."""
    return "".join(ch for ch in (raw or "").strip() if ch.isprintable())[:128]


class KeyRequest(BaseModel):
    app: str
    tenant: str
    rotate: bool = False


class BudgetRequest(BaseModel):
    """Monthly token budgets of an app. A field left out is not changed; `clear` removes one ("app" or "default_user")."""
    app_tokens: int | None = Field(None, ge=1, le=10**13)
    default_user_tokens: int | None = Field(None, ge=1, le=10**13)
    clear: list[str] = []


class UserBudgetRequest(BaseModel):
    tokens: int = Field(ge=1, le=10**13)


class PersonalKeyRequest(BaseModel):
    tenant: str
    rotate: bool = False  # true: the new key replaces (revokes) your earlier personal keys


class ModelRequest(BaseModel):
    tenant: str
    name: str                 # what callers put in "model"
    app: str                  # the DataOS app that serves it
    url: str                  # http://<app>:8080
    key: str = ""             # that server's own API key (kept encrypted)
    repo: str = ""
    description: str = ""


# names that already mean something to a provider: a local model must not shadow them
RESERVED_MODEL_PREFIXES = ("gpt", "o1", "o3", "o4", "chatgpt", "text-", "claude", "davinci", "babbage", "whisper", "dall-e",
                           "tts", "omni", "gemini", "ft:", "ada", "curie", "computer-use", "codex")
MODEL_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9.-]{0,38}[a-z0-9]$")


PERSONAL = "user:"  # app name of a personal key; ':' can't appear in a DataOS app name, so the two never clash


def create_app(store: Store | None = None, heimdall: Heimdall | None = None,
               upstream_transport: httpx.AsyncBaseTransport | None = None,
               dataos_transport: httpx.BaseTransport | None = None, vault: Vault | None = None,
               oauth_transport: httpx.BaseTransport | None = None,
               trace_transport: httpx.BaseTransport | None = None) -> FastAPI:
    fqdn = os.environ.get("DATAOS_FQDN", "")
    store = store or Store()
    heimdall = heimdall or (Heimdall(f"https://{fqdn}") if fqdn else None)
    pricing = Pricing()
    vault = vault or Vault()
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
        if app.startswith(PERSONAL):  # a person's own key ("user:<id>", never a DataOS app name): only theirs
            if app != PERSONAL + ident.user_id and ident.user_id not in admins:
                raise HTTPException(403, f"{app} is someone else's personal key")
            return
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
                "personal key": f"{base}/admin/keys/personal  (your own key for laptop use: dapp llm-key personal)",
                "tools": f"{base}/mcp/<tenant>/<tool>  (MCP tools: DataOS token/API key or a dapp app key)",
            },
            # models served inside DataOS: ask the openai endpoint for them by name
            "local_models": {m: f"served by DataOS app {urlsplit(c['url']).hostname}; "
                                f"model \"{m}\" on {base}/openai/v1/chat/completions"
                             for m, c in sorted(local_models().items())},
            "dashboard": f"{base.rsplit('/', 1)[0]}/dapp/",
        }
        if "text/html" in request.headers.get("accept", ""):
            return HTMLResponse(landing.page(base=base, host=os.environ.get("DATAOS_FQDN", ""), dashboard=info["dashboard"],
                                             providers=providers, local_models=info["local_models"],
                                             endpoints=info["endpoints"]))
        return info

    @api.get("/healthz")
    def healthz() -> dict[str, Any]:
        return {"ok": True, "providers": {p: bool(os.environ.get(c["key_env"])) for p, c in PROVIDERS.items()},
                "local_models": sorted(local_models()),
                # which DataOS app serves each local model (the host in its url), so the dashboard can show its use
                "local_model_apps": {m: urlsplit(c["url"]).hostname for m, c in sorted(local_models().items())}}

    @api.post("/admin/keys")
    def issue_key(body: KeyRequest, request: Request, ident: Identity = Depends(caller)) -> dict[str, Any]:
        require_owner(request, ident, body.tenant, body.app)
        key = store.issue_key(tenant=body.tenant, app=body.app, owner=ident.user_id, rotate=body.rotate)
        log.info("issued key for %s/%s to %s (rotate=%s)", body.tenant, body.app, ident.user_id, body.rotate)
        return {"app": body.app, "tenant": body.tenant, "key": key, "prefix": key[:12]}

    @api.post("/admin/keys/personal")
    def issue_personal_key(body: PersonalKeyRequest, ident: Identity = Depends(caller)) -> dict[str, Any]:
        """Your own key, to call the LLMs from a laptop or notebook. Usage counts as app user:<you>.
        It is for LLM calls only: MCP tools refuse it (use your DataOS key there)."""
        app = PERSONAL + ident.user_id
        key = store.issue_key(tenant=body.tenant, app=app, owner=ident.user_id, rotate=body.rotate)
        log.info("issued personal key for %s/%s (rotate=%s)", body.tenant, app, body.rotate)
        return {"app": app, "tenant": body.tenant, "key": key, "prefix": key[:12]}

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
    def usage(app: str | None = None, tenant: str | None = None, model: str | None = None, since_hours: int = 720,
              group_by: str = "app,model", all_users: bool = False,
              ident: Identity = Depends(caller)) -> dict[str, Any]:
        owner = None if (all_users and ident.user_id in admins) else ident.user_id
        wanted = [g for g in (x.strip() for x in group_by.split(",")) if g in GROUPABLE] or ["app"]
        # A price belongs to a model, so always fetch per model, price each row, then roll up to the grouping asked for.
        fine = store.summary(owner=owner, app=app, tenant=tenant, model=model, since_hours=since_hours,
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

    # ------------------------------------------------------------ local models (deployed from the dashboard)
    # ------------------------------------------------------------ budgets (monthly tokens, per app and per app user)
    def require_budget_owner(request: Request, ident: Identity, tenant: str, app: str) -> None:
        """Budgets are the app owner's alone: not gateway admins, not tenant admins (checked with DataOS every time)."""
        if app.startswith(PERSONAL):
            raise HTTPException(400, "personal keys have no budgets")
        owners = app_owners(request, tenant, app)
        if owners is None:
            raise HTTPException(404, f"no app {tenant}/{app}")
        if ident.user_id not in owners:
            raise HTTPException(403, f"only an owner of app {tenant}/{app} may change its budgets")

    def budget_view(tenant: str, app: str, viewer: str, full: bool) -> dict[str, Any]:
        b = store.budgets(tenant, app)
        month = month_of()
        app_budget, default_user = b.get(APP_BUDGET), b.get(DEFAULT_USER_BUDGET)
        app_spent = store.spent(tenant, app)
        spends = store.user_spend(tenant, app) if full else {viewer: store.spent(tenant, app, viewer)}
        people = set(spends) | ({u for u in b if u not in (APP_BUDGET, DEFAULT_USER_BUDGET)} if full else set())

        def left(budget: int | None, spent: int) -> int | None:
            return None if budget is None else max(0, budget - spent)

        def pct(budget: int | None, spent: int) -> float | None:
            return None if not budget else round(100 * spent / budget, 1)

        users = []
        for u in sorted(people, key=lambda x: -spends.get(x, 0)):
            ub = b.get(u, default_user)
            users.append({"user": u, "spent": spends.get(u, 0), "budget": ub, "own_budget": u in b,
                          "left": left(ub, spends.get(u, 0)), "pct": pct(ub, spends.get(u, 0))})
        return {"tenant": tenant, "app": app, "month": month, "resets_on": next_month_start(month).date().isoformat(),
                "app_budget": app_budget, "default_user_budget": default_user, "app_spent": app_spent,
                "app_left": left(app_budget, app_spent), "app_pct": pct(app_budget, app_spent),
                "users": users, "can_edit": full}

    @api.get("/admin/budgets/{tenant}/{app}")
    def get_budgets(tenant: str, app: str, request: Request, ident: Identity = Depends(caller)) -> dict[str, Any]:
        """The app's owner sees everything; anyone else sees the app's totals and their own row."""
        if app.startswith(PERSONAL):
            raise HTTPException(400, "personal keys have no budgets")
        owners = app_owners(request, tenant, app) or []
        return budget_view(tenant, app, ident.user_id, full=ident.user_id in owners)

    @api.put("/admin/budgets/{tenant}/{app}")
    def set_budgets(tenant: str, app: str, body: BudgetRequest, request: Request,
                    ident: Identity = Depends(caller)) -> dict[str, Any]:
        require_budget_owner(request, ident, tenant, app)
        current = store.budgets(tenant, app)
        app_budget = None if "app" in body.clear else (body.app_tokens or current.get(APP_BUDGET))
        default_user = None if "default_user" in body.clear else (body.default_user_tokens or current.get(DEFAULT_USER_BUDGET))
        if app_budget and default_user and default_user > app_budget:
            raise HTTPException(422, f"the default user budget ({default_user}) can't be above the app budget ({app_budget})")
        for field, user in (("app", APP_BUDGET), ("default_user", DEFAULT_USER_BUDGET)):
            if field in body.clear:
                store.set_budget(tenant, app, user, None, ident.user_id)
        if body.app_tokens:
            store.set_budget(tenant, app, APP_BUDGET, body.app_tokens, ident.user_id)
        if body.default_user_tokens:
            store.set_budget(tenant, app, DEFAULT_USER_BUDGET, body.default_user_tokens, ident.user_id)
        log.info("budgets of %s/%s set by %s: %s", tenant, app, ident.user_id, body.model_dump())
        view = budget_view(tenant, app, ident.user_id, full=True)
        over = [u["user"] for u in view["users"] if u["own_budget"] and app_budget and (u["budget"] or 0) > app_budget]
        if over:
            view["note"] = f"these users' own budgets are above the app budget, which applies first: {', '.join(over)}"
        return view

    @api.put("/admin/budgets/{tenant}/{app}/users/{user}")
    def set_user_budget(tenant: str, app: str, user: str, body: UserBudgetRequest, request: Request,
                        ident: Identity = Depends(caller)) -> dict[str, Any]:
        require_budget_owner(request, ident, tenant, app)
        user = clean_user(user)
        if not user or user in (APP_BUDGET, DEFAULT_USER_BUDGET):
            raise HTTPException(422, "say which user (their DataOS id)")
        app_budget = store.budgets(tenant, app).get(APP_BUDGET)
        if app_budget and body.tokens > app_budget:
            raise HTTPException(422, f"a user budget ({body.tokens}) can't be above the app budget ({app_budget})")
        store.set_budget(tenant, app, user, body.tokens, ident.user_id)
        log.info("budget of %s in %s/%s set to %s by %s", user, tenant, app, body.tokens, ident.user_id)
        return budget_view(tenant, app, ident.user_id, full=True)

    @api.delete("/admin/budgets/{tenant}/{app}/users/{user}")
    def delete_user_budget(tenant: str, app: str, user: str, request: Request,
                           ident: Identity = Depends(caller)) -> dict[str, Any]:
        require_budget_owner(request, ident, tenant, app)
        store.set_budget(tenant, app, clean_user(user), None, ident.user_id)
        return budget_view(tenant, app, ident.user_id, full=True)

    def model_view(m: LocalModel) -> dict[str, Any]:
        return {"name": m.name, "tenant": m.tenant, "owner": m.owner, "app": m.app, "repo": m.repo,
                "description": m.description, "created": m.created.isoformat(), "source": "dashboard"}

    ready_cache: dict[str, tuple[float, bool]] = {}

    async def model_ready(url: str) -> bool:
        """Has the model server loaded its model? llama-server answers /health with 200 only then (503 while it loads)."""
        now = time.monotonic()
        hit = ready_cache.get(url)
        if hit and hit[0] > now:
            return hit[1]
        try:
            ok = (await upstream.get(f"{url.rstrip('/')}/health", timeout=2.0)).status_code == 200
        except Exception:
            ok = False
        ready_cache[url] = (now + 5, ok)
        return ok

    async def local_model_rows(tenant: str | None) -> list[dict[str, Any]]:
        """The models of a tenant: the gateway's own config and the ones people deployed, each with whether it is ready."""
        rows = [({"name": n, "tenant": tenant or "", "owner": "", "app": urlsplit(c["url"]).hostname, "repo": "",
                  "description": c.get("description", ""), "created": "", "source": "config"}, c["url"])
                for n, c in sorted(local_models().items())]
        rows += [(model_view(m), m.url) for m in store.list_models(tenant)]
        flags = await asyncio.gather(*[model_ready(url) for _, url in rows])
        return [{**row, "ready": ok} for (row, _), ok in zip(rows, flags, strict=True)]

    @api.get("/admin/models")
    async def list_local_models(tenant: str | None = None, ident: Identity = Depends(caller)) -> list[dict[str, Any]]:
        return await local_model_rows(tenant)

    @api.get("/local-models")
    async def models_for_apps(request: Request) -> dict[str, Any]:
        """For an app's own key: the local models its tenant can ask for, and which are ready now (the chatbot's drop-down)."""
        key = app_key(request)
        return {"models": [{k: r[k] for k in ("name", "repo", "app", "source", "ready")}
                           for r in await local_model_rows(key.tenant)]}

    @api.post("/admin/models")
    def register_model(body: ModelRequest, request: Request, ident: Identity = Depends(caller)) -> dict[str, Any]:
        name = body.name.strip().lower()
        if not MODEL_NAME_RE.match(name) or name.startswith(RESERVED_MODEL_PREFIXES):
            raise HTTPException(422, "model name: lower-case letters, digits, '.' and '-', 2-40 characters, and not a "
                                     f"provider's own name ({', '.join(RESERVED_MODEL_PREFIXES[:6])}…)")
        if name in local_models():
            raise HTTPException(409, f"{name} is a model of the gateway's own configuration")
        if body.url != f"http://{body.app}:8080":   # only the app's own in-cluster address: never an arbitrary URL
            raise HTTPException(422, f"url must be http://{body.app}:8080, the address of the DataOS app that serves it")
        require_owner(request, ident, body.tenant, body.app)
        if app_owners(request, body.tenant, body.app) is None:
            raise HTTPException(404, f"no DataOS app {body.tenant}/{body.app}: deploy it first")
        if store.get_model(body.tenant, name):
            raise HTTPException(409, f"model {name} already exists in {body.tenant}")
        m = store.save_model(tenant=body.tenant, name=name, owner=ident.user_id, app=body.app, url=body.url,
                             key_secret=vault.seal(body.key) if body.key else "", repo=body.repo[:256],
                             description=body.description[:512])
        log.info("registered model %s/%s (app %s) for %s", body.tenant, name, body.app, ident.user_id)
        return model_view(m)

    @api.delete("/admin/models/{tenant}/{name}")
    def unregister_model(tenant: str, name: str, ident: Identity = Depends(caller)) -> dict[str, Any]:
        m = store.get_model(tenant, name)
        if m is None:
            raise HTTPException(404, f"no model {tenant}/{name}")
        if ident.user_id != m.owner and ident.user_id not in admins:
            raise HTTPException(403, f"model {name} belongs to {m.owner}")
        return {"deleted": store.delete_model(tenant, name), "name": name, "app": m.app}

    def find_local_model(model: str, tenant: str) -> dict | None:
        """The model server to use for this name: the gateway's own config first, then models people deployed."""
        conf = local_models().get(model)
        if conf:
            return {"url": conf["url"], "key": os.environ.get(conf.get("key_env", ""), "") if conf.get("key_env") else "",
                    "key_env": conf.get("key_env", "")}
        m = store.get_model(tenant, model)
        if m is None:
            return None
        return {"url": m.url, "key": vault.open(m.key_secret) if m.key_secret else "", "key_env": ""}

    # ------------------------------------------------------------ proxy
    def app_key(request: Request) -> AppKey:
        token = request.headers.get("x-api-key") or token_from_headers(request.headers)
        k = store.lookup(token or "")
        if k is None:
            raise HTTPException(401, "invalid or revoked dapp app key")
        return k

    def check_budget(key: AppKey, end_user: str) -> dict[str, str]:
        """Before a call: refuse it (429) when the app, or this user in the app, has used the month's budget; otherwise
        say what is left in headers. A user with no budget of their own and no default may use the app's whole budget.
        The tokens of a call are only known after it, so the call that crosses the line finishes; the next is refused."""
        if key.app.startswith(PERSONAL):
            return {}
        b = store.budgets(key.tenant, key.app)
        if not b:
            return {}
        out: dict[str, str] = {}
        resets = next_month_start(month_of()).date().isoformat()
        app_budget = b.get(APP_BUDGET)
        if app_budget is not None:
            spent = store.spent(key.tenant, key.app)
            if spent >= app_budget:
                raise HTTPException(429, f"app {key.app} has used its monthly token budget ({spent:,} of {app_budget:,} "
                                         f"tokens); it resets on {resets} (UTC). The app's owner can raise it.",
                                    headers={"x-dapp-budget-left": "0"})
            out["x-dapp-budget-left"] = str(app_budget - spent)
        user_budget = b.get(end_user, b.get(DEFAULT_USER_BUDGET)) if end_user else None
        if user_budget is not None:
            spent = store.spent(key.tenant, key.app, end_user)
            if spent >= user_budget:
                raise HTTPException(429, f"{end_user} has used their monthly token budget in app {key.app} ({spent:,} of "
                                         f"{user_budget:,} tokens); it resets on {resets} (UTC). Ask the app's owner "
                                         f"to raise it.", headers={**out, "x-dapp-user-budget-left": "0"})
            out["x-dapp-user-budget-left"] = str(user_budget - spent)
        return out

    async def proxy(provider: str, path: str, request: Request, key: AppKey) -> Response:
        body = await request.body()
        req_json: dict | None = None
        if body and "json" in request.headers.get("content-type", ""):
            try:
                req_json = json.loads(body)
            except ValueError:
                req_json = None
        model = req_json.get("model") if isinstance(req_json, dict) else None
        local = find_local_model(model, key.tenant) if provider == "openai" and isinstance(model, str) else None
        if local:
            provider = "local"
            conf = {"upstream": lambda: local["url"], "key_env": local["key_env"],
                    "auth": lambda k: {"authorization": f"Bearer {k}"}}
            real_key = local["key"]
            if local["key_env"] and not real_key:
                raise HTTPException(503, f"local model {model} is not configured on the gateway ({local['key_env']})")
        else:
            conf = PROVIDERS[provider]
            real_key = os.environ.get(conf["key_env"])
            if not real_key:
                raise HTTPException(503, f"{provider} is not configured on the gateway ({conf['key_env']})")
        streaming = bool(req_json and req_json.get("stream"))
        if provider in ("openai", "local") and streaming and path.endswith("chat/completions") and req_json is not None:
            req_json.setdefault("stream_options", {})["include_usage"] = True  # otherwise no usage in stream
            body = json.dumps(req_json).encode()

        headers = {k: v for k, v in request.headers.items() if k.lower() not in DROP_REQ}
        if real_key:
            headers.update(conf["auth"](real_key))
        url = f"{conf['upstream']().rstrip('/')}/{path}"
        meter = Meter(model=(req_json or {}).get("model", "") if isinstance(req_json, dict) else "")
        started = time.monotonic()

        end_user = clean_user(request.headers.get("x-dapp-user"))
        budget_headers = check_budget(key, end_user)

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
        resp_headers = {**{k: v for k, v in up.headers.items() if k.lower() not in DROP_RESP}, **budget_headers}
        if provider in ("openai", "local"):
            resp_headers["x-dapp-model-backend"] = provider
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

    mount_agent_traces(api, app_key, clean_user, trace_transport)
    mount_tools(api, store=store, heimdall=heimdall, caller=caller, admins=admins, upstream=upstream,
                clean_user=clean_user, vault=vault,
                oauth_http=httpx.Client(timeout=httpx.Timeout(20, connect=5), transport=oauth_transport))

    @api.exception_handler(HTTPException)
    async def _http_exc(_: Request, exc: HTTPException) -> JSONResponse:
        # Provider-shaped error so SDKs surface the message.
        kind = "budget_exceeded" if exc.status_code == 429 else "dapp_gateway_error"
        return JSONResponse({"type": "error", "error": {"type": kind, "message": exc.detail}},
                            status_code=exc.status_code, headers=getattr(exc, "headers", None))

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
