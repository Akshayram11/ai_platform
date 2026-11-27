"""Compute (self-reported by apps) and LLM token (dapp gateway) usage.

Compute comes from Prometheus/Thanos. DataOS does not collect pod CPU/memory for app namespaces, so apps
with `metrics: true` report their own through dapp_metrics; DataOS' sidecar scrapes that and remote-writes it
with tenant=<tenant> and job_name=<app name> (plus our pod label). poliks serves the query API at
https://<fqdn>/observability and accepts the caller's DataOS token, so the dashboard asks as the user.
"""

from __future__ import annotations

from typing import Any

import httpx

# Samples are pushed every ~15s; a pod that has gone (old deploy, scale-down) leaves series that Prometheus
# still returns for 5 minutes. Only count pods whose uptime counter moved in the last 90 seconds.
ALIVE_WINDOW = "90s"


def observability_url(base_url: str) -> str:
    return f"{base_url.rstrip('/')}/observability"


def _q(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"')


def _selector(app: str, tenant: str, service: str | None = None) -> str:
    """job_name is only the app's name, and other people's apps in the tenant may share it (their own workspaces):
    service_name (the DataOS service behind this very app) keeps their pods out of our numbers."""
    sel = f'tenant="{_q(tenant)}",job_name="{_q(app)}"'
    return sel + (f',service_name="{_q(service)}"' if service else "")


def service_of(runtime_detail: dict[str, Any] | None) -> str | None:
    """The DataOS service name of an app (e.g. servicev2alphachatbotsvc-jwyp), from its runtime's pods."""
    for node in (runtime_detail or {}).get("nodeDetails") or []:
        name = ((node.get("labels") or {}).get("app") or "").strip()
        if name.startswith("servicev2alpha"):
            return name
    return None


def _alive(sel: str) -> str:
    return f"(changes(dapp_container_uptime_seconds{{{sel}}}[{ALIVE_WINDOW}]) > 0)"


class Prometheus:
    def __init__(self, url: str, *, headers: dict[str, str] | None = None,
                 transport: httpx.BaseTransport | None = None):
        self.http = httpx.Client(base_url=url.rstrip("/"), timeout=20, headers=headers or {}, transport=transport)

    def _scalar(self, query: str) -> float | None:
        r = self.http.get("/api/v1/query", params={"query": query})
        r.raise_for_status()
        res = r.json().get("data", {}).get("result", [])
        if not res:
            return None
        return float(res[0]["value"][1])

    def _range(self, query: str, start: float, end: float, step: str) -> list[tuple[float, float]]:
        r = self.http.get("/api/v1/query_range", params={"query": query, "start": start, "end": end, "step": step})
        r.raise_for_status()
        res = r.json().get("data", {}).get("result", [])
        if not res:
            return []
        return [(float(t), float(v)) for t, v in res[0]["values"]]

    def app_now(self, app: str, tenant: str, service: str | None = None) -> dict[str, Any]:
        sel = _selector(app, tenant, service)
        alive = _alive(sel)
        gauge = lambda name: f"sum({name}{{{sel}}} and on(pod) {alive})"
        return {
            "cpu_cores": self._scalar(
                f"sum(rate(dapp_container_cpu_usage_seconds_total{{{sel}}}[5m]) and on(pod) {alive})"),
            "memory_bytes": self._scalar(gauge("dapp_container_memory_working_set_bytes")),
            "cpu_limit_cores": self._scalar(gauge("dapp_container_cpu_limit_cores")),
            "memory_limit_bytes": self._scalar(gauge("dapp_container_memory_limit_bytes")),
            "pods": self._scalar(f"count(max by (pod) (dapp_container_uptime_seconds{{{sel}}}) and on(pod) {alive})"),
            # core-hours over the last 24h: what to charge back
            "cpu_core_hours_24h": self._scalar(
                f"sum(increase(dapp_container_cpu_usage_seconds_total{{{sel}}}[24h])) / 3600"),
            "requests_24h": self._scalar(f"sum(increase(dapp_http_requests_total{{{sel}}}[24h]))"),
            "errors_24h": self._scalar(f'sum(increase(dapp_http_requests_total{{{sel},code="5xx"}}[24h]))'),
        }

    def app_series(self, app: str, tenant: str, start: float, end: float, step: str = "5m",
                   service: str | None = None) -> dict[str, Any]:
        sel = _selector(app, tenant, service)
        return {
            "cpu_cores": self._range(f"sum(rate(dapp_container_cpu_usage_seconds_total{{{sel}}}[5m]))",
                                     start, end, step),
            "memory_bytes": self._range(f"sum(max by (pod) (dapp_container_memory_working_set_bytes{{{sel}}}))",
                                        start, end, step),
        }


def parse_tokens(text: str | int) -> int:
    """A token amount as people write it: 5000000, 5M, 200k, 2.5m, 50L / 50lakh, 1cr. At least 1."""
    if isinstance(text, int):
        n = text
    else:
        t = str(text).strip().lower().replace(",", "").replace("_", "").replace(" ", "")
        mult = {"k": 10**3, "m": 10**6, "b": 10**9, "l": 10**5, "lakh": 10**5, "lac": 10**5, "cr": 10**7, "crore": 10**7}
        unit = next((u for u in sorted(mult, key=len, reverse=True) if t.endswith(u)), "")
        num = t[: len(t) - len(unit)] if unit else t
        try:
            n = round(float(num) * mult.get(unit, 1))
        except ValueError as e:
            raise ValueError(f"{text!r} is not a token amount: write 5000000, 5M, 200k or 50lakh") from e
    if n < 1:
        raise ValueError("a budget is at least 1 token")
    return n


def show_tokens(n: int | None) -> str:
    if n is None:
        return "–"
    for div, unit in ((10**9, "B"), (10**6, "M"), (10**3, "k")):
        if n >= div:
            return f"{n / div:.1f}".rstrip("0").rstrip(".") + unit
    return str(n)


class GatewayUsage:
    """Reads LLM usage from the dapp gateway's admin API, as the calling DataOS user."""

    def __init__(self, url: str, auth_headers: dict[str, str], *, transport: httpx.BaseTransport | None = None):
        self.http = httpx.Client(base_url=url.rstrip("/"), timeout=20, headers=auth_headers, transport=transport)

    def summary(self, *, app: str | None = None, since_hours: int = 24 * 30,
                group_by: str = "app,model", model: str | None = None) -> dict[str, Any]:
        params: dict[str, Any] = {"since_hours": since_hours, "group_by": group_by}
        if app:
            params["app"] = app
        if model:
            params["model"] = model
        r = self.http.get("/admin/usage", params=params)
        r.raise_for_status()
        return r.json()

    def list_keys(self) -> list[dict[str, Any]]:
        r = self.http.get("/admin/keys")
        r.raise_for_status()
        return r.json()

    def revoke_key(self, app: str, tenant: str, prefix: str) -> dict[str, Any]:
        r = self.http.delete(f"/admin/keys/{tenant}/{app}/{prefix}")
        r.raise_for_status()
        return r.json()

    def issue_key(self, app: str, tenant: str, *, rotate: bool = False) -> dict[str, Any]:
        r = self.http.post("/admin/keys", json={"app": app, "tenant": tenant, "rotate": rotate})
        r.raise_for_status()
        return r.json()

    def issue_personal_key(self, tenant: str, *, rotate: bool = False) -> dict[str, Any]:
        r = self.http.post("/admin/keys/personal", json={"tenant": tenant, "rotate": rotate})
        r.raise_for_status()
        return r.json()

    def revoke_keys(self, app: str, tenant: str) -> dict[str, Any]:
        r = self.http.delete(f"/admin/keys/{tenant}/{app}")
        r.raise_for_status()
        return r.json()

    # ---- models served by DataOS apps
    def list_models(self, tenant: str | None = None) -> list[dict[str, Any]]:
        return self._tool_call("GET", "/admin/models", params={"tenant": tenant} if tenant else None)

    def register_model(self, body: dict[str, Any]) -> dict[str, Any]:
        return self._tool_call("POST", "/admin/models", json=body)

    def unregister_model(self, tenant: str, name: str) -> dict[str, Any]:
        return self._tool_call("DELETE", f"/admin/models/{tenant}/{name}")

    # monthly token budgets (the gateway lets only the app's owner change them)
    def budgets(self, tenant: str, app: str) -> dict[str, Any]:
        return self._tool_call("GET", f"/admin/budgets/{tenant}/{app}")

    def set_budgets(self, tenant: str, app: str, *, app_tokens: int | None = None, default_user_tokens: int | None = None,
                    clear: list[str] | None = None) -> dict[str, Any]:
        body = {k: v for k, v in (("app_tokens", app_tokens), ("default_user_tokens", default_user_tokens)) if v}
        return self._tool_call("PUT", f"/admin/budgets/{tenant}/{app}", json={**body, "clear": clear or []})

    def set_user_budget(self, tenant: str, app: str, user: str, tokens: int) -> dict[str, Any]:
        return self._tool_call("PUT", f"/admin/budgets/{tenant}/{app}/users/{user}", json={"tokens": tokens})

    def delete_user_budget(self, tenant: str, app: str, user: str) -> dict[str, Any]:
        return self._tool_call("DELETE", f"/admin/budgets/{tenant}/{app}/users/{user}")

    def models_served_by(self, app: str) -> list[str]:
        """Models (e.g. "small") that this DataOS app serves for the gateway."""
        return sorted(m["name"] for m in self.list_models() if m.get("app") == app)

    # ---- MCP tools
    def _tool_call(self, method: str, path: str, **kw) -> Any:
        """Gateway errors come back as {error: {message}}; surface that text instead of a bare HTTP status."""
        r = self.http.request(method, path, **kw)
        if r.status_code >= 400:
            try:
                body = r.json()
                msg = body["error"]["message"] if "error" in body else body["detail"]
                msg = msg if isinstance(msg, str) else str(msg)[:300]
            except (ValueError, KeyError, TypeError):
                msg = r.text[:300]
                if msg.lstrip().startswith("<") or r.status_code in (502, 503, 504):   # an error page from the ingress, not the gateway
                    msg = "the gateway is restarting or not reachable; try again in a minute"
            raise GatewayError(f"gateway: {msg} (HTTP {r.status_code})")
        return r.json()

    def list_tools(self, tenant: str | None = None) -> list[dict[str, Any]]:
        return self._tool_call("GET", "/admin/tools", params={"tenant": tenant} if tenant else None)

    def get_tool(self, tenant: str, name: str) -> dict[str, Any]:
        return self._tool_call("GET", f"/admin/tools/{tenant}/{name}")

    def save_tool(self, body: dict[str, Any]) -> dict[str, Any]:
        return self._tool_call("POST", "/admin/tools", json=body)

    def delete_tool(self, tenant: str, name: str) -> dict[str, Any]:
        return self._tool_call("DELETE", f"/admin/tools/{tenant}/{name}")

    def tool_access(self, tenant: str, name: str, allow: list[str], deny: list[str]) -> dict[str, Any]:
        return self._tool_call("POST", f"/admin/tools/{tenant}/{name}/access", json={"allow": allow, "deny": deny})

    # tools that ask each person to sign in (OAuth): the owner sets it up once, everyone connects their own account
    def oauth_setup(self, tenant: str, name: str, redirect_uri: str, *, client_id: str = "", client_secret: str = "",
                    scope: str | None = None) -> dict[str, Any]:
        body = {"redirect_uri": redirect_uri, "client_id": client_id, "client_secret": client_secret, "scope": scope}
        return self._tool_call("POST", f"/admin/tools/{tenant}/{name}/oauth/setup", json=body)

    def oauth_start(self, tenant: str, name: str, back: str = "") -> dict[str, Any]:
        return self._tool_call("POST", f"/admin/tools/{tenant}/{name}/oauth/start", json={"back": back})

    def oauth_finish(self, state: str, code: str) -> dict[str, Any]:
        return self._tool_call("POST", "/admin/oauth/finish", json={"state": state, "code": code})

    def oauth_disconnect(self, tenant: str, name: str) -> dict[str, Any]:
        return self._tool_call("DELETE", f"/admin/tools/{tenant}/{name}/oauth/me")

    def tool_usage(self, *, tool: str | None, tenant: str | None, since_hours: int, group_by: str) -> dict[str, Any]:
        params: dict[str, Any] = {"since_hours": since_hours, "group_by": group_by}
        params.update({k: v for k, v in (("tool", tool), ("tenant", tenant)) if v})
        return self._tool_call("GET", "/admin/tool-usage", params=params)


class GatewayError(RuntimeError):
    pass
