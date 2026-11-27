"""Report and read agent turns through the gateway's existing app-key trust boundary."""

import hashlib
import json
import os
from uuid import uuid4

import httpx
from fastapi import Depends, HTTPException, Query, Request

from dapp.agent_runtime.tracing import TraceId, TraceReport

_TRACKING = "/api/2.0/mlflow"
_TRACES = "/api/3.0/mlflow/traces"


def _experiment_name(tenant, scope, subject):
    # Use a structured key so a colon or another delimiter in a principal cannot merge scopes.
    digest = hashlib.sha256(json.dumps([tenant, scope, subject]).encode()).hexdigest()
    return f"dapp-{scope}-{digest}"


def _attributes(values):
    return [{"key": key, "value": {"intValue": str(value)} if isinstance(value, int) else {"stringValue": value}}
            for key, value in values.items() if value is not None]


def _otlp(report, scope, app):
    root_id = uuid4().hex[:16]

    def span(name, start, end, ok, attrs, span_type, *, parent=None):
        value = {
            "traceId": report.trace_id[3:], "spanId": uuid4().hex[:16] if parent else root_id,
            "name": name, "kind": 1, "startTimeUnixNano": str(start), "endTimeUnixNano": str(max(start, end)),
            "status": {"code": 1 if ok else 2},
            "attributes": _attributes({**attrs, "mlflow.spanType": json.dumps(span_type)}),
        }
        if parent:
            value["parentSpanId"] = parent
        return value

    spans = [span("invoke_agent", report.start_ns, report.end_ns, report.ok, {
        "dapp.run_mode": scope, "dapp.app": app, "dapp.agent.version": report.version,
        "gen_ai.request.model": report.model,
    }, "AGENT")]
    for step in report.steps:
        attrs = {
            "gen_ai.tool.name": step.function, "dapp.tool.server": step.server,
            "gen_ai.request.model": report.model if step.kind == "chat" else None,
            "gen_ai.response.model": step.response_model, "dapp.model.backend": step.backend,
            "http.response.status_code": step.status_code, "gen_ai.usage.input_tokens": step.input_tokens,
            "gen_ai.usage.output_tokens": step.output_tokens, "error.type": step.error_type,
        }
        span_type = {"chat": "CHAT_MODEL", "discover_tools": "RETRIEVER", "execute_tool": "TOOL"}[step.kind]
        spans.append(span(step.kind, step.start_ns, step.end_ns, step.ok, attrs, span_type, parent=root_id))
    return {"resourceSpans": [{"scopeSpans": [{"scope": {"name": "dapp.agent"}, "spans": spans}]}]}


def _summary(info):
    return {
        "id": info["trace_id"],
        "started": info.get("request_time", ""),
        "duration": info.get("execution_duration", ""),
        "status": info.get("state", ""),
    }


def _step(span):
    attrs = {}
    for attr in span.get("attributes", []):
        value = attr.get("value", {})
        attrs[attr["key"]] = next(iter(value.values()), None)
    return {"name": span.get("name"), "ok": span.get("status", {}).get("code") in (1, "STATUS_CODE_OK"),
            "duration_ms": max(0, int(span["end_time_unix_nano"]) - int(span["start_time_unix_nano"])) / 10**6,
            "model": attrs.get("gen_ai.request.model"), "actual_model": attrs.get("gen_ai.response.model"),
            "backend": attrs.get("dapp.model.backend"), "server": attrs.get("dapp.tool.server"),
            "function": attrs.get("gen_ai.tool.name"), "version": attrs.get("dapp.agent.version"),
            "http_status": attrs.get("http.response.status_code"), "error": attrs.get("error.type"),
            "input_tokens": attrs.get("gen_ai.usage.input_tokens"), "output_tokens": attrs.get("gen_ai.usage.output_tokens")}


