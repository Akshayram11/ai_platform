"""High-level app operations. The CLI, the MCP server and the dashboard all call these, so behaviour is
identical whether a person, an agent or a browser does it.
"""

from __future__ import annotations

import copy
import difflib
import logging
import os
import re
import secrets as pysecrets
import time
from dataclasses import dataclass
from typing import Any

from .claude import (
    TOKEN_KEY,
    ClaudeDeploy,
    claude_app_spec,
    clean_token,
    instance_name,
    is_claude_instance,
    token_secret,
)
from .client import DataOS, DataOSError
from .config import Context, current_context_name
from .ctl import ApiWriter, Writer, make_writer
from .models import MAX_MODELS_PER_PERSON, ModelDeploy, model_app_spec, render_volume
from .pack import Packed, pack
from .pipelines import (
    HOWS,
    PipelineRequest,
    build_manifest,
    check,
    depot_kind,
    depot_secrets,
    depot_summary,
    describe,
    missing_access,
    phase_of,
    resource_id,
    run_row,
)
from .pipelines import summary as pipeline_summary
from .sources import (
    SOURCE_TAG,
    SourceRequest,
    SourceType,
    depot_manifest,
    parse_types,
    redact,
    secret_manifest,
)
from .sources import check as check_source
from .spec import (
    ALLOWED_ROLES_ENV,
    ALLOWED_USERS_ENV,
    DAPP_TAG,
    OBJECTS_TOOL,
    AppSpec,
    app_tag,
    llm_secret_name,
    render_app,
    render_grant,
    render_secret,
    service_name,
    subject_for,
    wire_manifest,
    yaml_decode,
)
from .usage import GatewayUsage, Prometheus, observability_url, service_of

log = logging.getLogger("dapp.platform")


class PlatformError(RuntimeError):
    pass


def _safe_list(fn) -> list[dict[str, Any]]:
    try:
        return fn()
    except (PlatformError, DataOSError):
        return []


class PipelineAccessError(PlatformError):
    """DataOS refused a pipeline because the person may not use a Depot, Secret or Compute it names."""

    def __init__(self, missing: list[dict[str, str]], detail: str):
        names = ", ".join(f"{m['kind']} {m['name']}" for m in missing)
        super().__init__(f"You can't use {names} yet. Ask its owner for access (the Request access button does it).")
        self.missing, self.detail = missing, detail


def gateway_public_url(ctx: Context) -> str:
    return os.environ.get("DAPP_GATEWAY_URL") or f"{ctx.base_url}/dapp-gateway"


def gateway_internal_url() -> str:
    return os.environ.get("DAPP_GATEWAY_INTERNAL_URL", "http://dapp-gateway:8080")


def llm_env(gateway: str) -> dict[str, str]:
    """Point the common SDKs at the gateway; they pick these up without code changes."""
    return {
        "ANTHROPIC_BASE_URL": f"{gateway}/anthropic",
        "OPENAI_BASE_URL": f"{gateway}/openai/v1",
        "DAPP_LLM_GATEWAY_URL": gateway,
    }


VAR_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")
ID_RE = re.compile(r"^[A-Za-z0-9_.@*:-]+$")


def split_ids(csv: str) -> list[str]:
    """User/role ids from a comma list. Anything that isn't a plausible id is dropped, never carried forward."""
    return [i for i in (p.strip() for p in csv.split(",")) if ID_RE.match(i)]


def uses(a: dict[str, Any]) -> dict[str, Any]:
    """The DataOS secrets and Volumes an app mounts (names only: a secret's values are never read)."""
    svc = ((a.get("spec") or {}).get("service")) or {}
    return {"secrets": [str(x.get("id", "")).split(":")[-1] for x in svc.get("secrets") or [] if x.get("id")],
            "volumes": [{"id": v.get("id"), "directory": v.get("directory"), "readOnly": bool(v.get("readOnly"))}
                        for v in svc.get("volumes") or [] if v.get("id")],
            "disk": ((svc.get("disk") or {}).get("size") if svc.get("disk") else None)}


def access_summary(a: dict[str, Any]) -> dict[str, Any]:
    """Who can open an app, in one glance: login (n people, roles, or everyone), public, or DataOS policy."""
    spec = a.get("spec") or {}
    envs = live_envs(a)
    if envs.get("DAPP_AUTH") == "oidc":
        users = split_ids(envs.get(ALLOWED_USERS_ENV, ""))
        owners = (spec.get("app") or {}).get("owners") or []
        return {"mode": "login", "everyone": "*" in users,
                "people": len({u for u in users if u != "*"} | set(owners)),
                "roles": len(split_ids(envs.get(ALLOWED_ROLES_ENV, "")))}
    if (((spec.get("service") or {}).get("http") or {}).get("noNetworkAuthorization")):
        return {"mode": "public"}
    return {"mode": "gateway"}


def live_envs(res: dict[str, Any]) -> dict[str, str]:
    """A live app's env values as the container sees them (stored values may carry YAML quoting)."""
    raw = ((res.get("spec") or {}).get("service") or {}).get("envs") or {}
    return {k: yaml_decode(v) for k, v in raw.items()}


def app_state(status: str | None, runtime: str | None, error: str | None) -> str:
    """One word for humans: running / failed / pending / deleting (runtime looks like 'app-service:running:1')."""
    r, s = (runtime or "").lower(), (status or "").lower()
    if "fail" in r or "error" in r or error:
        return "failed"
    if "running" in r:
        return "running"
    if "delet" in s:
        return "deleting"
    return "pending"


def flatten_logs(raw: Any) -> str:
    """Poros returns {nodeLogs: [{name: pod, logs: [{name: container, log: [lines]}]}]}."""
    if isinstance(raw, str):
        return raw
    lines: list[str] = []
    for node in (raw or {}).get("nodeLogs") or []:
        for c in node.get("logs") or []:
            lines.append(f"==> {node.get('name')} / {c.get('name')} <==")
            lines.extend(c.get("log") or [])
            lines.append("")
    return "\n".join(lines) if lines else str(raw)


@dataclass
class DeployPlan:
    manifests: list[dict[str, Any]]
    notes: list[str]
    code: Packed | None = None


