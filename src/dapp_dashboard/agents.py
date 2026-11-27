"""Dashboard routes and durable versions for the DataOS agent builder."""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import threading
from pathlib import Path
from tempfile import TemporaryDirectory
from uuid import uuid4

import httpx
from fastapi import HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from pydantic import BaseModel, Field

from dapp.agent_runtime import engine
from dapp.agent_runtime.engine import AgentConfig, AgentError, Chat
from dapp.agent_runtime.tracing import TraceId
from dapp.client import DataOSError
from dapp.ctl import WriteError
from dapp.platform import PlatformError, gateway_internal_url
from dapp.spec import AppSpec, yaml_decode
from dapp.usage import GatewayError
from dapp_objects import Client, ObjectsError

log = logging.getLogger("dapp.dashboard.agents")
RUNTIME = Path(engine.__file__).parent
RUNTIME_FILES = ("__init__.py", "engine.py", "mcp_transport.py", "tracing.py")
APP_FILES = ("published.py", "published.html", "chat.js", "agent.css")


def config_payload(config: AgentConfig):
    # Older snapshots did not have this field. Keep the same version identity when tracing is off.
    return config.model_dump_json(exclude=None if config.trace_enabled else {"trace_enabled"})


class Preview(Chat):
    config: AgentConfig


class Publish(BaseModel):
    version: int | None = Field(None, ge=1)


class Registry:
    def __init__(self, db):
        self.db = db

    def initialize(self):
        self.db.sql("""
            create table if not exists agents (
                id text primary key, tenant text not null, owner text not null, app text not null unique,
                draft text not null, applied integer, created text not null default current_timestamp
            )
        """)
        self.db.sql("""
            create table if not exists agent_versions (
                id text not null, version integer not null, config text not null,
                created text not null default current_timestamp,
                primary key (id, version), unique (id, config)
            )
        """)

    def list(self, session):
        rows = self.db.sql("select id, app, draft, applied from agents where tenant = ? and owner = ? "
                           "order by created desc, id", [session.tenant, session.user_id])
        return [{"id": row["id"], "app": row["app"], "applied": row["applied"],
                 "title": json.loads(row["draft"])["title"]} for row in rows]

    def tool_users(self, session) -> dict[str, list[str]]:
        """Which of this person's agents use each tool (by the tools ticked in the draft)."""
        out: dict[str, list[str]] = {}
        rows = self.db.sql("select draft from agents where tenant = ? and owner = ? order by created desc, id",
                           [session.tenant, session.user_id])
        for row in rows:
            draft = json.loads(row["draft"])
            for tool in draft.get("tools", []):
                out.setdefault(tool, []).append(draft.get("title", "agent"))
        return out

    def get(self, session, agent_id):
        rows = self.db.sql("select id, app, draft, applied from agents where id = ? and tenant = ? and owner = ?",
                           [agent_id, session.tenant, session.user_id])
        if not rows:
            raise HTTPException(404, "Agent not found.")
        row = rows[0]
        return {**row, "draft": json.loads(row["draft"]), "versions": self.db.sql(
            "select version, created from agent_versions where id = ? order by version desc", [agent_id])}

    def create(self, session, config: AgentConfig):
        agent_id = uuid4().hex
        slug = re.sub(r"[^a-z0-9]+", "-", config.title.lower()).strip("-")[:26].rstrip("-") or "chat"
        self.db.sql("insert into agents (id, tenant, owner, app, draft) values (?, ?, ?, ?, ?)",
                    [agent_id, session.tenant, session.user_id, f"agent-{slug}-{agent_id[:12]}",
                     config_payload(config)])
        return self.get(session, agent_id)

    def save(self, session, agent_id, config: AgentConfig):
        self.db.sql("update agents set draft = ? where id = ? and tenant = ? and owner = ?",
                    [config_payload(config), agent_id, session.tenant, session.user_id])
        return self.get(session, agent_id)

    def snapshot(self, agent_id, config: AgentConfig):
        payload = config_payload(config)
        self.db.sql("insert or ignore into agent_versions (id, version, config) "
                    "select ?, coalesce(max(version), 0) + 1, ? from agent_versions where id = ?",
                    [agent_id, payload, agent_id])
        return self.db.sql("select version from agent_versions where id = ? and config = ?",
                           [agent_id, payload])[0]["version"]

    def configuration(self, agent_id, version):
        rows = self.db.sql("select config from agent_versions where id = ? and version = ?", [agent_id, version])
        if not rows:
            raise HTTPException(404, "Agent version not found.")
        return AgentConfig.model_validate_json(rows[0]["config"])

    def delete(self, session, agent_id):
        agent = self.get(session, agent_id)              # 404 unless it is yours
        self.db.sql("delete from agent_versions where id = ?", [agent_id])
        self.db.sql("delete from agents where id = ? and tenant = ? and owner = ?",
                    [agent_id, session.tenant, session.user_id])
        return agent

    def mark_applied(self, session, agent_id, version):
        self.db.sql("update agents set applied = ? where id = ? and tenant = ? and owner = ?",
                    [version, agent_id, session.tenant, session.user_id])


