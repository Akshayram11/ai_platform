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


def _selector(app: str, tenant: str) -> str:
    return f'tenant="{_q(tenant)}",job_name="{_q(app)}"'


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

    def app_now(self, app: str, tenant: str) -> dict[str, Any]:
        sel = _selector(app, tenant)
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

    def app_series(self, app: str, tenant: str, start: float, end: float, step: str = "5m") -> dict[str, Any]:
        sel = _selector(app, tenant)
        return {
            "cpu_cores": self._range(f"sum(rate(dapp_container_cpu_usage_seconds_total{{{sel}}}[5m]))",
                                     start, end, step),
            "memory_bytes": self._range(f"sum(max by (pod) (dapp_container_memory_working_set_bytes{{{sel}}}))",
                                        start, end, step),
        }


class GatewayUsage:
    """Reads LLM usage from the dapp gateway's admin API, as the calling DataOS user."""

    def __init__(self, url: str, auth_headers: dict[str, str], *, transport: httpx.BaseTransport | None = None):
        self.http = httpx.Client(base_url=url.rstrip("/"), timeout=20, headers=auth_headers, transport=transport)

    def summary(self, *, app: str | None = None, since_hours: int = 24 * 30,
                group_by: str = "app,model") -> dict[str, Any]:
        params: dict[str, Any] = {"since_hours": since_hours, "group_by": group_by}
        if app:
            params["app"] = app
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

    def revoke_keys(self, app: str, tenant: str) -> dict[str, Any]:
        r = self.http.delete(f"/admin/keys/{tenant}/{app}")
        r.raise_for_status()
        return r.json()