class Platform:
    def __init__(self, client: DataOS, writer: Writer | None = None):
        self.client = client
        self.ctx = client.ctx
        if writer is None:
            writer = make_writer(client)
            # dataos-ctl acts on *its* current context; if dapp targets another one, go through the API.
            if writer.name == "dataos-ctl" and self.ctx.name not in ("env", current_context_name()):
                writer = ApiWriter(client)
        self.writer = writer

    # ---------- read ----------
    def list_apps(self, *, everyone: bool = False, with_access: bool = False) -> list[dict[str, Any]]:
        """with_access also asks DataOS for each app's spec, to say who can open it (a heavier call)."""
        owner = None if everyone else self.ctx.user_id
        apps = self.client.list_resources("app", owner=owner, details=with_access)
        return [self._summarize(a) for a in apps]

    def _summarize(self, a: dict[str, Any]) -> dict[str, Any]:
        tags = a.get("tags") or []
        status, runtime, error = a.get("status"), a.get("runtime"), a.get("error") or a.get("runtimeError")
        if isinstance(status, dict):  # GET returns the full status object, LIST a flattened string
            runtime = runtime or (status.get("runtimeState") or {}).get("status")
            error = error or status.get("error")
            status = status.get("aggregateStatus")
        return {
            "name": a.get("name"),
            "tenant": a.get("tenant") or self.ctx.tenant,
            "owner": a.get("owner"),
            "state": app_state(status, runtime, error),
            "status": status,
            "runtime": runtime,
            "error": error,
            "created": a.get("createdAt"),
            "managed_by_dapp": DAPP_TAG in tags,
            "url": self.app_url(a),
            **({"access": access_summary(a), **uses(a)} if "spec" in a else {}),
        }

    def app_url(self, a: dict[str, Any]) -> str | None:
        spec = a.get("spec") or {}
        path = ((spec.get("service") or {}).get("http") or {}).get("path") or f"/{a.get('name')}"
        return f"{self.ctx.base_url}{path}/"

    def get_app(self, name: str) -> dict[str, Any]:
        app = self.client.get_resource("app", name)
        if app is None:
            raise PlatformError(f"app '{name}' not found in tenant {self.ctx.tenant}")
        return app

    def status(self, name: str) -> dict[str, Any]:
        app = self.get_app(name)
        out = {**self._summarize(app), "spec": app.get("spec"), "service": service_name(name)}
        try:  # the generated service is a sub-resource; Poros serves its runtime on the app
            out["runtime_detail"] = self.client.runtime("app", name)
        except DataOSError as e:
            out["runtime_detail"] = {"error": str(e)}
        return out

    def logs(self, name: str, *, tail: int = 100, node: str | None = None) -> Any:
        return self.client.logs("app", name, tail=tail, node=node)

    def logs_text(self, name: str, *, tail: int = 100, node: str | None = None) -> str:
        return flatten_logs(self.logs(name, tail=tail, node=node))

    # ---------- deploy / lifecycle ----------
    def plan(self, spec: AppSpec, *, compute: str | None = None) -> DeployPlan:
        tenant = spec.tenant or self.ctx.tenant
        notes: list[str] = []
        if spec.llm:
            spec = spec.model_copy(update={"env": {**llm_env(gateway_internal_url()), **spec.env}})
            notes.append(f"LLM calls are routed via the dapp gateway; key secret: {spec.llm_secret}")
            if spec.tools:
                spec = spec.model_copy(update={"env": {
                    "DAPP_TOOLS": ",".join(spec.tools),
                    "DAPP_TOOLS_URL": f"{gateway_internal_url()}/mcp/{tenant}", **spec.env}})
                notes.append(f"MCP tools via the gateway: {', '.join(spec.tools)}")
            if OBJECTS_TOOL in spec.tools:
                spec = spec.model_copy(update={"env": {
                    "DAPP_OBJECTS_URL": f"{gateway_internal_url()}/mcp/{tenant}/{OBJECTS_TOOL}", **spec.env}})
                notes.append("durable storage: immortal-obj (objects are this app's own: app:" + spec.name + ")")
        code = None
        if spec.runtime:
            code = pack(spec.code)
            # the version in the app's env changes with the code, so a code change always rolls the app
            spec = spec.model_copy(update={"env": {**spec.env, "DAPP_CODE_VERSION": code.sha256[:12]}})
            notes.append(f"runtime {spec.runtime}: {len(code.files)} files, {code.size // 1024 + 1} KB packed, "
                         f"version {code.sha256[:12]}, stored in secret {spec.code_secret}")
        if spec.uses_login:
            spec = self._keep_live_access(spec)
            notes.append(f"DataOS sign-in inside the app (dapp_login); allowed: owners + {spec.access.users or '-'}"
                         f" roles {spec.access.roles or '-'}")
        elif spec.public:
            notes.append("public: anyone with the URL can open this app")
        app = render_app(spec, tenant=tenant, user_id=self.ctx.user_id, fqdn=self.ctx.fqdn,
                         compute=compute or (None if spec.compute else os.environ.get("DAPP_DEFAULT_COMPUTE")))
        return DeployPlan([app], notes, code)

    def _keep_live_access(self, spec: AppSpec) -> AppSpec:
        """`dapp access allow` edits the live app; a later deploy from dapp.yaml must not drop those people."""
        try:
            live = self.client.get_resource("app", spec.name)
        except DataOSError:
            live = None
        envs = live_envs(live or {})
        users = split_ids(envs.get(ALLOWED_USERS_ENV, ""))
        roles = split_ids(envs.get(ALLOWED_ROLES_ENV, ""))
        if not users and not roles:
            return spec
        access = spec.access.model_copy(update={
            "users": sorted(set(spec.access.users) | set(users)),
            "roles": sorted(set(spec.access.roles) | set(roles)),
        })
        return spec.model_copy(update={"access": access})

    def deploy(self, spec: AppSpec, *, compute: str | None = None, lint: bool = False,
               rotate_llm_key: bool = False) -> dict[str, Any]:
        plan = self.plan(spec, compute=compute)
        tenant = plan.manifests[0]["tenant"]
        manifests = list(plan.manifests)
        if spec.llm and not lint:
            existing = self.client.get_resource("secret", spec.llm_secret)
            if existing is None or rotate_llm_key:
                key = self._gateway().issue_key(spec.name, tenant, rotate=rotate_llm_key)["key"]
                manifests.insert(0, render_secret(
                    spec.llm_secret, tenant=tenant, app=spec.name,
                    description=f"dapp LLM gateway key for {spec.name}",
                    data={"DAPP_LLM_KEY": key},  # the one key the app holds; the real provider keys stay in the gateway
                ))
                plan.notes.append("issued a new LLM gateway key" if existing is None else "rotated LLM gateway key")
        if plan.code and not lint:
            manifests.insert(0, render_secret(spec.code_secret, tenant=tenant, app=spec.name, data=plan.code.data,
                                              description=f"dapp source code of {spec.name} "
                                                          f"({plan.code.sha256[:12]})"))
        if spec.uses_login and not lint and self.client.get_resource("secret", spec.oidc_secret) is None:
            manifests.insert(0, render_secret(
                spec.oidc_secret, tenant=tenant, app=spec.name,
                description=f"dapp sign-in secrets for {spec.name}",
                data={"clientSecret": pysecrets.token_urlsafe(32), "DAPP_SESSION_KEY": pysecrets.token_urlsafe(32)},
            ))
            plan.notes.append(f"created OIDC client secret {spec.oidc_secret}")
        output = self._write(*manifests, lint=lint)
        if spec.tools and not lint:
            plan.notes.extend(self._allow_app_on_tools(spec.name, spec.tools))
        app = manifests[-1]
        return {
            "app": spec.name,
            "tenant": tenant,
            "url": f"{self.ctx.base_url}{app['spec']['service']['http']['path']}/",
            "linted_only": lint,
            "writer": self.writer.name,
            "notes": plan.notes,
            "output": output,
        }

    # ---------- Claude Code: one terminal per person, deployed from the dashboard ----------
    def claude_instance(self) -> dict[str, Any]:
        """Your Claude Code: its app name and URL, whether it is deployed (and its state), and whether a Claude token is stored."""
        app = instance_name(self.ctx.user_id or "")
        live = self.client.get_resource("app", app)
        state = None
        if live is not None:
            try:
                state = self.status(app)["state"]
            except (PlatformError, DataOSError, KeyError):
                state = "unknown"
        return {"app": app, "exists": live is not None, "state": state, "url": f"{self.ctx.base_url}/{app}/",
                "token": self.client.get_resource("secret", token_secret(app)) is not None}

    def claude_instances(self) -> list[dict[str, Any]]:
        """Every Claude Code in the tenant that DataOS lets you list: who has one and in what state."""
        return [a for a in self.list_apps(everyone=True) if is_claude_instance(a)]

    def deploy_claude(self, d: ClaudeDeploy, *, compute: str, token: str = "") -> dict[str, Any]:
        """Deploy (or start again) your own Claude Code. The token, if given, becomes a DataOS secret only this app reads."""
        d.check()
        token = clean_token(token)
        me = self.ctx.user_id or ""
        app = instance_name(me)
        if self.client.get_resource("app", app) is not None:
            raise PlatformError(f"your Claude Code ({app}) is already deployed")
        if token:
            self._write(render_secret(token_secret(app), tenant=self.ctx.tenant, app=app, data={TOKEN_KEY: token},
                                      description=f"Claude token of {me} for {app}"))
        with_token = bool(token) or self.client.get_resource("secret", token_secret(app)) is not None
        done = self.deploy(claude_app_spec(app, me, d, compute=compute, fqdn=self.ctx.fqdn, with_token=with_token))
        return {"app": app, "url": done["url"], "token": with_token, "notes": done["notes"]}

    def set_claude_token(self, token: str) -> dict[str, Any]:
        """Store (or replace) your Claude token and attach it to your running Claude Code, which restarts to use it."""
        token = clean_token(token)
        if not token:
            raise PlatformError("paste the token that `claude setup-token` prints")
        app = instance_name(self.ctx.user_id or "")
        if self.client.get_resource("app", app) is None:
            self._write(render_secret(token_secret(app), tenant=self.ctx.tenant, app=app, data={TOKEN_KEY: token},
                                      description=f"Claude token of {self.ctx.user_id} for {app}"))
            return {"app": app, "attached": False}
        return {**self.set_secret(app, "anthropic", {TOKEN_KEY: token}, attach=True), "attached": True}

    def stop_claude(self) -> dict[str, Any]:
        """Remove the pod (and its cost). Files, conversations and the token stay; deploying again brings them back."""
        app = instance_name(self.ctx.user_id or "")
        return {"app": app, "output": self._delete_app(app)}

    def delete_claude(self) -> dict[str, Any]:
        """Stop it and delete its secrets too (the token and the sign-in). Your files stay in your immortal-obj objects."""
        app = instance_name(self.ctx.user_id or "")
        return self.delete(app, purge=True)

    # ---------- models (llama.cpp servers deployed from the dashboard) ----------
    def models(self) -> list[dict[str, Any]]:
        """Models callers in this tenant can use: the gateway's own and the ones people deployed."""
        return self._gateway().list_models(self.ctx.tenant)

    def deploy_model(self, d: ModelDeploy, *, compute: str, dataplane: str) -> dict[str, Any]:
        """Volume for the model file, a key for its server, the private app, and its name in the gateway. If a step fails the
        earlier ones are removed again, so a failed deploy leaves nothing behind."""
        d.check()
        tenant, gw = self.ctx.tenant, self._gateway()
        known = gw.list_models(tenant)
        if any(m["name"] == d.name for m in known):
            raise PlatformError(f"a model called {d.name} already exists")
        mine = [m for m in known if m.get("source") == "dashboard" and m.get("owner") == self.ctx.user_id]
        if len(mine) >= MAX_MODELS_PER_PERSON:
            raise PlatformError(f"you already run {len(mine)} models; delete one first (the limit is {MAX_MODELS_PER_PERSON})")
        if self.client.get_resource("app", d.app) is not None:
            raise PlatformError(f"an app called {d.app} already exists")
        made: list[tuple[str, str]] = []
        try:
            self._write(render_volume(d, tenant=tenant, dataplane=dataplane))
            made.append(("volume", d.volume))
            token = pysecrets.token_urlsafe(32)
            self._write(render_secret(d.secret, tenant=tenant, app=d.app, data={"LLAMA_API_KEY": token},
                                      description=f"API key of the model server {d.app}"))
            made.append(("secret", d.secret))
            self._wait_for("volume", d.volume)
            deployed = self.deploy(model_app_spec(d, compute=compute))
            made.append(("app", d.app))
            gw.register_model({"tenant": tenant, "name": d.name, "app": d.app, "url": f"http://{d.app}:8080", "key": token,
                               "repo": d.source, "description": f"{d.source} on CPU via llama.cpp"})
        except Exception:
            for kind, name in reversed(made):
                try:
                    self.writer.delete(kind, name)
                except Exception as e:  # best effort: the first error is the one to show
                    log.warning("could not remove %s %s after a failed model deploy: %s", kind, name, e)
            raise
        return {"name": d.name, "app": d.app, "volume": d.volume, "secret": d.secret, "source": d.source,
                "url": deployed["url"], "notes": deployed["notes"]}

    def delete_model(self, name: str) -> dict[str, Any]:
        """Stop routing to it, delete its app and secrets. The Volume goes when its pod has stopped (delete_model_volume)."""
        tenant, gw = self.ctx.tenant, self._gateway()
        m = next((m for m in gw.list_models(tenant) if m["name"] == name), None)
        if m is None:
            raise PlatformError(f"no model {name} in {tenant}")
        if m.get("source") != "dashboard":
            raise PlatformError(f"{name} comes from the gateway's own configuration; remove it where it was deployed")
        gw.unregister_model(tenant, name)                      # the gateway checks that you own it
        app = m["app"]
        out = self.delete(app, purge=True)
        return {"name": name, "app": app, "volume": f"{app}-data"[:60].rstrip("-"), "output": out["output"]}

    def delete_model_volume(self, volume: str, *, timeout: float = 240, every: float = 10) -> bool:
        """Delete a model's Volume once nothing mounts it any more (DataOS refuses while the pod is still stopping)."""
        deadline = time.monotonic() + timeout
        while True:
            try:
                self.writer.delete("volume", volume)
                if self.client.get_resource("volume", volume) is None:
                    return True
            except Exception as e:
                log.info("volume %s not deleted yet: %s", volume, e)
            if time.monotonic() >= deadline:
                return False
            time.sleep(every)

    def _wait_for(self, kind: str, name: str, *, timeout: float = 60, every: float = 2) -> None:
        """Poros creates some resources a moment after the apply returns; an app that mounts a Volume needs it to exist."""
        deadline = time.monotonic() + timeout
        while self.client.get_resource(kind, name) is None:
            if time.monotonic() >= deadline:
                raise PlatformError(f"{kind} {name} did not appear in {int(timeout)} s")
            time.sleep(every)

    # ---------- data pipelines (DataOS Nilus) ----------
    # Everything runs as the signed-in person. Access they lack is reported (PipelineAccessError), never granted here;
    # granting happens only in grant_depot_access, called when an owner or a tenant admin approves a request.
    def depots(self) -> list[dict[str, Any]]:
        return sorted((depot_summary(d) for d in self.client.list_resources("depot", details=True)),
                      key=lambda d: (not d["supported"], d["name"]))

    def depot(self, name: str) -> dict[str, Any]:
        d = self.client.get_resource("depot", name)
        if d is None:
            raise PlatformError(f"Depot {name} not found in tenant {self.ctx.tenant}")
        return d

    def pipelines(self, *, everyone: bool = True) -> list[dict[str, Any]]:
        owner = None if everyone else self.ctx.user_id
        rows = [pipeline_summary(r) for r in self.client.list_resources("nilus", owner=owner, details=True)]
        return sorted(rows, key=lambda r: (r["owner"] != self.ctx.user_id, r["name"] or ""))

    def pipeline(self, name: str) -> dict[str, Any]:
        r = self.client.get_resource("nilus", name)
        if r is None:
            raise PlatformError(f"no pipeline {name}")
        return r

    def pipeline_phase(self, name: str) -> str:
        try:
            return phase_of(self.client.runtime("nilus", name))
        except DataOSError:
            return "unknown"

    def plan_pipeline(self, req: PipelineRequest) -> dict[str, Any]:
        """The manifest and a one-line description, without applying anything."""
        source = self.depot(req.source_depot)
        dest = self.depot(req.dest_depot) if req.dest_depot and HOWS.get(req.how) and HOWS[req.how].type != "metadata" else None
        errs = check(req, source, dest)
        if errs:
            raise PlatformError("; ".join(errs))
        return {"manifest": build_manifest(req, source, dest, owner=self.ctx.user_id),
                "describe": describe(req, source, dest), "source": depot_summary(source),
                "dest": depot_summary(dest) if dest else None}

    def deploy_pipeline(self, req: PipelineRequest) -> dict[str, Any]:
        if self.client.get_resource("nilus", req.name) is not None:
            raise PlatformError(f"a pipeline named {req.name} already exists: pick another name, or open it and press Run again")
        plan = self.plan_pipeline(req)
        self._apply_pipeline(plan["manifest"])
        return {"name": req.name, "describe": plan["describe"]}

    def _apply_pipeline(self, manifest: dict[str, Any]) -> None:
        try:
            self._write(manifest)
        except (DataOSError, RuntimeError) as e:
            missing = missing_access(str(e))
            if missing:
                raise PipelineAccessError(missing, str(e)) from e
            raise

    def rerun_pipeline(self, name: str, *, timeout: float = 90, every: float = 3) -> dict[str, Any]:
        """Run it again now. Re-applying an unchanged pipeline does not run it (tested), so delete and create it again."""
        res = self.pipeline(name)
        if (res.get("spec") or {}).get("type") in ("cdc", "stream"):
            raise PlatformError("CDC and stream pipelines run continuously; there is nothing to run again")
        manifest = self._editable(res)
        self.client.delete_resource("nilus", name)
        deadline = time.monotonic() + timeout
        while self.client.get_resource("nilus", name) is not None:
            if time.monotonic() >= deadline:
                raise PlatformError(f"{name} was not removed within {int(timeout)} s; try again in a minute")
            time.sleep(every)
        self._apply_pipeline(manifest)
        return {"name": name}

    # ---- what a pipeline leaves behind, and removing it with the pipeline ----
    def _lakehouse_rel(self, depot: dict[str, Any]) -> str | None:
        """The Iceberg catalog's path on the DataOS host (e.g. /lh/engineering/s3lhspark), for a Lakehouse Depot served there."""
        rel = str(((depot.get("spec") or {}).get("spec") or {}).get("metastoreRelativePath") or "")
        return rel.rstrip("/") if depot_kind(depot) == "lakehouse" and rel.startswith("/lh/") else None

    @staticmethod
    def _table_parts(table: str) -> tuple[str, str] | None:
        ns, _, name = (table or "").partition(".")
        return (ns, name) if ns and name and "." not in name else None

    def lakehouse_table(self, depot_name: str, table: str) -> dict[str, Any]:
        """Does the table exist in the Lakehouse, and how big is it (rows, bytes)? Read-only."""
        depot = self.depot(depot_name)
        rel, parts = self._lakehouse_rel(depot), self._table_parts(table)
        if rel is None or parts is None:
            return {"exists": None, "why": "this destination can't be checked from here"}
        r = self.client._call("get", f"{rel}/v1/namespaces/{parts[0]}/tables/{parts[1]}")
        if r.status_code == 404:
            return {"exists": False}
        if not r.is_success:
            raise PlatformError(f"could not read table {table}: HTTP {r.status_code}")
        meta = r.json().get("metadata") or {}
        snaps = meta.get("snapshots") or []
        snap = next((x for x in snaps if x.get("snapshot-id") == meta.get("current-snapshot-id")), snaps[-1] if snaps else {})
        summ = snap.get("summary") or {}
        num = lambda k: int(summ[k]) if str(summ.get(k, "")).isdigit() else None
        return {"exists": True, "rows": num("total-records"), "bytes": num("total-files-size"), "files": num("total-data-files")}

    def pipeline_leftovers(self, name: str) -> dict[str, Any]:
        """What deleting this pipeline can also remove, and what must stay. Only things this person may remove are offered."""
        res = self.pipeline(name)
        row = pipeline_summary(res)
        mine = res.get("owner") == self.ctx.user_id
        others = [p for p in self.pipelines() if p["name"] != name]
        out: dict[str, Any] = {"name": name, "mine": mine, "table": None, "source": None, "runs_stay": True}
        if row["dest"] and row["dest_table"]:
            writers = [p["name"] for p in others if p["dest"] == row["dest"] and p["dest_table"] == row["dest_table"]]
            wrote = any(r["ok"] and r["dest"] == row["dest_table"] for r in _safe_list(lambda: self.pipeline_runs(name, limit=50)))
            info: dict[str, Any] = {"depot": row["dest"], "table": row["dest_table"], "shared_with": writers}
            try:
                info.update(self.lakehouse_table(row["dest"], row["dest_table"]))
            except (PlatformError, DataOSError) as e:
                info.update({"exists": None, "why": str(e)})
            info["can_drop"] = bool(mine and info.get("exists") and wrote and not writers)
            if info["exists"] and not info["can_drop"]:
                info["why"] = ("someone else owns this pipeline" if not mine else
                               f"pipeline(s) {', '.join(writers)} also write it" if writers else
                               "this pipeline has not written it successfully, so it is not ours to drop")
            out["table"] = info
        if row["source"]:
            try:
                d = self.depot(row["source"])
            except PlatformError:
                d = None
            if d and SOURCE_TAG in (d.get("tags") or []) and d.get("owner") == self.ctx.user_id:
                users = [p["name"] for p in others if row["source"] in (p["source"], p["dest"])]
                out["source"] = {"depot": row["source"], "shared_with": users, "can_remove": mine and not users}
        return out

    def delete_pipeline(self, name: str, *, drop_table: bool = False, remove_source: bool = False) -> dict[str, Any]:
        """Delete the pipeline; optionally also its destination table in the Lakehouse and the source connection dapp made for it.
        The extras are checked again here (not only in the page): the table must exist, this pipeline must have written it, nobody
        else may write it, and the caller must own the pipeline. The pipeline goes first, so nothing writes while the table goes."""
        left = self.pipeline_leftovers(name) if (drop_table or remove_source) else None
        if drop_table and not (left and left["table"] and left["table"]["can_drop"]):
            why = ((left or {}).get("table") or {}).get("why") or "it is not this pipeline's to drop"
            raise PlatformError(f"won't drop the table: {why}")
        if remove_source and not (left and left["source"] and left["source"]["can_remove"]):
            raise PlatformError("won't remove the source: it isn't one you connected, or other pipelines still use it")
        out: dict[str, Any] = {"name": name, "output": self.writer.delete("nilus", name), "table_dropped": None,
                               "source_removed": None, "problems": []}
        if drop_table and left:
            t = left["table"]
            try:
                out["table_dropped"] = self.drop_lakehouse_table(t["depot"], t["table"])
            except (PlatformError, DataOSError) as e:
                out["problems"].append(f"the pipeline is deleted, but its table {t['table']} could not be dropped: {e}")
        if remove_source and left:
            try:
                out["source_removed"] = self.delete_source(left["source"]["depot"])["name"]
            except (PlatformError, DataOSError) as e:
                out["problems"].append(f"the pipeline is deleted, but the source could not be removed: {e}")
        return out

    def drop_lakehouse_table(self, depot_name: str, table: str) -> str:
        """DROP TABLE with purge, through the Iceberg catalog, as the signed-in person (DataOS decides if they may)."""
        depot = self.depot(depot_name)
        rel, parts = self._lakehouse_rel(depot), self._table_parts(table)
        if rel is None or parts is None:
            raise PlatformError("this destination can't be dropped from here")
        r = self.client._call("delete", f"{rel}/v1/namespaces/{parts[0]}/tables/{parts[1]}", params={"purgeRequested": "true"})
        if r.status_code == 404:
            return f"{table} was already gone"
        if r.status_code in (401, 403):
            raise PlatformError(f"you may not drop {table} in {depot_name} (DataOS refused)")
        if not r.is_success:
            raise PlatformError(f"HTTP {r.status_code} from the catalog")
        return f"dropped {depot_name} {table}"

    def pipeline_logs(self, name: str, *, tail: int = 200) -> str:
        logs = self.client.logs("nilus", name, tail=tail)
        return logs if isinstance(logs, str) else flatten_logs(logs)

    def _nilus_manager(self, path: str, **params: Any) -> Any:
        r = self.client._call("get", f"/nilus-manager/api/v1/tenants/{self.ctx.tenant}{path}", params=params)
        if r.status_code == 404:
            return None
        if not r.is_success:
            raise PlatformError(f"Nilus Manager: HTTP {r.status_code}: {r.text[:200]}")
        return r.json()

    def pipeline_runs(self, name: str, *, limit: int = 20) -> list[dict[str, Any]]:
        body = self._nilus_manager(f"/pipelines/{resource_id(name)}/runs", limit=limit) or {}
        return [run_row(r) for r in body.get("pipelines") or []]

    def pipeline_stats(self, name: str, *, period: str = "7d") -> dict[str, Any] | None:
        body = self._nilus_manager(f"/pipelines/{resource_id(name)}/stats", period=period) or {}
        return body.get("stats")

    # ---------- governed access to Depots (for pipelines) ----------
    def can_grant(self, depot: dict[str, Any]) -> bool:
        """The Depot's owner, or a tenant admin, may approve someone's request to use it."""
        me = self.ctx.user_id
        owners = {depot.get("owner"), *(((depot.get("spec") or {}).get("owners")) or [])}
        if me in owners:
            return True
        try:
            tags = self.client.whoami().get("tags") or []
        except DataOSError:
            return False
        return f"roles:id:{self.ctx.tenant}-tenant-admin" in tags

    def _access_grants(self, depot_name: str, user: str, secret_ids: list[str], note: str, *,
                       depot: bool = True) -> list[dict[str, Any]]:
        """DataOS grants letting ONE person use ONE Depot (use-specific-depot) and its Secrets (use-specific-secret)."""
        who = subject_for(user)
        safe = "".join(c if c.isalnum() else "-" for c in user.split(":")[-1].lower())[:24]
        tags = ["governance", "grant", DAPP_TAG, "dapp-depot-access", f"dapp-depot-{depot_name}"]
        base = {"version": "v2alpha", "type": "grant", "tenant": self.ctx.tenant, "tags": tags, "description": note[:240]}
        tail = pysecrets.token_hex(2)
        grants = []
        if depot:
            grants.append({**base, "name": f"dapp-depot-{depot_name}-{safe}-{tail}"[:63].rstrip("-"),
                           "grant": {"policy_use_case_id": "use-specific-depot", "subjects": [who],
                                     "values": {"depot-access": [{"depot": depot_name}]}, "collection": self.ctx.tenant,
                                     "requester": self.ctx.user_id or "unknown", "notes": note, "approve": True}})
        if secret_ids:
            grants.append({**base, "name": f"dapp-depotsec-{depot_name}-{safe}-{tail}"[:63].rstrip("-"),
                           "grant": {"policy_use_case_id": "use-specific-secret", "subjects": [who],
                                     "values": {"read-specific-secret-governance": [{"id": s} for s in secret_ids]},
                                     "collection": "system", "requester": self.ctx.user_id or "unknown", "notes": note,
                                     "approve": True}})
        return grants

    def grant_depot_access(self, depot_name: str, user: str, *, reason: str = "") -> dict[str, Any]:
        """Let ONE person use ONE Depot and the Secrets behind it (Can Use), as DataOS grants. Only for an approved request:
        the caller must own the Depot or be a tenant admin. Never use-any-depots / use-any-secret."""
        depot = self.depot(depot_name)
        if not self.can_grant(depot):
            raise PlatformError(f"only the owner of Depot {depot_name} or a tenant admin can grant access to it")
        note = f"dapp: {user} may use Depot {depot_name}" + (f" ({reason[:120]})" if reason else "") + \
               f", approved by {self.ctx.user_id}"
        secrets_ = depot_secrets(depot)
        grants = self._access_grants(depot_name, user, secrets_, note)
        self._write(*grants)
        return {"depot": depot_name, "user": user, "grants": [g["name"] for g in grants], "secrets": secrets_}

    # ---------- connecting a new source: a Secret with the credentials, a Depot on top (both the caller's) ----------
    def source_types(self) -> dict[str, SourceType]:
        r = self.client._call("get", "/ds/api/v2/types")
        if not r.is_success:
            raise PlatformError(f"could not read the Depot types from DataOS: HTTP {r.status_code}")
        return parse_types(r.json())

    def sources(self) -> list[dict[str, Any]]:
        """The sources this person connected with dapp (Depots tagged dapp-source that they own)."""
        rows = self.client.list_resources("depot", owner=self.ctx.user_id, tags=[SOURCE_TAG], details=True)
        return [{**depot_summary(d), "secret": next(iter(depot_secrets(d)), None), "created": d.get("created")}
                for d in rows if d.get("owner") in (None, self.ctx.user_id)]

    def _own_source(self, name: str) -> dict[str, Any]:
        d = self.depot(name)
        if SOURCE_TAG not in (d.get("tags") or []) or d.get("owner") != self.ctx.user_id:
            raise PlatformError(f"{name} is not a source you connected with dapp")
        return d

    def add_source(self, req: SourceRequest, *, patience: float = 60, every: float = 3) -> dict[str, Any]:
        """Secret (credentials) -> Can Use on it for yourself -> Depot -> Can Use on it for yourself. All as the caller,
        on resources the caller creates and owns (DataOS requires the self-grant, and audits it). On any failure the
        pieces already made are removed, and the message has the credentials taken out."""
        stype = self.source_types().get(req.type)
        errs = check_source(req, stype)
        if errs:
            raise PlatformError("; ".join(errs))
        assert stype is not None
        if self.client.get_resource("depot", req.name) is not None:
            raise PlatformError(f"a Depot named {req.name} already exists: pick another name")
        existing = self.client.get_resource("secret", req.secret_name)
        if existing is not None and existing.get("owner") != self.ctx.user_id:
            raise PlatformError(f"a Secret named {req.secret_name} belongs to someone else: pick another name")
        api = ApiWriter(self.client)             # never dataos-ctl here: its output could echo the Secret
        tenant, me = self.ctx.tenant, self.ctx.user_id or ""
        note = f"dapp: {me} connected source {req.name} and may use it"
        made: list[tuple[str, str]] = []
        try:
            api.apply(secret_manifest(req, tenant))
            made.append(("secret", req.secret_name))
            for g in self._access_grants(req.name, me, [f"{tenant}:{req.secret_name}"], note, depot=False):
                api.apply(g)
                made.append(("grant", g["name"]))
            depot = depot_manifest(req, stype, tenant)
            deadline = time.monotonic() + patience
            while True:                             # a fresh grant can take a few seconds to count
                try:
                    api.apply(depot)
                    break
                except DataOSError as e:
                    if "not-allowed" not in str(e) or time.monotonic() >= deadline:
                        raise
                    time.sleep(every)
            made.append(("depot", req.name))
            for g in self._access_grants(req.name, me, [], note):
                api.apply(g)
                made.append(("grant", g["name"]))
        except Exception as e:
            for kind, name in reversed(made):
                try:
                    self.client.delete_resource(kind, name)
                except DataOSError:
                    log.warning("could not remove %s %s after a failed connect", kind, name)
            raise PlatformError(f"Could not connect {req.name}: {redact(str(e), req)}") from None
        return {"name": req.name, "type": stype.name, "secret": req.secret_name,
                "grants": [n for k, n in made if k == "grant"]}

    def replace_source_credentials(self, name: str, secret: dict[str, Any]) -> dict[str, Any]:
        d = self._own_source(name)
        req = SourceRequest(name=name, type=depot_kind(d), secret=secret)
        stype = self.source_types().get(req.type)
        missing = [f.key for f in (stype.fields if stype else ()) if f.side == "secret" and f.required
                   and not (req.secret.get(f.key) and req.secret[f.key].get_secret_value())]
        if missing:
            raise PlatformError(f"give all the credentials again: {', '.join(missing)} missing")
        try:
            ApiWriter(self.client).apply(secret_manifest(req, self.ctx.tenant))
        except DataOSError as e:
            raise PlatformError(redact(str(e), req)) from None
        return {"name": name, "secret": req.secret_name}

    def delete_source(self, name: str, *, keep_secret: bool = False) -> dict[str, Any]:
        """Remove the Depot, its Secret (the credentials) and the dapp grants on it. Pipelines using it stop working."""
        self._own_source(name)
        users = [p["name"] for p in self.pipelines() if name in (p.get("source"), p.get("dest"))]
        self.client.delete_resource("depot", name)
        removed = []
        for g in self.depot_grants(name):
            if self.client.delete_resource("grant", g["name"]):
                removed.append(g["name"])
        secret = f"dapp-src-{name}"
        gone = False if keep_secret else self.client.delete_resource("secret", secret)
        return {"name": name, "secret_deleted": gone, "grants_removed": removed, "pipelines_affected": users}

    def depot_grants(self, depot_name: str) -> list[dict[str, Any]]:
        out = []
        for g in self.client.list_resources("grant", tags=[f"dapp-depot-{depot_name}"], details=True):
            spec = g.get("spec") or g.get("grant") or {}
            out.append({"name": g.get("name"), "use_case": spec.get("policy_use_case_id"),
                        "subjects": spec.get("subjects") or [], "description": g.get("description")})
        return out

    def revoke_depot_grant(self, depot_name: str, grant: str) -> dict[str, Any]:
        if not self.can_grant(self.depot(depot_name)):
            raise PlatformError(f"only the owner of Depot {depot_name} or a tenant admin can take access away")
        if not any(g["name"] == grant for g in self.depot_grants(depot_name)):
            raise PlatformError(f"{grant} is not a dapp grant on Depot {depot_name}")
        return {"grant": grant, "output": self.writer.delete("grant", grant)}

    def scale(self, name: str, replicas: int) -> dict[str, Any]:
        if replicas < 1:
            raise PlatformError("replicas must be >= 1 (use `dapp rm` to remove an app)")
        app = self._editable(self.get_app(name))
        app["spec"]["replicas"] = replicas
        return {"app": name, "replicas": replicas, "output": self._write(app)}

    def _delete_app(self, name: str, *, patience: float = 75, every: float = 5) -> str:
        """Delete an app. DataOS grants its creator the right to delete it a short while after creating it, so a delete right after a
        deploy can get a 403 that goes away by itself; for a young app wait for it instead of telling the person they have no permission."""
        from datetime import UTC, datetime

        deadline = time.monotonic() + patience
        while True:
            try:
                return self.writer.delete("app", name)
            except DataOSError as e:
                if e.status != 403 or time.monotonic() >= deadline:
                    raise
                made = ((self.client.get_resource("app", name) or {}).get("createdAt") or "")
                try:
                    age = (datetime.now(UTC) - datetime.fromisoformat(made)).total_seconds()
                except ValueError:
                    raise e from None
                if age > 300:                      # not a new app: this really is a missing permission
                    raise
                time.sleep(every)

    def delete(self, name: str, *, purge: bool = False) -> dict[str, Any]:
        out = [self._delete_app(name)]
        if purge:
            for s in self.list_secrets(name):
                out.append(self.writer.delete("secret", s["name"]))
            for g in self.list_access(name):
                out.append(self.writer.delete("grant", g["name"]))
            try:
                self._gateway().revoke_keys(name, self.ctx.tenant)
                out.append("revoked LLM gateway keys")
            except Exception as e:  # gateway may not be installed
                out.append(f"LLM keys not revoked: {e}")
        return {"app": name, "purged": purge, "output": "\n".join(out)}

    # ---------- secrets ----------
    def list_secrets(self, app: str) -> list[dict[str, Any]]:
        """Secrets dapp created for the app plus any the app references. Values are never returned."""
        tagged = {s["name"]: s for s in self.client.list_resources("secret", tags=[app_tag(app)])}
        try:
            refs = (self.get_app(app).get("spec") or {}).get("service", {}).get("secrets") or []
        except PlatformError:
            refs = []
        out = [{"name": n, "owner": s.get("owner"), "managed_by_dapp": True, "referenced": False}
               for n, s in tagged.items()]
        for r in refs:
            sid = r.get("id", "")
            short = sid.split(":", 1)[-1]
            row = next((o for o in out if o["name"] == short), None)
            if row:
                row["referenced"] = True
            else:
                out.append({"name": sid, "owner": None, "managed_by_dapp": False, "referenced": True})
        return out

    def set_secret(self, app: str, secret: str, data: dict[str, str], *, attach: bool = True) -> dict[str, Any]:
        """Create/replace a key-value secret (all keys; DataOS can't merge unseen values) and attach it."""
        if not data:
            raise PlatformError("no KEY=VALUE pairs given")
        if not secret.startswith("dapp-"):
            secret = f"dapp-{app}-{secret}"  # so the owner's single dapp-** read grant covers it
        tenant = self.ctx.tenant
        manifests = [render_secret(secret, tenant=tenant, data=data, app=app,
                                   description=f"dapp secret for {app}")]
        if attach:
            a = self._editable(self.get_app(app))
            svc = a["spec"].setdefault("service", {})
            ids = [s.get("id") for s in svc.get("secrets") or []]
            if secret not in ids and f"{tenant}:{secret}" not in ids:
                svc.setdefault("secrets", []).append({"id": secret})
                manifests.append(a)
        out = self._write(*manifests)
        return {"app": app, "secret": secret, "keys": sorted(data), "attached": attach, "output": out}

    def delete_secret(self, app: str, secret: str) -> dict[str, Any]:
        a = self._editable(self.get_app(app))
        svc = a["spec"].get("service", {})
        before = svc.get("secrets") or []
        after = [s for s in before if s.get("id") not in (secret, f"{self.ctx.tenant}:{secret}")]
        out = []
        if len(after) != len(before):
            svc["secrets"] = after
            out.append(self._write(a))
        out.append(self.writer.delete("secret", secret))
        return {"app": app, "secret": secret, "output": "\n".join(out)}

    def apply_secret_file(self, path: str, env: dict[str, str], *, dry_run: bool = False) -> dict[str, Any]:
        """Apply secret manifests whose values are ${VAR} references, never literals in the file.

        Refuses unset or empty variables (dataos-ctl would silently write "" and wipe a working key) and files
        that still hold <placeholders> (the *.generated.yaml shapes). Returns names and key names, never values.
        """
        import yaml

        with open(path) as f:
            docs = [d for d in yaml.safe_load_all(f) if d]  # comments are gone after parsing
        problems: list[str] = []

        def fill(value: Any, where: str) -> Any:
            if isinstance(value, dict):
                return {k: fill(v, f"{where}.{k}") for k, v in value.items()}
            if isinstance(value, list):
                return [fill(v, where) for v in value]
            if not isinstance(value, str):
                return value
            if value.startswith("<") and value.endswith(">"):
                problems.append(f"{where} is a placeholder ({value[:40]}): this file is a shape, dapp creates it")
            for var in VAR_RE.findall(value):
                if not env.get(var):
                    problems.append(f"{where} needs ${{{var}}}, which is unset or empty")
            return VAR_RE.sub(lambda m: env.get(m.group(1), ""), value)

        filled = [fill(d, d.get("name", "?")) for d in docs]
        bad = [d.get("name") for d in filled if d.get("type") != "secret"]
        if bad:
            problems.append(f"only type: secret is accepted here, got {bad}")
        if problems:
            raise PlatformError("not applied:\n  " + "\n  ".join(problems))
        out = "dry run: nothing applied" if dry_run else self._write(*filled)
        return {"secrets": [{"name": d["name"], "tenant": d.get("tenant"),
                             "keys": sorted((d.get("spec") or {}).get("data") or {})} for d in filled],
                "applied": not dry_run, "output": out}

    # ---------- access (app-level allow-list, enforced by dapp_login) ----------
    def allowlist(self, app: str) -> dict[str, Any]:
        a = self.get_app(app)
        spec = a.get("spec") or {}
        envs = live_envs(a)
        login = envs.get("DAPP_AUTH") == "oidc"
        public = bool(((spec.get("service") or {}).get("http") or {}).get("noNetworkAuthorization")) and not login
        return {
            "app": app,
            "mode": "login" if login else ("public" if public else "gateway"),
            "owners": (spec.get("app") or {}).get("owners") or [],
            "users": split_ids(envs.get(ALLOWED_USERS_ENV, "")),
            "roles": split_ids(envs.get(ALLOWED_ROLES_ENV, "")),
        }

    def resolve_users(self, typed: list[str]) -> tuple[list[str], dict[str, str]]:
        """Turn what people typed into real DataOS user ids: an id in any letter case, or an email. Anything that
        matches nobody raises with the closest ids, so a typo can't silently "grant" access to a person who
        doesn't exist. If DataOS won't let us read the user list we can't check, and accept what was typed."""
        names = [t for t in typed if t != "*"]
        if not names:
            return list(typed), {}
        try:
            people = self.client.list_users()
        except DataOSError:
            return list(typed), {}
        by_id = {u["id"].lower(): u["id"] for u in people}
        by_email = {u["email"].lower(): u["id"] for u in people if u.get("email")}
        ids: list[str] = []
        changed: dict[str, str] = {}
        problems: list[str] = []
        for t in typed:
            if t == "*":
                ids.append(t)
                continue
            key = t.lower()
            found = by_id.get(key) or by_email.get(key)
            if found:
                ids.append(found)
                if found != t:
                    changed[t] = found
                continue
            near = difflib.get_close_matches(key, list(by_id), n=3, cutoff=0.6)
            hint = f" Did you mean: {', '.join(by_id[n] for n in near)}?" if near else ""
            problems.append(f"'{t}' is not a DataOS user.{hint}")
        if problems:
            raise PlatformError("not added: " + " ".join(problems))
        return ids, changed

    def allow(self, app: str, users: list[str] = (), roles: list[str] = (), *, verify: bool = True) -> dict[str, Any]:
        users, resolved = self.resolve_users(list(users)) if verify and users else (list(users), {})
        return {**self._edit_allowlist(app, add_users=users, add_roles=roles), "resolved": resolved}

    def deny(self, app: str, users: list[str] = (), roles: list[str] = ()) -> dict[str, Any]:
        return self._edit_allowlist(app, del_users=users, del_roles=roles)

    def _edit_allowlist(self, app: str, add_users=(), add_roles=(), del_users=(), del_roles=()) -> dict[str, Any]:
        a = self._editable(self.get_app(app))
        svc = a["spec"].setdefault("service", {})
        envs = svc.setdefault("envs", {})
        if envs.get("DAPP_AUTH") != "oidc":
            raise PlatformError(f"{app} doesn't use dapp sign-in (login: true); its access can't be listed here")
        owners = set((a["spec"].get("app") or {}).get("owners") or [])
        if owners & set(del_users):
            raise PlatformError(f"owners always have access: {', '.join(sorted(owners & set(del_users)))}")
        users = (set(split_ids(envs.get(ALLOWED_USERS_ENV, ""))) | set(add_users)) - set(del_users)
        roles = (set(split_ids(envs.get(ALLOWED_ROLES_ENV, ""))) | set(add_roles)) - set(del_roles)
        envs[ALLOWED_USERS_ENV] = ",".join(sorted(users | owners))
        envs[ALLOWED_ROLES_ENV] = ",".join(sorted(roles))
        out = self._write(a)
        return {**self.allowlist_from(a), "output": out, "note": "the app restarts to pick up the change"}

    @staticmethod
    def allowlist_from(a: dict[str, Any]) -> dict[str, Any]:
        envs = live_envs(a)
        return {"app": a.get("name"), "users": split_ids(envs.get(ALLOWED_USERS_ENV, "")),
                "roles": split_ids(envs.get(ALLOWED_ROLES_ENV, ""))}

    # ---------- Heimdall grants (advanced) ----------
    def list_access(self, app: str) -> list[dict[str, Any]]:
        grants = self.client.list_resources("grant", tags=[app_tag(app)], details=True)
        rows = []
        for g in grants:
            spec = g.get("spec") or g.get("grant") or {}
            rows.append({
                "name": g.get("name"),
                "use_case": spec.get("policy_use_case_id"),
                "subjects": spec.get("subjects"),
                "values": spec.get("values"),
                "status": g.get("status"),
            })
        return rows

    def grant_access(self, app: str, user: str, *, use_case: str,
                     values: dict[str, list[dict[str, str]]] | None = None) -> dict[str, Any]:
        self.get_app(app)
        safe_user = "".join(c if c.isalnum() else "-" for c in user.split(":")[-1].lower())[:30]
        name = f"dapp-{app}-{safe_user}-{pysecrets.token_hex(2)}"
        grant = render_grant(
            name=name, tenant=self.ctx.tenant, use_case=use_case, subjects=[subject_for(user)],
            values=values or {}, requester=self.ctx.user_id or "unknown",
            notes=f"dapp: {user} gets {use_case} on app {app}", app=app,
        )
        return {"grant": name, "output": self.writer.apply(grant)}

    def revoke_access(self, grant_name: str) -> dict[str, Any]:
        return {"grant": grant_name, "output": self.writer.delete("grant", grant_name)}

    # ---------- LLM gateway keys ----------
    def llm_keys(self) -> list[dict[str, Any]]:
        """The caller's gateway keys: prefix, app, state. The key itself can't be shown, the gateway keeps a hash."""
        return self._gateway().list_keys()

    def personal_llm_key(self, *, rotate: bool = False) -> dict[str, Any]:
        """Your own gateway key (app user:<you>), for calling the LLMs from outside DataOS."""
        r = self._gateway().issue_personal_key(self.ctx.tenant, rotate=rotate)
        return {**r, "base_url": f"{gateway_public_url(self.ctx)}/openai/v1"}

    def _llm_secret_of(self, app: str) -> str:
        a = self.get_app(app)
        want = llm_secret_name(app)
        ids = [s.get("id", "") for s in ((a.get("spec") or {}).get("service") or {}).get("secrets") or []]
        if not any(i.split(":")[-1] == want for i in ids):
            raise PlatformError(f"{app} doesn't use the LLM gateway: set `llm: true` in its dapp.yaml and deploy")
        return want

    def _app_pods(self, app: str) -> set[str]:
        nodes = (self.client.runtime("app", app).get("nodeDetails") or [])
        return {n["name"] for n in nodes if n.get("type") == "cgroup-stateless" and n.get("name")}

    def start_llm_rotation(self, app: str) -> dict[str, Any]:
        """Step 1: issue a new key and store it in the app's secret. The old keys stay valid for now, because the
        running pod still has one until DataOS restarts the app (it does, by itself, when the secret changes)."""
        secret = self._llm_secret_of(app)
        tenant = self.ctx.tenant
        old = [k["prefix"] for k in self.llm_keys() if k["app"] == app and k["tenant"] == tenant and not k["revoked"]]
        before = sorted(self._app_pods(app))
        issued = self._gateway().issue_key(app, tenant, rotate=False)
        out = self._write(render_secret(secret, tenant=tenant, app=app, data={"DAPP_LLM_KEY": issued["key"]},
                                        description=f"dapp LLM gateway key for {app}"))
        return {"app": app, "new_key_prefix": issued["prefix"], "secret": secret, "old_prefixes": old,
                "before_pods": before, "output": out}

    def finish_llm_rotation(self, app: str, old_prefixes: list[str], before_pods: list[str], *,
                            timeout: float = 150, poll: float = 5) -> dict[str, Any]:
        """Step 2: once every old pod is gone (so nothing still uses an old key), revoke the old keys."""
        deadline = time.monotonic() + timeout
        rolled = False
        while time.monotonic() < deadline:
            now = self._app_pods(app)
            if now and not (now & set(before_pods)):
                rolled = True
                break
            time.sleep(poll)
        revoked: list[str] = []
        if rolled:
            for prefix in old_prefixes:
                try:
                    self.revoke_llm_key(app, prefix)
                    revoked.append(prefix)
                except Exception as e:  # already revoked is fine
                    log.warning("could not revoke %s for %s: %s", prefix, app, e)
        return {"app": app, "rolled_over": rolled, "revoked": revoked,
                "still_active": [] if rolled else old_prefixes}

    def rotate_llm_key(self, app: str, *, timeout: float = 150) -> dict[str, Any]:
        """New key in, app restarted by DataOS, then old keys revoked: no failed calls in between."""
        started = self.start_llm_rotation(app)
        done = self.finish_llm_rotation(app, started["old_prefixes"], started["before_pods"], timeout=timeout)
        return {**{k: started[k] for k in ("app", "new_key_prefix", "secret")}, **done}

    def revoke_llm_key(self, app: str, prefix: str) -> dict[str, Any]:
        """Kill one key. If it is the app's current one the app gets 401s until the key is rotated."""
        return {"app": app, **self._gateway().revoke_key(app, self.ctx.tenant, prefix)}

    def list_users(self) -> list[dict[str, str]]:
        return self.client.list_users()

    # ---------- API keys (DataOS) ----------
    def list_keys(self) -> list[dict[str, Any]]:
        return self.client.list_apikeys()

    def create_key(self, name: str, duration: str = "720h", description: str = "") -> dict[str, Any]:
        return self.client.create_apikey(name, duration=duration, description=description)

    def delete_key(self, name: str, *, force: bool = False) -> None:
        mine = {k["name"]: k for k in self.client.list_apikeys()}
        if name not in mine:
            raise PlatformError(f"no API key named {name}")
        if mine[name].get("system") and not force:
            raise PlatformError(f"{name} was created by DataOS to run your apps as you; deleting it breaks them "
                                "(pass force only if you know it is unused)")
        self.client.delete_apikey(name)

    # ---------- usage ----------
    def compute_usage(self, app: str) -> dict[str, Any]:
        url = os.environ.get("DAPP_PROMETHEUS_URL") or observability_url(self.ctx.base_url)
        if app.startswith("user:"):  # a personal LLM key: calls from a laptop, no pods in DataOS
            return {"note": "personal LLM key: no compute in DataOS"}
        service = self.service_name(app)
        if not service:
            return {"error": f"{app} has no running pods, so its compute can't be told apart from same-named apps"}
        return Prometheus(url, headers=self.ctx.auth_headers()).app_now(app, self.ctx.tenant, service)

    def service_name(self, app: str) -> str | None:
        """The DataOS service behind your app; metrics are filtered by it (job_name alone is shared by same-named
        apps of other people)."""
        try:
            return service_of(self.client.runtime("app", app))
        except DataOSError:
            return None

    def llm_usage(self, app: str | None = None, since_hours: int = 24 * 30,
                  group_by: str = "app,model") -> dict[str, Any]:
        """Tokens and cost. group_by can include end_user: the signed-in people who used the app."""
        return self._gateway().summary(app=app, since_hours=since_hours, group_by=group_by)

    # ---------- MCP tools (through the gateway) ----------
    def tools(self) -> list[dict[str, Any]]:
        """Tools you own or may use, in this tenant."""
        return self._gateway().list_tools(self.ctx.tenant)

    def tool(self, name: str) -> dict[str, Any]:
        return self._gateway().get_tool(self.ctx.tenant, name)

    def tool_url(self, name: str) -> str:
        return f"{gateway_public_url(self.ctx)}/mcp/{self.ctx.tenant}/{name}"

    def save_tool(self, name: str, *, url: str, description: str = "", auth_header: str = "",
                  auth_value: str | None = None, allow: list[str] | None = None) -> dict[str, Any]:
        """Register an MCP server (or update yours). auth_value goes to the gateway only, encrypted there."""
        if allow is not None:
            allow = self._tool_ids(allow)
        if auth_value and not auth_header and not auth_value.startswith(("Bearer ", "Basic ")):
            auth_value = f"Bearer {auth_value}"   # a bare token goes in Authorization: Bearer <token>
        if auth_value is not None and not auth_header:
            auth_header = "Authorization" if auth_value else ""
        body = {"tenant": self.ctx.tenant, "name": name, "url": url, "description": description,
                "auth_header": auth_header, "auth_value": auth_value, "allow": allow}
        return {**self._gateway().save_tool(body), "mcp_url": self.tool_url(name)}

    def delete_tool(self, name: str) -> dict[str, Any]:
        return self._gateway().delete_tool(self.ctx.tenant, name)

    def tool_allow(self, name: str, who: list[str]) -> dict[str, Any]:
        """who: DataOS user ids or emails (checked against Heimdall), "*" for every DataOS user, app:<name>."""
        return self._gateway().tool_access(self.ctx.tenant, name, self._tool_ids(who), [])

    def tool_deny(self, name: str, who: list[str]) -> dict[str, Any]:
        return self._gateway().tool_access(self.ctx.tenant, name, [], list(who))

    def tool_usage(self, name: str | None = None, *, since_hours: int = 24 * 30,
                   group_by: str = "tool,caller") -> dict[str, Any]:
        return self._gateway().tool_usage(tool=name, tenant=self.ctx.tenant, since_hours=since_hours,
                                          group_by=group_by)

    def _allow_app_on_tools(self, app: str, tools: list[str]) -> list[str]:
        """Let the app call each tool. Works for tools you own; for someone else's, say who has to do it."""
        notes = []
        for t in tools:
            if t == OBJECTS_TOOL:
                continue                            # open to every app (app:*); each app only sees its own objects
            try:
                self._gateway().tool_access(self.ctx.tenant, t, [f"app:{app}"], [])
                notes.append(f"tool {t}: app:{app} allowed")
            except Exception as e:
                notes.append(f"tool {t}: could not allow app:{app} ({e}); its owner must run "
                             f"`dapp tool allow {t} app:{app}`")
        return notes

    def _tool_ids(self, who: list[str]) -> list[str]:
        people = [w for w in who if w != "*" and not w.startswith("app:")]
        ids, _ = self.resolve_users(people) if people else ([], {})
        return [w for w in who if w == "*" or w.startswith("app:")] + ids

    # ---------- helpers ----------
    def _gateway(self) -> GatewayUsage:
        return GatewayUsage(gateway_public_url(self.ctx), self.ctx.auth_headers())

    def _write(self, *manifests: dict[str, Any], lint: bool = False) -> str:
        """The one place manifests leave dapp: env values get quoted for the DataOS template here."""
        return self.writer.apply(*[wire_manifest(m) for m in manifests], lint=lint)

    @staticmethod
    def _editable(res: dict[str, Any]) -> dict[str, Any]:
        """Strip server-side fields so the resource can be re-applied."""
        keep = ("name", "version", "type", "tenant", "tags", "description", "title", "owner", "spec")
        out = copy.deepcopy({k: res[k] for k in keep if k in res})
        out.setdefault("spec", {})
        service = out["spec"].get("service") or {}
        if service.get("envs"):  # edit plain values; _write quotes them again on the way out
            service["envs"] = {k: yaml_decode(v) for k, v in service["envs"].items()}
        return out