def sign_in_notes(tools: list[dict], chosen: list[str]) -> list[str]:
    """What a person publishing should know about tools that ask people to sign in."""
    by_name = {t["name"]: t for t in tools}
    notes = []
    for name in chosen:
        t = by_name.get(name) or {}
        if t.get("auth_mode") != "oauth":
            continue
        if t.get("act_as") == "owner":
            if not t.get("owner_connected"):
                notes.append(f"Tool {name} is shared through {t.get('owner')}'s account, which is not connected right "
                             f"now: the agent will say so until {t.get('owner')} connects it on the Tools page.")
        else:
            notes.append(f"Tool {name} asks each person to sign in: whoever opens this agent is asked to connect "
                         f"{name} the first time (a link appears in the chat).")
    return notes


def deploy_version(platform, session, agent, config: AgentConfig, version: int):
    live = platform.client.get_resource("app", agent["app"])
    if live:
        svc = (live.get("spec") or {}).get("service") or {}
        marker = yaml_decode((svc.get("envs") or {}).get("DAPP_AGENT_ID"))
        if live.get("owner") != session.user_id or marker != agent["id"]:
            raise HTTPException(409, "This deployment name belongs to a different app.")

    with TemporaryDirectory(prefix="dapp-agent-") as folder:
        source = Path(folder)
        package = source / "agent_runtime"
        package.mkdir()
        for filename in RUNTIME_FILES:
            shutil.copyfile(RUNTIME / filename, package / filename)
        for filename in APP_FILES:
            shutil.copyfile(RUNTIME / filename, source / filename)
        (source / "agent.json").write_text(json.dumps({"config": json.loads(config_payload(config)), "version": version}))
        spec = AppSpec(
            name=agent["app"], title=config.title, description="Published by DataOS Agent Builder",
            tenant=session.tenant, runtime="python-3.12",
            code=folder, compute=os.environ.get("DAPP_AGENT_COMPUTE") or os.environ.get(
                "DAPP_DEFAULT_COMPUTE", "engineering-compute"),
            run="uvicorn published:app --host 127.0.0.1 --port $PORT",
            # The proxy supplies x-dapp-user, and keeps the private app behind DataOS sign-in.
            llm=True, metrics=True, tools=config.tools, owners=[session.user_id],
            env={"ROOT_PATH": "/" + agent["app"], "DAPP_AGENT_ID": agent["id"], "DAPP_AGENT_VERSION": str(version)},
        )
        return platform.deploy(spec)