class TraceStore:
    def __init__(self, url, password, transport=None):
        self.url = url
        self.password = password
        self.transport = transport

    def client(self):
        if not self.url or not self.password:
            raise HTTPException(503, "Agent trace storage is not configured.")
        return httpx.Client(base_url=self.url, auth=("dapp-traces", self.password), timeout=5.0,
                            transport=self.transport, trust_env=False)

    @staticmethod
    def experiment(http, name, *, create=False):
        response = http.get(_TRACKING + "/experiments/get-by-name", params={"experiment_name": name})
        if response.status_code == 404 and create:
            added = http.post(_TRACKING + "/experiments/create", json={"name": name})
            if added.is_success:
                return added.json()["experiment_id"]
            if added.status_code != 409 and added.json().get("error_code") != "RESOURCE_ALREADY_EXISTS":
                added.raise_for_status()
            response = http.get(_TRACKING + "/experiments/get-by-name", params={"experiment_name": name})
        if response.status_code == 404 and not create:
            return None
        response.raise_for_status()
        return response.json()["experiment"]["experiment_id"]

    def write(self, tenant, scope, subject, report):
        with self.client() as http:
            experiment = self.experiment(http, _experiment_name(tenant, scope, subject), create=True)
            response = http.post("/v1/traces", headers={"x-mlflow-experiment-id": experiment},
                                 json=_otlp(report, scope, subject if scope == "published" else None))
            response.raise_for_status()

    def read(self, tenant, scope, subject, trace_id, page):
        with self.client() as http:
            experiment = self.experiment(http, _experiment_name(tenant, scope, subject))
            if trace_id:
                if experiment is None:
                    raise HTTPException(404, "Trace not found.")
                response = http.get(_TRACES + "/get", params={"trace_id": trace_id})
                if response.status_code == 404:
                    raise HTTPException(404, "Trace not found.")
                response.raise_for_status()
                trace = response.json()["trace"]
                info = trace["trace_info"]
                if info["trace_location"]["mlflow_experiment"]["experiment_id"] != experiment:
                    raise HTTPException(404, "Trace not found.")
                spans = sorted(trace.get("spans", []), key=lambda span: int(span["start_time_unix_nano"]))
                return {"trace": {**_summary(info), "steps": [_step(span) for span in spans]}}
            if experiment is None:
                return {"runs": []}
            query = {"locations": [{"type": "MLFLOW_EXPERIMENT", "mlflow_experiment": {"experiment_id": experiment}}],
                     "max_results": 25, "order_by": ["timestamp_ms DESC"]}
            if page:
                query["page_token"] = page
            response = http.post(_TRACES + "/search", json=query)
            response.raise_for_status()
            listing = response.json()
            return {"runs": [_summary(trace) for trace in listing.get("traces", [])],
                    "next_page": listing.get("next_page_token")}


def mount_agent_traces(api, app_key, clean_user, transport=None):
    storage = TraceStore(os.environ.get("DAPP_TRACE_URL"), os.environ.get("DAPP_TRACE_PASSWORD"), transport)
    dashboard = os.environ.get("DAPP_TRACE_DASHBOARD_APP", "dapp")

    def trace_user(request):
        user = request.headers.get("x-dapp-user")
        return user if user and user == clean_user(user) else None

    @api.post("/agent-traces", status_code=201, include_in_schema=False)
    def write_trace(report: TraceReport, request: Request, key=Depends(app_key)):
        if key.app == dashboard:
            scope, subject = "preview", trace_user(request)
            if not subject or report.version is not None:
                raise HTTPException(403, "A preview requires the dashboard user's identity.")
        elif key.app.startswith("agent-") and report.version is not None:
            scope, subject = "published", key.app
        else:
            raise HTTPException(403, "An agent app key is required.")
        try:
            storage.write(key.tenant, scope, subject, report)
        except (httpx.HTTPError, ValueError, KeyError) as exc:
            raise HTTPException(502, "Could not store the agent trace.") from exc
        return {"trace_id": report.trace_id}

    @api.get("/agent-traces", include_in_schema=False)
    def read_traces(request: Request, tenant: str = Query(max_length=64), app: str = Query("", max_length=48),
                    trace_id: TraceId | None = None, page: str = Query("", max_length=2048),
                    key=Depends(app_key)):
        if key.app != dashboard or tenant != key.tenant:
            raise HTTPException(403, "A dashboard app key is required.")
        user = trace_user(request)
        if not user or (app and not app.startswith("agent-")):
            raise HTTPException(403, "A dashboard user and valid agent scope are required.")
        try:
            return storage.read(key.tenant, "published" if app else "preview", app or user, trace_id, page)
        except (httpx.HTTPError, ValueError, KeyError) as exc:
            raise HTTPException(502, "Could not load agent traces.") from exc
