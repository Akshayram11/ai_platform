"""The dapp.yaml app spec and the DataOS manifests generated from it.

dapp.yaml is deliberately small. It renders into the existing DataOS ``app`` resource (v1alpha, defined
in dataos-component-install/installs/dataproduct-stack-shell/components/app-domain-definition.yaml), which
Poros turns into a ``service`` named ``<name>svc`` (Deployment + Service + Kong ingress + HPA).
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

DAPP_TAG = "dapp"
NAME_RE = re.compile(r"^[a-z]([a-z0-9-]{0,46}[a-z0-9])?$")

# Secret that holds the per-app LLM gateway key; projected as env vars into the app.
LLM_SECRET_SUFFIX = "-llm"
# Every secret dapp creates is named dapp-<app>-<purpose>, so one grant (read engineering:dapp-**) covers them all.
SECRET_PREFIX = "dapp-"
# Secret with the app's OIDC client secret (key `clientSecret`, what Poros reads) and session key.
OIDC_SECRET_SUFFIX = "-oidc"
RUNTIMES = ("python-3.12",)
RUNTIME_REVISION = "r3"   # bump with each runtime image build (runtimes/python.Dockerfile)
RUNTIME_RE = re.compile(r"^(" + "|".join(re.escape(r) for r in RUNTIMES) + r")$")
RUNTIME_COMMAND = ["python", "/opt/dapp/dapp_run.py"]
METRICS_PORT = 9102
METRICS_PATH = "/metrics"
ALLOWED_USERS_ENV = "DAPP_ALLOWED_USERS"
# Injected by the app template into every container (defaultProjections); defining them again breaks the render.
RESERVED_ENV = {
    "DATAOS_RUN_AS_APIKEY", "DATAOS_RUN_AS_USER", "DATAOS_TENANT_ID", "HEIMDALL_URL", "DATAOS_RESOURCE_ID",
    "DATAOS_RUN_ID", "DEPOT_SERVICE_URL", "POROS_SERVICE_URL", "COLLATION_SERVICE_URL",
}
ALLOWED_ROLES_ENV = "DAPP_ALLOWED_ROLES"


def yaml_plain(value: str) -> str:
    """The app template pastes env values into YAML unquoted, so `*,x`, `true`, `a: b` or `#x` would break the
    render or change type. Anything that doesn't survive as a plain scalar goes in as a double-quoted one."""
    try:
        if yaml.safe_load(f"k: {value}") == {"k": value}:
            return value
    except yaml.YAMLError:
        pass
    return json.dumps(value)


def yaml_decode(raw: object) -> str:
    """What the container actually sees for an env value stored on a live app (inverse of yaml_plain)."""
    text = "" if raw is None else str(raw)
    try:
        loaded = yaml.safe_load(f"k: {text}")
    except yaml.YAMLError:
        return text
    value = loaded.get("k") if isinstance(loaded, dict) else text
    return "" if value is None else str(value)


def wire_manifest(manifest: dict[str, Any]) -> dict[str, Any]:
    """The manifest as submitted to DataOS: env values quoted where the unquoted YAML template needs it.
    Everything inside dapp works with plain values; this is the only place they get quoted."""
    if manifest.get("type") != "app":
        return manifest
    out = {**manifest, "spec": {**(manifest.get("spec") or {})}}
    service = {**(out["spec"].get("service") or {})}
    if service.get("envs"):
        service["envs"] = {k: yaml_plain(str(v)) for k, v in service["envs"].items()}
        out["spec"]["service"] = service
    return out


def llm_secret_name(app: str) -> str:
    return f"{SECRET_PREFIX}{app}{LLM_SECRET_SUFFIX}"


def app_tag(name: str) -> str:
    return f"dapp-app-{name}"


def service_name(app_name: str) -> str:
    """The app domain template names the generated service ``<app>svc``."""
    return f"{app_name}svc"


class Resources(BaseModel):
    model_config = ConfigDict(extra="forbid")
    cpu: str = "100m"
    memory: str = "128Mi"
    cpu_limit: str = Field("1", alias="cpuLimit")
    memory_limit: str = Field("512Mi", alias="memoryLimit")


class AutoScale(BaseModel):
    model_config = ConfigDict(extra="forbid")
    min: int = 1
    max: int = 3
    cpu: int = 80
    memory: int | None = None