def mount_agents(api, *, need, check_csrf, render, gateway_for, platform_for, app_objects_factory, hooks=None):
    ready = False
    setup_lock = threading.Lock()
    # The dashboard runs a single replica. Serialize deployments so two tabs cannot race a rollback.
    deploy_lock = threading.Lock()

    def registry():
        nonlocal ready
        client = app_objects_factory() if app_objects_factory else Client()
        if not client.url or not client.key:
            raise HTTPException(503, "Agent storage requires objects: true on the dashboard.")
        store = Registry(client.open("agent-builder"))
        with setup_lock:
            if not ready:
                store.initialize()
                ready = True
        return store

    if hooks is not None:                       # the Tools page asks which agents use a tool
        hooks["tool_users"] = lambda session: registry().tool_users(session)

    def catalog(session):
        gateway = gateway_for(session)
        hosted = sorted({m.strip() for m in os.environ.get("DAPP_AGENT_MODELS", "gpt-6-luna").split(",") if m.strip()})
        local = gateway.list_models(session.tenant)
        # Every model the picker shows: provider models through the gateway, and models hosted in DataOS (dapp model
        # deploy / the Models tab) with whether they answer yet. Only ready ones can be chosen (`models`).
        options = [{"name": m, "kind": "gateway", "ready": True} for m in hosted]
        options += [{"name": m["name"], "kind": "dataos", "ready": bool(m.get("ready")),
                     "description": m.get("description") or m.get("repo") or ""}
                    for m in sorted(local, key=lambda m: m["name"]) if m["name"] not in hosted]
        tools = [
            {"name": t["name"], "description": t.get("description", ""), "auth_mode": t.get("auth_mode", "key"),
             "act_as": t.get("act_as", ""), "connected": t.get("connected"), "owner": t.get("owner", ""),
             "owner_connected": t.get("owner_connected")}
            for t in gateway.list_tools(session.tenant)
        ]
        return {"models": sorted(o["name"] for o in options if o["ready"]), "model_options": options,
                "tools": sorted(tools, key=lambda t: t["name"])}

    def validate(session, config):
        options = catalog(session)
        if config.model not in options["models"]:
            raise HTTPException(422, "Select an available gateway model.")
        missing = set(config.tools) - {tool["name"] for tool in options["tools"]}
        if missing:
            raise HTTPException(403, f"Tool access is unavailable for {', '.join(sorted(missing))}.")

    @api.get("/agents", response_class=HTMLResponse)
    def page(request: Request):
        return render(request, "agents.html", need(request))

    @api.get("/traces", response_class=HTMLResponse)
    def traces(request: Request, agent: str = "", trace: TraceId | None = None, page: str = ""):
        session = need(request)
        agents, current, result, problem = [], None, {}, ""
        try:
            agents = registry().list(session)
            if agent:
                current = registry().get(session, agent)  # checks tenant and owner before reading MLflow
            key = os.environ.get("DAPP_LLM_KEY")
            service_tenant = os.environ.get("DATAOS_TENANT_ID")
            if not key or (service_tenant and service_tenant != session.tenant):
                problem = "Trace storage is unavailable for this workspace."
            else:
                params = {"app": current["app"] if current else "", "page": page, "tenant": session.tenant}
                if trace:
                    params["trace_id"] = trace
                response = httpx.get(f"{gateway_internal_url().rstrip('/')}/agent-traces", params=params,
                                     headers={"Authorization": f"Bearer {key}", "x-dapp-user": session.user_id},
                                     timeout=4.0)
                if response.status_code == 404 and trace:
                    raise HTTPException(404, "Trace not found.")
                response.raise_for_status()
                result = response.json()
        except (httpx.HTTPError, ObjectsError, ValueError):
            problem = "Could not load traces. Check that the trace service and gateway are running."
        return render(request, "traces.html", session, agents=agents, current=current, result=result, problem=problem)

    @api.get("/assets/agent-chat.js", include_in_schema=False)
    def chat_js():
        return FileResponse(RUNTIME / "chat.js", media_type="text/javascript")

    @api.get("/assets/agent.css", include_in_schema=False)
    def stylesheet():
        return FileResponse(RUNTIME / "agent.css", media_type="text/css")

    @api.get("/api/agents")
    def index(request: Request):
        session = need(request)
        try:
            return {**catalog(session), "agents": registry().list(session)}
        except (ObjectsError, GatewayError, httpx.HTTPError, ValueError) as exc:
            raise HTTPException(502, "Could not load the gateway catalog or agent storage.") from exc

    @api.get("/api/agents/models")       # before /api/agents/{agent_id}, which would take "models" as an id
    def models(request: Request):
        """The picker refreshes this while a model hosted in DataOS is still starting."""
        session = need(request)
        try:
            return catalog(session)
        except (GatewayError, httpx.HTTPError, ValueError) as exc:
            raise HTTPException(502, "Could not load the gateway catalog.") from exc

    @api.get("/api/agents/{agent_id}")
    def detail(agent_id: str, request: Request):
        try:
            return registry().get(need(request), agent_id)
        except ObjectsError as exc:
            raise HTTPException(502, "Agent storage is unavailable.") from exc

    @api.post("/api/agents")
    def create(config: AgentConfig, request: Request):
        s = need(request)
        check_csrf(s, request.headers.get("x-csrf-token", ""))
        try:
            return registry().create(s, config)
        except ObjectsError as exc:
            raise HTTPException(502, "Agent storage is unavailable.") from exc

    @api.put("/api/agents/{agent_id}")
    def save(agent_id: str, config: AgentConfig, request: Request):
        s = need(request)
        check_csrf(s, request.headers.get("x-csrf-token", ""))
        try:
            return registry().save(s, agent_id, config)
        except ObjectsError as exc:
            raise HTTPException(502, "Could not save the draft.") from exc

    @api.post("/api/agents/preview")
    def preview(req: Preview, request: Request):
        s = need(request)
        check_csrf(s, request.headers.get("x-csrf-token", ""))
        try:
            validate(s, req.config)
            return engine.run_agent(req.config, req, gateway=gateway_internal_url(),
                                    key=os.environ.get("DAPP_LLM_KEY", ""), tenant=s.tenant,
                                    user=s.user_id, tool_token=s.token)
        except AgentError as exc:
            if exc.trace_id:
                return JSONResponse({"error": str(exc), "trace_id": exc.trace_id}, status_code=502)
            raise HTTPException(502, str(exc)) from exc
        except GatewayError as exc:
            raise HTTPException(502, str(exc)) from exc
        except (httpx.HTTPError, ValueError) as exc:
            raise HTTPException(502, "The gateway catalog is unavailable.") from exc

    @api.delete("/api/agents/{agent_id}")
    def delete(agent_id: str, request: Request):
        """Forget the agent (draft and versions) and remove its app, if it still runs and is this agent's."""
        s = need(request)
        check_csrf(s, request.headers.get("x-csrf-token", ""))
        try:
            db = registry()
            agent = db.get(s, agent_id)
            removed = None
            p = platform_for(s)
            live = p.client.get_resource("app", agent["app"])
            if live:
                svc = (live.get("spec") or {}).get("service") or {}
                if live.get("owner") == s.user_id and yaml_decode((svc.get("envs") or {}).get("DAPP_AGENT_ID")) == agent_id:
                    p.delete(agent["app"], purge=True)       # its code secret, sign-in client and gateway key too
                    removed = agent["app"]
            db.delete(s, agent_id)
            return {"deleted": agent_id, "app_removed": removed}
        except ObjectsError as exc:
            raise HTTPException(502, "Agent storage is unavailable.") from exc
        except (DataOSError, PlatformError, WriteError, GatewayError) as exc:
            raise HTTPException(502, "Could not remove the agent's app; the agent was kept. Try again, or remove "
                                     f"{agent['app']} on the Apps page first.") from exc

    @api.post("/api/agents/{agent_id}/publish")
    def publish(agent_id: str, payload: Publish, request: Request):
        s = need(request)
        check_csrf(s, request.headers.get("x-csrf-token", ""))
        if not deploy_lock.acquire(blocking=False):
            raise HTTPException(409, "Another agent deployment is in progress. Try again shortly.")
        try:
            db = registry()
            agent = db.get(s, agent_id)
            version = payload.version
            config = db.configuration(agent_id, version) if version else AgentConfig.model_validate(agent["draft"])
            validate(s, config)
            if version is None:
                version = db.snapshot(agent_id, config)
            result = deploy_version(platform_for(s), s, agent, config, version)
            db.mark_applied(s, agent_id, version)
            # A tool someone else owns: the agent's app can't call it until that owner allows it.
            warnings = [f"Tool {m[1]} is not allowed for this agent yet: its owner must add the app {m[2]} on the "
                        f"Tools page (Who can use it → Apps)." for line in result.get("notes", [])
                        if (m := re.match(r"tool (\S+): could not allow app:(\S+)", line))]
            warnings += sign_in_notes(catalog(s)["tools"], config.tools)
            return {"agent": db.get(s, agent_id), "url": result["url"], "warnings": warnings}
        except (DataOSError, PlatformError, WriteError, GatewayError) as exc:
            log.warning("Agent deployment for %s failed: %s", agent_id, type(exc).__name__)
            raise HTTPException(502, "Could not publish this version. Check DataOS deploy permissions, compute, "
                                     "and gateway access.") from exc
        except (ObjectsError, httpx.HTTPError, ValueError) as exc:
            raise HTTPException(502, "The deployment could not be confirmed. Check Apps before retrying.") from exc
        finally:
            deploy_lock.release()