class WorkingDir(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str
    directory: str


class Access(BaseModel):
    """Who may open the app (enforced in the app by dapp_login). Owners are always allowed."""

    model_config = ConfigDict(extra="forbid")
    users: list[str] = Field(default_factory=list, description='DataOS user ids; "*" = any DataOS user')
    roles: list[str] = Field(default_factory=list, description="Heimdall role ids, e.g. engineering-tenant-admin")


class Disk(BaseModel):
    """A persistent disk that survives restarts and redeploys (DataOS makes the app a StatefulSet)."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)
    size: str = Field(pattern=r"^[0-9]+(\.[0-9]+)?(Mi|Gi|Ti)$", description="e.g. 1Gi")
    mount_path: str = Field(alias="mountPath", pattern=r"^/[^\s]*$")
    type: str = "disk"


class AppSpec(BaseModel):
    """What a person or agent writes in dapp.yaml."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    name: str
    description: str
    image: str | None = Field(None, description="Container image; or use runtime + code + run instead")
    runtime: str | None = Field(None, description="Run from source on a dapp runtime image, e.g. python-3.12")
    code: str | None = Field(None, description="Folder with the app's source (relative to dapp.yaml), for runtime:")
    run: str | None = Field(None, description="Command that starts the app inside the code folder, for runtime:")
    port: int = 8080
    tenant: str | None = None
    title: str | None = None
    icon: str | None = None
    domain: str | None = None
    command: list[str] | None = None
    args: list[str] | None = None
    env: dict[str, str] = Field(default_factory=dict)
    secrets: list[str] = Field(default_factory=list, description="Existing DataOS secrets to inject as env vars")
    compute: str | None = None
    run_as: str | None = Field(None, alias="runAs")
    replicas: int = 1
    resources: Resources = Field(default_factory=Resources)
    autoscale: AutoScale | None = None
    path: str | None = None
    strip_path: bool = Field(False, alias="stripPath",
                             description="Remove the path prefix before the request reaches the app (static servers)")
    public: bool = Field(False, description="No sign-in at all: anyone with the URL can open it")
    login: bool | None = Field(None, description="DataOS sign-in inside the app via dapp_login (default: not public)")
    access: Access = Field(default_factory=Access)
    llm: bool = Field(False, description="Route LLM calls through the dapp gateway and meter tokens")
    metrics: bool = Field(False, description="The app runs dapp_metrics, so DataOS scrapes its CPU/memory (port 9102)")
    tools: list[str] = Field(default_factory=list,
                             description="Gateway MCP tools the app may call (needs llm: true: it uses its gateway key)")
    owners: list[str] = Field(default_factory=list)
    tags: list[str] = Field(default_factory=list)
    working_dirs: list[WorkingDir] = Field(default_factory=list, alias="workingDirs")
    disk: Disk | None = Field(None, description="Persistent disk; makes the app stateful (one replica per disk)")
    image_pull_secret: str | None = Field(None, alias="imagePullSecret")
    hostname: str | None = None

    @field_validator("name")
    @classmethod
    def _name(cls, v: str) -> str:
        if not NAME_RE.match(v):
            raise ValueError("name must be lowercase letters, digits and '-', start with a letter, max 48 chars")
        return v

    @field_validator("env")
    @classmethod
    def _env(cls, v: dict[str, str]) -> dict[str, str]:
        clash = sorted(set(v) & RESERVED_ENV)
        if clash:
            raise ValueError(f"env {', '.join(clash)} is set by DataOS for every app; remove it from dapp.yaml")
        return v

    @field_validator("tools")
    @classmethod
    def _tools(cls, v: list[str]) -> list[str]:
        bad = [t for t in v if not re.match(r"^[a-z0-9]([a-z0-9-]{0,46}[a-z0-9])?$", t)]
        if bad:
            raise ValueError(f"tool names are lower-case letters, digits and '-': {', '.join(bad)}")
        return v

    @model_validator(mode="after")
    def _image_or_runtime(self) -> AppSpec:
        if bool(self.image) == bool(self.runtime):
            raise ValueError("set either image: (a container image) or runtime: (run from source), not both")
        if self.runtime:
            if not RUNTIME_RE.match(self.runtime):
                raise ValueError(f"runtime must be one of: {', '.join(RUNTIMES)}")
            if not (self.code and self.run):
                raise ValueError("runtime: needs code: (the source folder) and run: (the start command)")
            if self.command:
                raise ValueError("runtime apps start with run:, not command:")
            if self.uses_login:
                raise ValueError("sign-in for runtime apps isn't built yet: set public: true (anyone with the URL "
                                 "can open it), or deploy an image with dapp_login")
        elif self.code or self.run:
            raise ValueError("code: and run: are for runtime: apps; with image: use command:")
        return self

    @property
    def code_secret(self) -> str:
        return f"{SECRET_PREFIX}{self.name}-code"

    @model_validator(mode="after")
    def _tools_need_llm(self) -> AppSpec:
        if self.tools and not self.llm:
            raise ValueError("tools: needs llm: true (the app calls tools with its gateway key)")
        return self

    @field_validator("port")
    @classmethod
    def _port(cls, v: int) -> int:
        if not 1 <= v <= 65535:
            raise ValueError("port must be 1-65535")
        return v

    @property
    def http_path(self) -> str:
        p = self.path or f"/{self.name}"
        return p if p.startswith("/") else f"/{p}"

    @property
    def llm_secret(self) -> str:
        return llm_secret_name(self.name)

    @property
    def oidc_secret(self) -> str:
        return f"{SECRET_PREFIX}{self.name}{OIDC_SECRET_SUFFIX}"

    @property
    def uses_login(self) -> bool:
        return (not self.public) if self.login is None else (self.login and not self.public)


def load_spec(path: str | Path) -> AppSpec:
    data = yaml.safe_load(Path(path).read_text()) or {}
    spec = AppSpec.model_validate(data)
    if spec.code and not Path(spec.code).is_absolute():  # code: is relative to the dapp.yaml, not the shell
        spec = spec.model_copy(update={"code": str((Path(path).resolve().parent / spec.code).resolve())})
    return spec


def runtime_image(runtime: str) -> str:
    """Pinned to a revision: a moving tag wouldn't roll pods that already run an older build."""
    registry = os.environ.get("DAPP_RUNTIME_REGISTRY", "docker.io/tmdcio")
    return f"{registry}/dapp-runtime:{runtime}-{RUNTIME_REVISION}"


def oidc_client_id(tenant: str, app: str) -> str:
    return f"dapp-{tenant}-{app}"


def login_env(spec: AppSpec, *, tenant: str, fqdn: str, owners: list[str]) -> dict[str, str]:
    base = f"https://{fqdn}"
    users = sorted(set(owners) | set(spec.access.users))
    return {
        "DAPP_AUTH": "oidc",
        "DAPP_OIDC_ISSUER": f"{base}/oidc",
        "DAPP_OIDC_CLIENT_ID": oidc_client_id(tenant, spec.name),
        "DAPP_PUBLIC_URL": f"{base}{spec.http_path}",
        "DAPP_HEIMDALL_URL": f"{base}/heimdall",
        ALLOWED_USERS_ENV: ",".join(users),
        ALLOWED_ROLES_ENV: ",".join(sorted(set(spec.access.roles))),
    }


def render_app(spec: AppSpec, *, tenant: str, user_id: str | None, compute: str | None = None,
               fqdn: str | None = None) -> dict[str, Any]:
    owners = spec.owners or ([user_id] if user_id else [])
    if not owners:
        raise ValueError("owners is empty and the current user is unknown; set owners in dapp.yaml")
    compute = compute or spec.compute  # explicit --compute overrides dapp.yaml
    if not compute:
        raise ValueError("compute is required (set `compute:` in dapp.yaml or --compute)")

    if spec.runtime:
        spec = spec.model_copy(update={"image": runtime_image(spec.runtime), "command": RUNTIME_COMMAND,
                                       "env": {"DAPP_RUN": spec.run, **spec.env}})
    if not spec.command:
        raise ValueError(
            "command is required by the DataOS app schema: set `command:` (and `args:`) in dapp.yaml to the "
            "image's entrypoint, e.g. command: [/docker-entrypoint.sh] args: [nginx, -g, 'daemon off;']"
        )
    secrets = list(spec.secrets)
    if spec.runtime and spec.code_secret not in secrets:
        secrets.append(spec.code_secret)
    if spec.llm and spec.llm_secret not in secrets:
        secrets.append(spec.llm_secret)
    env = dict(spec.env)
    if spec.uses_login:
        if not fqdn:
            raise ValueError("login needs the DataOS fqdn")
        env = {**login_env(spec, tenant=tenant, fqdn=fqdn, owners=owners), **env}
        if spec.oidc_secret not in secrets:
            secrets.append(spec.oidc_secret)

    service: dict[str, Any] = {
        "type": "container",
        "image": spec.image,
        "port": spec.port,
        "http": {
            "path": spec.http_path,
            "stripPath": spec.strip_path,
            # login apps check identity themselves (dapp_login); Kong's path policy would 403 everyone
            "noNetworkAuthorization": spec.public or spec.uses_login,
            "ingress": {"enabled": True, "path": spec.http_path, "stripPath": spec.strip_path},
        },
    }
    if spec.uses_login:
        service["http"]["oidcClient"] = {
            "enabled": True,
            "clientId": oidc_client_id(tenant, spec.name),
            "clientSecret": f"{tenant}:{spec.oidc_secret}",
            "redirectPaths": [f"{spec.http_path}/_dapp/callback"],
        }
    if spec.command:
        service["command"] = spec.command
    if spec.args:
        service["arguments"] = spec.args
    if env:
        service["envs"] = {k: str(v) for k, v in env.items()}
    if secrets:
        service["secrets"] = [{"id": s} for s in secrets]
    if spec.disk:
        if spec.replicas != 1:
            raise ValueError("an app with a disk runs a single replica (replicas: 1)")
        clash = [w.directory for w in spec.working_dirs if w.directory.rstrip("/") == spec.disk.mount_path.rstrip("/")]
        if clash:
            raise ValueError(f"workingDirs and disk both mount {clash[0]}: drop the workingDir, the disk replaces it")
        service["stateful"] = True  # DataOS rejects a disk on a stateless app
        service["disk"] = {"size": spec.disk.size, "type": spec.disk.type, "mountPath": spec.disk.mount_path}
    if spec.metrics:
        service["metricEndpoint"] = {"port": METRICS_PORT, "path": METRICS_PATH}
    if spec.working_dirs:
        service["workingDirs"] = [w.model_dump() for w in spec.working_dirs]
    if spec.image_pull_secret:
        service["imagePullSecret"] = spec.image_pull_secret
    if spec.hostname:
        service["hostname"] = spec.hostname
    if spec.autoscale:
        a = spec.autoscale
        service["autoScaling"] = {
            "enabled": True,
            "minReplicas": a.min,
            "maxReplicas": a.max,
            "targetCPUUtilizationPercentage": a.cpu,
            **({"targetMemoryUtilizationPercentage": a.memory} if a.memory else {}),
        }

    app_meta: dict[str, Any] = {
        "title": spec.title or spec.name,
        "domain": spec.domain or tenant,
        "description": spec.description,
        "owners": owners,
        "visibility": "tenant",
        "link": {"title": "Open app", "url": f"{spec.http_path}/"},
    }
    if spec.icon:
        app_meta["icon"] = spec.icon
    if spec.tags:
        app_meta["tags"] = spec.tags

    body: dict[str, Any] = {
        "compute": compute,
        "replicas": spec.replicas,
        "resources": {
            "requests": {"cpu": spec.resources.cpu, "memory": spec.resources.memory},
            "limits": {"cpu": spec.resources.cpu_limit, "memory": spec.resources.memory_limit},
        },
        "app": app_meta,
        "service": service,
    }
    body["runAsUser"] = spec.run_as or owners[0]  # required by the app schema

    return {
        "name": spec.name,
        "version": "v1alpha",
        "type": "app",
        "tenant": tenant,
        "tags": [DAPP_TAG, app_tag(spec.name), *spec.tags],
        "description": spec.description,
        "spec": body,
    }


def render_secret(name: str, *, tenant: str, data: dict[str, str], description: str, app: str | None) -> dict:
    tags = [DAPP_TAG] + ([app_tag(app)] if app else [])
    return {
        "name": name,
        "version": "v2alpha",
        "type": "secret",
        "tenant": tenant,
        "tags": tags,
        "description": description,
        "spec": {"type": "key-value", "data": data},
    }


def render_grant(
    *,
    name: str,
    tenant: str,
    use_case: str,
    subjects: list[str],
    values: dict[str, list[dict[str, str]]],
    requester: str,
    notes: str,
    app: str | None,
) -> dict:
    tags = ["governance", "grant", DAPP_TAG] + ([app_tag(app)] if app else [])
    return {
        "name": name,
        "version": "v2alpha",
        "type": "grant",
        "tenant": tenant,
        "tags": tags,
        "description": notes,
        "grant": {
            "policy_use_case_id": use_case,
            "subjects": subjects,
            "values": values,
            "collection": tenant,
            "requester": requester,
            "notes": notes,
            "approve": True,
        },
    }


def subject_for(user: str) -> str:
    return user if ":" in user else f"users:id:{user}"


def dump_yaml(*docs: dict) -> str:
    return yaml.safe_dump_all(list(docs), sort_keys=False)
